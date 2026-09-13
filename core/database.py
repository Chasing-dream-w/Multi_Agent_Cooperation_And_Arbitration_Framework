"""database.py —— SQLite 历史记忆存储层（系统的"记忆"）

三张表（关系链：answers → turns → conversations）：
    conversations  对话表     一行 = 一段对话
    turns          轮次表     一行 = 一轮问答（问题 + 裁决）
    answers        成员输出表 一行 = 某成员在某轮的答案
    关联：answers.turn_id → turns.turn_id → turns.conv_id → conversations.conv_id
    多段对话靠这条链天然隔离。

长期记忆（总结机制）：
    small_summary  每轮每个角色写一条小总结（≤100字）
    big_summary    每 5 轮把"上次大总结 + 最近5条小总结"压成一条大总结
    为空本身即标注：big_summary 为空的轮是普通轮，非空的是大总结边界轮。

并发控制（防多用户串台）：
    ① WAL 模式 + busy_timeout —— 读写可并行，写冲突时等待而非报错
    ② 写锁 _turn_write_lock   —— 保护"算 seq + 插入"，防止 seq 撞号
    ③ ContextVar _current_conv_id —— 每个线程/请求各持一份"当前对话"，互不干扰
"""

import os
import json
import sqlite3
import datetime
import threading
import contextvars

# data/ 目录位于项目根（core 的上一级）；数据库文件固定叫 conversations.db
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "conversations.db")


def _now() -> str:
    """当前时间字符串（ISO 格式），用于各表的 created_at 字段。"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_connection() -> sqlite3.Connection:
    """打开一个数据库连接（用完即关，配合 with 使用会自动提交/回滚）。
    每次调用都确保 data/ 目录存在，并设置好并发相关的 PRAGMA。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    # timeout=5.0：遇到锁时最多等 5 秒（对应下面的 busy_timeout）
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row      # 让查询结果支持按列名访问：row["conv_id"]
    conn.execute("PRAGMA foreign_keys = ON")    # 开启外键约束
    conn.execute("PRAGMA journal_mode = WAL")   # WAL 模式：读写可并行，不再互相阻塞
    conn.execute("PRAGMA busy_timeout = 5000")  # 锁等待 5000 毫秒
    return conn


def _ensure_column(conn, table: str, column: str, coltype: str = "TEXT") -> None:
    """给已存在的表补列（数据库迁移用）：列已存在则跳过。
    这样老版本的数据库文件也能平滑升级，不用删库重建。"""
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_db() -> None:
    """建表、建索引、补齐缺列。幂等——可以重复调用，已存在则跳过。"""
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                conv_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                title      TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS turns (
                turn_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                conv_id         INTEGER NOT NULL,
                seq             INTEGER NOT NULL,          -- 轮次序号（对话内从1开始）
                question        TEXT NOT NULL,
                verdict         TEXT,                      -- 审核员裁决
                arbiter_role_id TEXT,                      -- 本轮用的是哪个审核员
                small_summary   TEXT,                      -- 裁判本轮小结
                big_summary     TEXT,                      -- 裁判大总结（每5轮）
                created_at      TEXT,
                UNIQUE(conv_id, seq),                      -- 同一对话内轮次不重号
                FOREIGN KEY (conv_id) REFERENCES conversations(conv_id)
            );
            CREATE INDEX IF NOT EXISTS idx_turns_conv_id ON turns(conv_id);

            CREATE TABLE IF NOT EXISTS answers (
                answer_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                turn_id       INTEGER NOT NULL,
                role_id       TEXT NOT NULL,               -- 哪个成员（如 mathematician）
                content       TEXT,                        -- 该成员的答案
                trajectory    TEXT,                        -- 思考轨迹（JSON 字符串）
                small_summary TEXT,                        -- 该成员本轮小结
                big_summary   TEXT,                        -- 该成员大总结（每5轮）
                created_at    TEXT,
                FOREIGN KEY (turn_id) REFERENCES turns(turn_id)
            );
            CREATE INDEX IF NOT EXISTS idx_answers_turn_id ON answers(turn_id);
            """
        )
        # 迁移：为早先已建、缺列的库补上新列
        _ensure_column(conn, "turns", "small_summary")
        _ensure_column(conn, "turns", "big_summary")
        _ensure_column(conn, "answers", "small_summary")
        _ensure_column(conn, "answers", "big_summary")


# ---------------------- 当前对话上下文 ----------------------

# 用 ContextVar 而非模块级全局变量：每个线程/请求各有一份独立的值，
# 多用户并发时不会互相串台。注意：新建线程不会自动继承上下文，
# 需在提交任务时用 contextvars.copy_context() 传入（见 main.parallel_solve）。
_current_conv_id = contextvars.ContextVar("current_conv_id", default=None)


def set_current_conversation(conv_id) -> None:
    """设置"当前对话"——让无参工具（如 Get_History_Memory）知道该读哪段对话的记忆。
    只作用于当前上下文（当前线程/请求），不影响其他并发请求。"""
    _current_conv_id.set(conv_id)


def get_current_conversation():
    """读取"当前对话 id"；未设置时返回 None。"""
    return _current_conv_id.get()


# ---------------------- 对话表 conversations ----------------------

def create_conversation(title: str = "新对话") -> int:
    """新建一段对话，返回它的 conv_id（自增主键）。"""
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO conversations (title, created_at) VALUES (?, ?)",
            (title, _now()),
        )
        return cur.lastrowid


def list_conversations() -> list:
    """列出所有对话（含各自的轮次数），按 conv_id 倒序（最新的在前）。
    供"选择已有对话"时展示。"""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT c.conv_id, c.title, c.created_at,
                   COUNT(t.turn_id) AS turn_count
            FROM conversations c
            LEFT JOIN turns t ON t.conv_id = c.conv_id
            GROUP BY c.conv_id
            ORDER BY c.conv_id DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def delete_conversation(conv_id: int) -> None:
    """删除一段对话，连同它的所有轮次与成员输出。
    注意删除顺序：先删最底层的 answers，再删 turns，最后删 conversations，
    否则会因为外键约束报错。"""
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM answers WHERE turn_id IN "
            "(SELECT turn_id FROM turns WHERE conv_id = ?)",
            (conv_id,),
        )
        conn.execute("DELETE FROM turns WHERE conv_id = ?", (conv_id,))
        conn.execute("DELETE FROM conversations WHERE conv_id = ?", (conv_id,))


def get_roles_used(conv_id: int) -> dict:
    """查出一段对话历史中用过的角色，返回 {"members": [...], "arbiters": [...]}。
    用途：继续旧对话时，据此预选角色（避免换一批角色导致没有记忆可恢复）。"""
    with get_connection() as conn:
        # 成员：answers 表里出现过的 role_id
        member_rows = conn.execute(
            """SELECT DISTINCT a.role_id FROM answers a
               JOIN turns t ON a.turn_id = t.turn_id
               WHERE t.conv_id = ?""",
            (conv_id,),
        ).fetchall()
        # 审核员：turns 表里出现过的 arbiter_role_id
        arbiter_rows = conn.execute(
            """SELECT DISTINCT arbiter_role_id FROM turns
               WHERE conv_id = ? AND arbiter_role_id IS NOT NULL""",
            (conv_id,),
        ).fetchall()
        return {
            "members": [r["role_id"] for r in member_rows],
            "arbiters": [r["arbiter_role_id"] for r in arbiter_rows],
        }


# ---------------------- 轮次表 turns ----------------------

def get_next_seq(conv_id: int) -> int:
    """算出该对话"下一轮"的序号 seq（从 1 开始）。
    即当前最大的 seq + 1。"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM turns WHERE conv_id = ?",
            (conv_id,),
        ).fetchone()
        return row["max_seq"] + 1


# 写锁：保护"分配 seq + 插入"这一段的原子性。
# 否则两个并发写入可能算出同一个 seq，第二个插入会撞 UNIQUE(conv_id, seq) 报错。
_turn_write_lock = threading.Lock()


def add_turn(conv_id: int, question: str, verdict: str = None,
             arbiter_role_id: str = None, small_summary: str = None,
             seq: int = None) -> int:
    """写入一轮问答，返回 turn_id。
    seq 省略时自动取下一序号；整个"算 seq + 插入"用锁保护，避免并发撞号。"""
    with _turn_write_lock:
        if seq is None:
            seq = get_next_seq(conv_id)
        with get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO turns
                    (conv_id, seq, question, verdict, arbiter_role_id,
                     small_summary, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (conv_id, seq, question, verdict, arbiter_role_id,
                 small_summary, _now()),
            )
            return cur.lastrowid


def get_turn_seq(turn_id: int):
    """由 turn_id 反查它的轮次 seq；不存在返回 None。
    （写入后用它判断"是不是第5轮"，以触发大总结。）"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT seq FROM turns WHERE turn_id = ?", (turn_id,)
        ).fetchone()
        return row["seq"] if row else None


def update_turn_verdict(turn_id: int, verdict: str,
                        arbiter_role_id: str = None) -> None:
    """事后补充/更新某一轮的审核员裁决。"""
    with get_connection() as conn:
        conn.execute(
            "UPDATE turns SET verdict = ?, arbiter_role_id = ? WHERE turn_id = ?",
            (verdict, arbiter_role_id, turn_id),
        )


def get_recent_turns(conv_id: int, n: int = 4) -> list:
    """取该对话最近 N 轮（含各成员的输出），返回按 seq 正序的列表。
    这就是"滑动窗口缓存最近 N 次对话"的实现：先倒序取 N 条，再翻正序。"""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM (
                SELECT * FROM turns
                WHERE conv_id = ?
                ORDER BY seq DESC
                LIMIT ?
            ) ORDER BY seq ASC
            """,
            (conv_id, n),
        ).fetchall()
        return [_turn_with_answers(conn, r) for r in rows]


def get_all_turns(conv_id: int) -> list:
    """取该对话的全部轮次（含各成员输出），按 seq 正序。"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM turns WHERE conv_id = ? ORDER BY seq ASC",
            (conv_id,),
        ).fetchall()
        return [_turn_with_answers(conn, r) for r in rows]


def _turn_with_answers(conn, turn_row) -> dict:
    """内部辅助：把一条 turns 记录转成 dict，并挂上它对应的成员输出 answers。"""
    turn = dict(turn_row)
    turn["answers"] = _fetch_answers(conn, turn["turn_id"])
    return turn


# ---------------------- 成员输出表 answers ----------------------

def add_answer(turn_id: int, role_id: str, content: str,
               trajectory: list = None, small_summary: str = None) -> int:
    """写入"某成员在某轮的输出"，返回 answer_id。
    trajectory 是列表，存储时用 JSON 字符串序列化（读取时再反序列化）。"""
    traj_text = json.dumps(trajectory, ensure_ascii=False) if trajectory is not None else None
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO answers
                (turn_id, role_id, content, trajectory, small_summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (turn_id, role_id, content, traj_text, small_summary, _now()),
        )
        return cur.lastrowid


def _fetch_answers(conn, turn_id: int) -> list:
    """内部：取某轮的全部成员输出。会把 trajectory 从 JSON 字符串还原成列表。"""
    rows = conn.execute(
        "SELECT role_id, content, trajectory, small_summary, big_summary, created_at "
        "FROM answers WHERE turn_id = ? ORDER BY answer_id ASC",
        (turn_id,),
    ).fetchall()
    result = []
    for r in rows:
        item = dict(r)
        item["trajectory"] = json.loads(item["trajectory"]) if item["trajectory"] else []
        result.append(item)
    return result


def get_answers(turn_id: int) -> list:
    """取某轮的全部成员输出（轨迹已还原为列表）。"""
    with get_connection() as conn:
        return _fetch_answers(conn, turn_id)


# ---------------------- 组合写入 ----------------------

def save_turn(conv_id: int, question: str, members: list,
              verdict: str = None, arbiter_role_id: str = None,
              arbiter_small_summary: str = None) -> int:
    """一次写入一整轮：先写 turns 一行，再逐个写各成员的 answers，返回 turn_id。

    members 形如：
        [{"role_id": "mathematician", "content": "...", "trajectory": [...],
          "small_summary": "..."},
         {"role_id": "programmer",    "content": "...", "trajectory": [...]}]
    """
    # 先写轮次（含裁判裁决与裁判小结）
    turn_id = add_turn(conv_id, question, verdict, arbiter_role_id,
                       small_summary=arbiter_small_summary)
    # 再逐个写成员输出
    for m in members:
        add_answer(
            turn_id,
            role_id=m["role_id"],
            content=m.get("content", ""),
            trajectory=m.get("trajectory"),
            small_summary=m.get("small_summary"),
        )
    return turn_id


# ---------------------- 总结线（长期记忆） ----------------------

# 成员角色的总结存在 answers 表；审核员的总结存在 turns 表。
# 参数 is_arbiter 用来区分该从哪张表读/写。

def get_summary_line(conv_id: int, role_id: str, is_arbiter: bool = False) -> list:
    """取某角色在这段对话里的"总结线"（按轮次升序）。
    返回形如 [{"seq":.., "small_summary":.., "big_summary":..}, ...]
    这是"每个角色一条独立记忆线"的读取入口。"""
    with get_connection() as conn:
        if is_arbiter:
            # 裁判的总结在 turns 表
            rows = conn.execute(
                """SELECT seq, small_summary, big_summary FROM turns
                   WHERE conv_id = ? AND arbiter_role_id = ?
                   ORDER BY seq ASC""",
                (conv_id, role_id),
            ).fetchall()
        else:
            # 成员的总结在 answers 表（经由 turn_id 关联回 turns 拿 seq）
            rows = conn.execute(
                """SELECT t.seq, a.small_summary, a.big_summary
                   FROM answers a JOIN turns t ON a.turn_id = t.turn_id
                   WHERE t.conv_id = ? AND a.role_id = ?
                   ORDER BY t.seq ASC""",
                (conv_id, role_id),
            ).fetchall()
        return [dict(r) for r in rows]


def get_summary_context(conv_id: int, role_id: str, current_seq: int,
                        is_arbiter: bool = False):
    """为大总结准备输入，返回二元组 (上一次大总结文本, [最近5条小总结...])。
    "上一次大总结" = 第 (current_seq - 5) 轮的大总结（即往前数第6条）。"""
    line = get_summary_line(conv_id, role_id, is_arbiter)
    # 找上一次大总结
    previous_big = ""
    for row in line:
        if row["seq"] == current_seq - 5 and row["big_summary"]:
            previous_big = row["big_summary"]
    # 收集最近5轮的小总结
    recent_smalls = [
        row["small_summary"] for row in line
        if current_seq - 4 <= row["seq"] <= current_seq and row["small_summary"]
    ]
    return previous_big, recent_smalls


def write_big_summary(conv_id: int, role_id: str, seq: int, text: str,
                      is_arbiter: bool = False) -> None:
    """把某角色第 seq 轮的大总结写入库（只写 big_summary 列，不动 small_summary）。"""
    with get_connection() as conn:
        if is_arbiter:
            conn.execute(
                """UPDATE turns SET big_summary = ?
                   WHERE conv_id = ? AND arbiter_role_id = ? AND seq = ?""",
                (text, conv_id, role_id, seq),
            )
        else:
            conn.execute(
                """UPDATE answers SET big_summary = ?
                   WHERE role_id = ? AND turn_id IN
                   (SELECT turn_id FROM turns WHERE conv_id = ? AND seq = ?)""",
                (text, role_id, conv_id, seq),
            )


def get_role_memory_text(conv_id: int, role_id: str,
                         is_arbiter: bool = False) -> str:
    """重建某角色的长期记忆文本（恢复对话时用）。
    规则：最近一条非空 big_summary（含它之前的压缩历史）+ 之后所有非空 small_summary。
    若从来没有过大总结，则直接用全部小总结。"""
    line = get_summary_line(conv_id, role_id, is_arbiter)
    # 找到最后一条有大总结的位置
    last_big = None
    for i, row in enumerate(line):
        if row["big_summary"]:
            last_big = i
    parts = []
    if last_big is not None:
        # 有大总结：用它 + 其后的小总结（之前的已被它吸收，不重复读）
        parts.append(line[last_big]["big_summary"])
        for row in line[last_big + 1:]:
            if row["small_summary"]:
                parts.append(row["small_summary"])
    else:
        # 没有大总结：全部小总结
        for row in line:
            if row["small_summary"]:
                parts.append(row["small_summary"])
    return "\n".join(parts)


# ---------------------- 自测 ----------------------

if __name__ == "__main__":
    init_db()
    conv_id = create_conversation("测试对话")
    print("新建对话 conv_id =", conv_id)

    turn_id = save_turn(
        conv_id=conv_id,
        question="1+1等于几？",
        members=[
            {"role_id": "mathematician", "content": "2",
             "trajectory": [{"type": "thought", "content": "直接相加"}],
             "small_summary": "用户问1+1，我答2。"},
            {"role_id": "programmer", "content": "结果是 2",
             "trajectory": [], "small_summary": "用户问1+1，我答2。"},
        ],
        verdict="两个答案一致，结论为 2。",
        arbiter_role_id="referee",
        arbiter_small_summary="用户问1+1，两成员一致，裁定为2。",
    )
    print("写入轮次 turn_id =", turn_id, "seq =", get_turn_seq(turn_id))

    print("最近1轮:", get_recent_turns(conv_id, 1))
    print("成员总结线:", get_summary_line(conv_id, "mathematician"))
    print("全部对话:", list_conversations())
