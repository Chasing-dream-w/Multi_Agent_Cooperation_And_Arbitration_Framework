"""main.py —— 编排层（整个系统的"总指挥"）

它负责把各个零件串成一条完整的流水线，本身不实现具体逻辑：
    agents/    角色与推理（每个角色怎么思考、怎么调工具）
    core/      基础设施（模型客户端、工具集、数据库）

一次问答的完整流程（也是本文件最重要的脉络）：
    1. 启动 → choose_conversation() 选"新建对话"或"已有对话"
    2. 若选了已有对话 → restore_memory() 把各角色的长期记忆注入它们的上下文
    3. 用户输入问题 → parallel_solve()：
          多线程并发让各成员作答 → 汇总 → 交给审核员仲裁
    4. persist_turn() 把这一轮（问题/各成员答案/裁决）写进数据库
    5. maybe_big_summary() 每满 5 轮，为每个角色压缩一次长期记忆
    6. 回到第 3 步，进入下一轮

关键设计：写入数据库（persist_turn）是"系统的编排动作"，不是给模型调用的工具。
          模型能主动调的读记忆工具在 core/tools.py 的 Get_History_Memory。
"""

from concurrent.futures import ThreadPoolExecutor
import contextvars
from core.tools import Reset_Cache, Self_Summary
from core import database
from agents.base_agent import create_agent
from agents.roles_config import ROLE_REGISTRY

# 每积累多少轮，触发一次"大总结"（长期记忆压缩）。改这里即可调整频率。
BIG_SUMMARY_EVERY = 5


def parallel_solve(question: str, member_agents: list, arbiter_agent) -> dict:
    """核心编排：并发让所有成员作答，再交审核员仲裁。

    参数：
        question      - 用户问题
        member_agents - 已实例化的成员Agent列表（至少1个）
        arbiter_agent - 审核员Agent（1个）
    返回结构化 dict（供展示或落库）：
        {
            "max_workers": 线程池上限（= 成员数量）,
            "answers":     {成员名: 答案},
            "trajectories":{成员名: 思考轨迹},
            "review":      审核员裁决文本,
        }
    """
    # 线程数 = 成员数量：每个成员一个线程，真正并行（模型调用是网络IO，线程即可）
    max_workers = max(1, len(member_agents))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # 逐个提交任务。用 copy_context() 把当前线程的 ContextVar（即"当前对话id"）
        # 复制进子线程——新线程默认不继承上下文，不传的话子线程里 get_current_conversation()
        # 会取到 None，导致工具读错对话。这是并发不"串台"的关键。
        futures = {}
        for agent in member_agents:
            ctx = contextvars.copy_context()
            futures[agent.name] = executor.submit(ctx.run, agent.run, question)
        # 逐个取结果。即使某个成员抛异常也在这里被兜住，
        # 降级成一句失败文本，不让整轮崩掉。
        answers = {}
        for name, future in futures.items():
            try:
                answers[name] = future.result()
            except Exception as e:
                answers[name] = f"[{name} 作答失败：{type(e).__name__}: {e}]"
        # 轨迹取自 agent 实例本身（每个 agent 各自维护，互不干扰）
        trajectories = {agent.name: agent.get_trajectory()
                        for agent in member_agents}

    # 审核员仲裁：把"问题 + 各成员答案 + 各成员轨迹"交给它，产出裁决
    try:
        review = arbiter_agent.evaluate(
            user_input=question,
            candidates=answers,
            trajectories=trajectories,
        )
    except Exception as e:
        review = f"[审核员 裁决失败：{type(e).__name__}: {e}]"
    return {
        "max_workers": max_workers,
        "answers": answers,
        "trajectories": trajectories,
        "review": review,
    }


def persist_turn(conv_id: int, question: str, result: dict,
                 member_agents: list, arbiter_agent) -> int:
    """把一轮完整问答写入数据库，返回该轮的 turn_id。

    这是"写入接口"（编排层动作），不是给模型用的工具。
    它把 parallel_solve 的返回结果 + 各 agent 本轮的小结，整理成
    数据库三表所需的格式，一次写入：turns 一行 + answers 若干行。
    """
    # 成员名 → role_id：agents 用中文名标识，数据库用 role_id 存储
    name_to_role = {agent.name: agent.role_id for agent in member_agents}
    # 成员名 → 本轮小总结（由 base_agent 从回答的 <summary> 标签里切出来）
    name_to_summary = {agent.name: agent.summary for agent in member_agents}
    members = [
        {
            "role_id": name_to_role[name],
            "content": ans,
            "trajectory": result["trajectories"].get(name, []),
            "small_summary": name_to_summary.get(name, ""),
        }
        for name, ans in result["answers"].items()
    ]
    return database.save_turn(
        conv_id=conv_id,
        question=question,
        members=members,
        verdict=result["review"],
        arbiter_role_id=arbiter_agent.role_id,
        arbiter_small_summary=arbiter_agent.summary,   # 裁判本轮的小结
    )


def maybe_big_summary(conv_id: int, seq: int, member_agents: list,
                      arbiter_agent) -> None:
    """每满 BIG_SUMMARY_EVERY 轮，为每个角色生成一次"大总结"（长期记忆压缩）。

    每个角色各自独立：把「该角色上一次的大总结 + 最近几轮的小总结」再压成一段。
    注意是"同一角色纵向合并"，不涉及其他角色的内容。
    """
    # 轮次不是 5 的倍数就直接跳过
    if seq % BIG_SUMMARY_EVERY != 0:
        return

    # 要压缩的目标：所有成员（总结存 answers 表）+ 裁判（总结存 turns 表）
    targets = [(a.role_id, a.name, False) for a in member_agents]
    targets.append((arbiter_agent.role_id, arbiter_agent.name, True))

    for role_id, name, is_arbiter in targets:
        # 取"上次大总结 + 最近5条小总结"作为压缩输入
        previous_big, recent_smalls = database.get_summary_context(
            conv_id, role_id, seq, is_arbiter=is_arbiter)
        # 调用模型压缩成一条新的大总结
        big = Self_Summary(name, previous_big, recent_smalls)
        if big:
            database.write_big_summary(conv_id, role_id, seq, big,
                                       is_arbiter=is_arbiter)
    print(f"[大总结] 第 {seq} 轮已为各角色生成大总结")


def choose_conversation() -> tuple:
    """终端交互：让用户选择「新建对话」还是「继续已有对话」。

    返回 (conv_id, is_new)：
        is_new=True  → 新建（后面不需要恢复记忆）
        is_new=False → 选中已有对话（后面需要恢复记忆）
    """
    database.init_db()   # 确保表已建好（幂等）
    while True:
        choice = input("新建对话(N) / 选择已有对话(S)？(N/S，直接回车默认N):").strip().lower()
        # --- 新建 ---
        if choice in ("", "n", "new", "新建"):
            title = input("请输入新对话的话题/标题:").strip() or "未命名对话"
            conv_id = database.create_conversation(title)
            print(f"已新建对话：conv_id={conv_id}，标题「{title}」")
            return conv_id, True
        # --- 选择已有 ---
        if choice in ("s", "select", "选择"):
            convs = database.list_conversations()
            if not convs:
                # 一个都没有，退化为新建
                print("暂无已有对话，转为新建。")
                title = input("请输入新对话的话题/标题:").strip() or "未命名对话"
                conv_id = database.create_conversation(title)
                print(f"已新建对话：conv_id={conv_id}，标题「{title}」")
                return conv_id, True
            # 列出所有对话供选择（带序号）
            print("已有对话列表：")
            for i, c in enumerate(convs, start=1):
                print(f"  {i}. {c['title']}  （{c['turn_count']} 轮，{c['created_at']}）")
            raw = input("请输入要选择的序号（直接回车取消）:").strip()
            if raw == "":
                continue   # 回车 → 回到上一级重新选
            if not raw.isdigit() or not (1 <= int(raw) <= len(convs)):
                print("序号无效，请重新选择。")
                continue
            sel = convs[int(raw) - 1]
            print(f"已选择对话：conv_id={sel['conv_id']}，标题「{sel['title']}」")
            return sel["conv_id"], False
        print("输入无效，请输入 N 或 S。")


def restore_memory(conv_id: int, member_agents: list, arbiter_agent) -> None:
    """恢复已有对话：把各角色的长期记忆注入到各自 Agent 的上下文里。

    每个角色读自己的总结线（成员读 answers 表，裁判读 turns 表），
    再由 agent.load_memory() 作为一条 system 消息塞进它的对话历史。
    """
    restored = 0
    # 成员角色
    for agent in member_agents:
        text = database.get_role_memory_text(conv_id, agent.role_id, is_arbiter=False)
        agent.load_memory(text)
        if text:           # 有记忆才算"恢复了"
            restored += 1
    # 审核员
    arb_text = database.get_role_memory_text(conv_id, arbiter_agent.role_id, is_arbiter=True)
    arbiter_agent.load_memory(arb_text)
    if arb_text:
        restored += 1
    print(f"[记忆恢复] 已为 {restored} 个角色注入历史记忆")


def _pick_roles(pool: list, title: str, min_count: int, max_count: int = None,
                preselect: list = None) -> list:
    """通用的"终端选角色"交互。

    操作方式：
        输入序号（可空格分隔多个，如 "1 3"）→ 选中；再输一次同一序号 → 取消
        u = 撤销上一个选择
        d = 确认（需满足数量要求）
        q = 退出程序

    参数：
        pool      - 候选角色列表（每项是 ROLE_REGISTRY 里的一个 dict）
        title     - 提示标题
        min_count - 至少选几个
        max_count - 最多选几个（None 表示不限）
        preselect - 预选哪些 role_id（如"继续旧对话"时沿用上次用过的角色）
    返回：选中的 role_id 列表（保持选择顺序）。
    """
    pool_ids = {r["role_id"] for r in pool}
    # 预选：只勾上"确实存在于本池"的角色，顺序沿用 preselect
    selected = [rid for rid in (preselect or []) if rid in pool_ids]
    id_to_name = {r["role_id"]: r["name"] for r in pool}

    if selected:
        print(f"[提示] 已按该对话此前用过的角色预选：{'、'.join(id_to_name[x] for x in selected)}")

    while True:
        # 打印候选列表（已选中的打 [x] 标记）
        print("\n" + "=" * 52)
        print(title)
        for i, r in enumerate(pool, start=1):
            mark = "[x]" if r["role_id"] in selected else "[ ]"
            print(f"  {mark} {i:>2}. {r['name']}")
        # 打印当前已选
        chosen = "、".join(id_to_name[x] for x in selected)
        print(f"\n当前已选（{len(selected)} 个）：{chosen if chosen else '（空）'}")
        print("输入序号选中/取消（可空格分隔多个）；u=撤销上一个；d=确认；q=退出")

        raw = input("选择> ").strip().lower()

        # 退出程序
        if raw == "q":
            print("已退出。")
            raise SystemExit
        # 确认：检查数量约束
        if raw == "d":
            if len(selected) < min_count:
                print(f"[!] 至少要选 {min_count} 个，当前只有 {len(selected)} 个。")
                continue
            if max_count is not None and len(selected) > max_count:
                print(f"[!] 最多只能选 {max_count} 个。")
                continue
            return selected
        # 撤销上一个
        if raw == "u":
            if selected:
                print(f"[撤销] {id_to_name[selected.pop()]}")
            else:
                print("[!] 还没有选择，无法撤销。")
            continue

        # 否则按序号处理（支持一次输入多个）
        parts = raw.replace(",", " ").replace("，", " ").split()
        if not parts:
            print("[!] 请输入序号，或 u/d/q。")
            continue
        # 先校验所有序号合法，避免部分生效
        bad = [p for p in parts if not p.isdigit() or not (1 <= int(p) <= len(pool))]
        if bad:
            print(f"[!] 无效序号：{'、'.join(bad)}")
            continue
        # 逐个切换选中状态
        for p in parts:
            rid = pool[int(p) - 1]["role_id"]
            if rid in selected:
                selected.remove(rid)                   # 再输一次 = 取消
                print(f"[-] 已取消：{id_to_name[rid]}")
            else:
                if max_count is not None and len(selected) >= max_count:
                    print(f"[!] 最多选 {max_count} 个，无法再添加。")
                    break
                selected.append(rid)
                print(f"[+] 已选中：{id_to_name[rid]}")


def choose_roles(existing: dict = None) -> tuple:
    """终端交互：先选成员（≥2 名），再选审核员（1 名）。
    角色直接从 ROLE_REGISTRY 读取，无需硬编码名字——
    区分依据：allowed_tools == "*" 的是审核员，其余是成员。

    参数：
        existing - 可选，形如 {"members": [...], "arbiters": [...]}。
                   "继续已有对话"时传入该对话历史用过的角色，做预选。
    返回 (member_role_ids, arbiter_role_id)。
    """
    existing = existing or {}
    members = [r for r in ROLE_REGISTRY if r["allowed_tools"] != "*"]
    arbiters = [r for r in ROLE_REGISTRY if r["allowed_tools"] == "*"]

    member_ids = _pick_roles(members, "第 1 步：选择成员角色（至少 2 名，可多选）",
                             min_count=2, preselect=existing.get("members"))
    print(f"\n成员已确定：{'、'.join(member_ids)}")
    arbiter_ids = _pick_roles(arbiters, "第 2 步：选择审核员角色（选 1 名）",
                              min_count=1, max_count=1, preselect=existing.get("arbiters"))
    print(f"审核员已确定：{arbiter_ids[0]}")
    return member_ids, arbiter_ids[0]


if __name__ == '__main__':
    # ---------- 第 0 步：选择对话（新建 / 继续已有） ----------
    # 先定"进哪段对话"，再定"由哪些角色来答"——因为旧对话的记忆是按它当初
    # 用过的角色存的，先选角色再读旧对话会错位。
    # （choose_conversation 内部会先 init_db，确保表已建好）
    conv_id, is_new = choose_conversation()
    database.set_current_conversation(conv_id)   # 设为"当前对话"（ContextVar）

    # ---------- 第 1 步：选择角色（至少 2 名成员 + 1 名审核员） ----------
    # 从角色池中选，详见 choose_roles。角色池定义在 agents/roles_config.py。
    # 若是"继续已有对话"，把该对话历史用过的角色取出做预选，方便沿用。
    existing_roles = None if is_new else database.get_roles_used(conv_id)
    member_ids, arbiter_id = choose_roles(existing_roles)

    # ---------- 第 2 步：按选定的角色创建 Agent 实例 ----------
    member_agents = [create_agent(rid) for rid in member_ids]
    arbiter_agent = create_agent(arbiter_id)

    # ---------- 第 3 步：若继续已有对话，先恢复所选角色的历史记忆 ----------
    if not is_new:
        restore_memory(conv_id, member_agents, arbiter_agent)

    # ---------- 第 4 步：主循环，一问一答，直到用户输入空行退出 ----------
    while True:
        # 每轮开始前，按滑动窗口裁剪各 Agent 的消息历史（默认保留最近3轮）
        for agent in member_agents + [arbiter_agent]:
            agent.set_messages_windows()

        question_in = input("请输入(空值回车以结束对话):")
        if question_in == '':
            break
        # 重置时间缓存，保证本轮取到的时间是最新的
        Reset_Cache()

        # 并发作答 + 仲裁
        result = parallel_solve(question_in, member_agents, arbiter_agent)

        # 打印各成员思考轨迹（调试/观察用）
        for name, traj in result["trajectories"].items():
            print("#" * 50)
            print(f"{name}思考轨迹:")
            for step in traj:
                print(step["content"])

        # 打印线程池上限与审核员裁决
        print("#" * 50)
        print(f"线程池上限: {result['max_workers']} (成员数量: {len(member_agents)})")
        print(result["review"])

        # 落库：本轮问答写入数据库
        turn_id = persist_turn(conv_id, question_in, result, member_agents, arbiter_agent)
        print(f"[已保存] conv_id={conv_id}, turn_id={turn_id}")

        # 若刚满 5 轮，触发各角色的大总结
        seq = database.get_turn_seq(turn_id)
        maybe_big_summary(conv_id, seq, member_agents, arbiter_agent)

    print("对话已结束！")
