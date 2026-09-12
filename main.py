from concurrent.futures import ThreadPoolExecutor
from core.tools import Reset_Cache, Self_Summary
from core import database
from agents.base_agent import create_agent

# 每积累多少轮，触发一次大总结
BIG_SUMMARY_EVERY = 5


def parallel_solve(question: str, member_agents: list, arbiter_agent) -> dict:
    """前端/后端统一接口：动态多线程并发成员作答，再由审核员仲裁。

    参数：
        question      - 用户问题
        member_agents - 已实例化的成员Agent列表（上线阶段由用户指定成员角色，至少1个）
        arbiter_agent - 审核员Agent（用户指定1个审核员角色）
    返回：
        结构化结果 dict，供前端展示：
        {
            "max_workers": 实际线程池上限,
            "answers": {成员名: 答案},
            "trajectories": {成员名: 思考轨迹},
            "review": 审核员裁决,
        }
    """
    # 动态多线程分配：线程上限 = 成员角色数量（至少1个线程）
    max_workers = max(1, len(member_agents))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {agent.name: executor.submit(agent.run, question)
                   for agent in member_agents}
        answers = {name: future.result() for name, future in futures.items()}
        trajectories = {agent.name: agent.get_trajectory()
                        for agent in member_agents}

    review = arbiter_agent.evaluate(
        user_input=question,
        candidates=answers,
        trajectories=trajectories,
    )
    return {
        "max_workers": max_workers,
        "answers": answers,
        "trajectories": trajectories,
        "review": review,
    }


def persist_turn(conv_id: int, question: str, result: dict,
                 member_agents: list, arbiter_agent) -> int:
    """写入接口（编排侧）：把一轮完整问答落库，返回 turn_id。
    注意：写入是系统的编排动作，不是给LLM调用的工具——读记忆在 tools.Get_History_Memory。"""
    # 按成员名查到对应的 role_id，组装 answers 表所需结构（含各成员本轮小结）
    name_to_role = {agent.name: agent.role_id for agent in member_agents}
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
    """每满 BIG_SUMMARY_EVERY 轮触发一次：为每个角色生成大总结并写回库。
    每个角色各自压缩自己的「上次大总结 + 最近5条小总结」，互相独立。"""
    if seq % BIG_SUMMARY_EVERY != 0:
        return

    # 成员（总结存 answers 表）+ 裁判（总结存 turns 表）
    targets = [(a.role_id, a.name, False) for a in member_agents]
    targets.append((arbiter_agent.role_id, arbiter_agent.name, True))

    for role_id, name, is_arbiter in targets:
        previous_big, recent_smalls = database.get_summary_context(
            conv_id, role_id, seq, is_arbiter=is_arbiter)
        big = Self_Summary(name, previous_big, recent_smalls)
        if big:
            database.write_big_summary(conv_id, role_id, seq, big,
                                       is_arbiter=is_arbiter)
    print(f"[大总结] 第 {seq} 轮已为各角色生成大总结")


def choose_conversation() -> tuple:
    """终端交互：新建对话 或 选择已有对话。
    返回 (conv_id, is_new)：is_new=True 表示新建，False 表示选中已有对话。"""
    database.init_db()
    while True:
        choice = input("新建对话(N) / 选择已有对话(S)？(N/S，直接回车默认N):").strip().lower()
        if choice in ("", "n", "new", "新建"):
            title = input("请输入新对话的话题/标题:").strip() or "未命名对话"
            conv_id = database.create_conversation(title)
            print(f"已新建对话：conv_id={conv_id}，标题「{title}」")
            return conv_id, True
        if choice in ("s", "select", "选择"):
            convs = database.list_conversations()
            if not convs:
                print("暂无已有对话，转为新建。")
                title = input("请输入新对话的话题/标题:").strip() or "未命名对话"
                conv_id = database.create_conversation(title)
                print(f"已新建对话：conv_id={conv_id}，标题「{title}」")
                return conv_id, True
            print("已有对话列表：")
            for i, c in enumerate(convs, start=1):
                print(f"  {i}. {c['title']}  （{c['turn_count']} 轮，{c['created_at']}）")
            raw = input("请输入要选择的序号（直接回车取消）:").strip()
            if raw == "":
                continue
            if not raw.isdigit() or not (1 <= int(raw) <= len(convs)):
                print("序号无效，请重新选择。")
                continue
            sel = convs[int(raw) - 1]
            print(f"已选择对话：conv_id={sel['conv_id']}，标题「{sel['title']}」")
            return sel["conv_id"], False
        print("输入无效，请输入 N 或 S。")


def restore_memory(conv_id: int, member_agents: list, arbiter_agent) -> None:
    """恢复已有对话：把各角色的长期记忆注入各自 Agent 的上下文。
    每个角色读自己的总结线；成员存 answers 表、裁判存 turns 表。"""
    restored = 0
    for agent in member_agents:
        text = database.get_role_memory_text(conv_id, agent.role_id, is_arbiter=False)
        agent.load_memory(text)
        if text:
            restored += 1
    arb_text = database.get_role_memory_text(conv_id, arbiter_agent.role_id, is_arbiter=True)
    arbiter_agent.load_memory(arb_text)
    if arb_text:
        restored += 1
    print(f"[记忆恢复] 已为 {restored} 个角色注入历史记忆")


if __name__ == '__main__':
    # ---------- 测试阶段：固定角色，不实现用户选择交互 ----------
    # 上线阶段：由用户指定成员角色个数（至少1个）与1个审核员角色，
    # 例如 MEMBER_IDS = ["mathematician", "programmer", "writer", ...]
    # 注意 role_id 拼写见 agents/roles_config.py（如 big_data_architect）
    MEMBER_IDS = ["mathematician", "programmer"]      # 成员角色（测试用，可增删）
    ARBITER_ID = "referee"                            # 审核员角色（指定1个）

    member_agents = [create_agent(rid) for rid in MEMBER_IDS]
    arbiter_agent = create_agent(ARBITER_ID)

    # 运行后先选择：新建对话 或 选择已有对话
    conv_id, is_new = choose_conversation()
    database.set_current_conversation(conv_id)

    # 分支：选择的是已有对话 → 先恢复各角色历史记忆，再进入交互
    if not is_new:
        restore_memory(conv_id, member_agents, arbiter_agent)

    while True:
        # 滑动窗口限制对话历史上限（默认最近3轮）
        for agent in member_agents + [arbiter_agent]:
            agent.set_messages_windows()

        question_in = input("请输入(空值回车以结束对话):")
        if question_in == '':
            break
        # 重置时间缓存，确保本轮时间新鲜
        Reset_Cache()

        result = parallel_solve(question_in, member_agents, arbiter_agent)

        # 打印各成员思考轨迹
        for name, traj in result["trajectories"].items():
            print("#" * 50)
            print(f"{name}思考轨迹:")
            for step in traj:
                print(step["content"])

        # 打印线程池上限与审核员裁决
        print("#" * 50)
        print(f"线程池上限: {result['max_workers']} (成员数量: {len(MEMBER_IDS)})")
        print(result["review"])

        # 写入接口：本轮问答落库
        turn_id = persist_turn(conv_id, question_in, result, member_agents, arbiter_agent)
        print(f"[已保存] conv_id={conv_id}, turn_id={turn_id}")

        # 每满5轮触发一次大总结
        seq = database.get_turn_seq(turn_id)
        maybe_big_summary(conv_id, seq, member_agents, arbiter_agent)

    print("对话已结束！")
