"""SQLite 历史记忆存储层。

三张表：
  conversations  对话表       一行 = 一段对话
  turns          轮次表       一行 = 一轮问答
  answers        成员输出表   一行 = 某成员在某轮的答案

关联链：answers.turn_id -> turns.turn_id -> turns.conv_id -> conversations.conv_id
多段对话靠这条链天然隔离。

总结机制（长期记忆）：
  - small_summary：每轮每个角色(AI自己)写一条小总结
  - big_summary：每 5 轮把"上次大总结 + 最近5条小总结"压成一条大总结
  - 为空本身即标注：big_summary 为空的轮次是普通轮，非空的是大总结边界轮
"""

import os
import json
import sqlite3
import datetime

# data/ 目录位于项目根（core 的上一级）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "conversations.db")


def _now() -> str:
    """当前时间字符串（ISO 格式）"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_connection() -> sqlite3.Connection:
    """获取数据库连接；自动确保 data/ 目录存在。
    用 with get_connection() as conn 时，退出会自动提交/回滚（sqlite3 的上下文管理器行为）。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row      # 查询结果可按列名访问：row["conv_id"]
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_column(conn, table: str, column: str, coltype: str = "TEXT") -> None:
    """给已存在的表补列（迁移用）；列已存在则跳过。"""
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_db() -> None:
    """建表、索引与列迁移（幂等，可重复调用）。"""
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
                seq             INTEGER NOT NULL,
                question        TEXT NOT NULL,
                verdict         TEXT,
                arbiter_role_id TEXT,
                small_summary   TEXT,
                big_summary     TEXT,
                created_at      TEXT,
                UNIQUE(conv_id, seq),
                FOREIGN KEY (conv_id) REFERENCES conversations(conv_id)
            );
            CREATE INDEX IF NOT EXISTS idx_turns_conv_id ON turns(conv_id);

            CREATE TABLE IF NOT EXISTS answers (
                answer_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                turn_id       INTEGER NOT NULL,
                role_id       TEXT NOT NULL,
                content       TEXT,
                trajectory    TEXT,
                small_summary TEXT,
                big_summary   TEXT,
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

# 用于让无参工具（如 Get_History_Memory）知道"当前在跟哪段对话交互"。
# 单活跃对话的简化方案；将来前端支持多对话并行时需改用更严格的上下文管理。
_current_conv_id = None


def set_current_conversation(conv_id) -> None:
    """设置当前对话 id"""
    global _current_conv_id
    _current_conv_id = conv_id


def get_current_conversation():
    """获取当前对话 id；未设置时返回 None"""
    return _current_conv_id


# ---------------------- 对话表 conversations ----------------------

def create_conversation(title: str = "新对话") -> int:
    """新建一段对话，返回 conv_id"""
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO conversations (title, created_at) VALUES (?, ?)",
            (title, _now()),
        )
        return cur.lastrowid


def list_conversations() -> list:
    """列出所有对话（含轮次数），按创建时间倒序"""
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
    """删除一段对话（连带其轮次与成员输出）"""
    with get_connection() as conn:
        # 先删子表，再删父表，避免外键约束报错
        conn.execute(
            "DELETE FROM answers WHERE turn_id IN "
            "(SELECT turn_id FROM turns WHERE conv_id = ?)",
            (conv_id,),
        )
        conn.execute("DELETE FROM turns WHERE conv_id = ?", (conv_id,))
        conn.execute("DELETE FROM conversations WHERE conv_id = ?", (conv_id,))


# ---------------------- 轮次表 turns ----------------------

def get_next_seq(conv_id: int) -> int:
    """计算该对话下一轮的轮次序号（从 1 开始）"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM turns WHERE conv_id = ?",
            (conv_id,),
        ).fetchone()
        return row["max_seq"] + 1


def add_turn(conv_id: int, question: str, verdict: str = None,
             arbiter_role_id: str = None, small_summary: str = None,
             seq: int = None) -> int:
    """写入一轮问答，返回 turn_id；seq 省略时自动取下一序号"""
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
    """查询某轮的轮次序号（seq）；不存在返回 None"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT seq FROM turns WHERE turn_id = ?", (turn_id,)
        ).fetchone()
        return row["seq"] if row else None


def update_turn_verdict(turn_id: int, verdict: str,
                        arbiter_role_id: str = None) -> None:
    """补充/更新某一轮的审核员输出"""
    with get_connection() as conn:
        conn.execute(
            "UPDATE turns SET verdict = ?, arbiter_role_id = ? WHERE turn_id = ?",
            (verdict, arbiter_role_id, turn_id),
        )


def get_recent_turns(conv_id: int, n: int = 4) -> list:
    """取该对话最近 N 轮（含各成员输出），返回按时间正序的列表。
    对应简介的"滑动窗口缓存最近 N 次对话"。"""
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
    """取该对话的全部轮次（含各成员输出），按轮次正序"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM turns WHERE conv_id = ? ORDER BY seq ASC",
            (conv_id,),
        ).fetchall()
        return [_turn_with_answers(conn, r) for r in rows]


def _turn_with_answers(conn, turn_row) -> dict:
    """把一条轮次记录组装成 dict，并挂上它的成员输出"""
    turn = dict(turn_row)
    turn["answers"] = _fetch_answers(conn, turn["turn_id"])
    return turn


# ---------------------- 成员输出表 answers ----------------------

def add_answer(turn_id: int, role_id: str, content: str,
               trajectory: list = None, small_summary: str = None) -> int:
    """写入某成员在某轮的输出，返回 answer_id。
    trajectory 为轨迹列表，内部转成 JSON 字符串存储。"""
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
    """内部：取某轮的全部成员输出（含轨迹还原）"""
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
    """取某轮的全部成员输出（含轨迹还原）"""
    with get_connection() as conn:
        return _fetch_answers(conn, turn_id)


# ---------------------- 组合写入 ----------------------

def save_turn(conv_id: int, question: str, members: list,
              verdict: str = None, arbiter_role_id: str = None,
              arbiter_small_summary: str = None) -> int:
    """一次性写入一轮完整问答：轮次 + 各成员输出，返回 turn_id。

    members 形如：
        [{"role_id": "mathematician", "content": "...", "trajectory": [...],
          "small_summary": "..."},
         {"role_id": "programmer",    "content": "...", "trajectory": [...]}]
    """
    turn_id = add_turn(conv_id, question, verdict, arbiter_role_id,
                       small_summary=arbiter_small_summary)
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
# is_arbiter 用来区分从哪张表读/写。

def get_summary_line(conv_id: int, role_id: str, is_arbiter: bool = False) -> list:
    """取某角色的总结线：按轮次升序返回
    [{"seq":.., "small_summary":.., "big_summary":..}, ...]"""
    with get_connection() as conn:
        if is_arbiter:
            rows = conn.execute(
                """SELECT seq, small_summary, big_summary FROM turns
                   WHERE conv_id = ? AND arbiter_role_id = ?
                   ORDER BY seq ASC""",
                (conv_id, role_id),
            ).fetchall()
        else:
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
    """为大总结准备输入：返回 (上一次大总结 or "", [最近5条小总结...])。
    上一次大总结 = 第 (current_seq - 5) 轮的大总结（往前数第6条）。"""
    line = get_summary_line(conv_id, role_id, is_arbiter)
    previous_big = ""
    for row in line:
        if row["seq"] == current_seq - 5 and row["big_summary"]:
            previous_big = row["big_summary"]
    recent_smalls = [
        row["small_summary"] for row in line
        if current_seq - 4 <= row["seq"] <= current_seq and row["small_summary"]
    ]
    return previous_big, recent_smalls


def write_big_summary(conv_id: int, role_id: str, seq: int, text: str,
                      is_arbiter: bool = False) -> None:
    """把某角色第 seq 轮的大总结写进库（不覆盖 small_summary）"""
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
    """重建某角色的长期记忆文本（恢复对话时用）：
    最近一条非空 big_summary + 其后所有非空 small_summary。
    若从无大总结，则用全部小总结。"""
    line = get_summary_line(conv_id, role_id, is_arbiter)
    last_big = None
    for i, row in enumerate(line):
        if row["big_summary"]:
            last_big = i
    parts = []
    if last_big is not None:
        parts.append(line[last_big]["big_summary"])
        for row in line[last_big + 1:]:
            if row["small_summary"]:
                parts.append(row["small_summary"])
    else:
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
