from concurrent.futures import ThreadPoolExecutor
from core.tools import Reset_Cache
from core import database
from agents.base_agent import create_agent


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
    # 按成员名查到对应的 role_id，组装 answers 表所需结构
    name_to_role = {agent.name: agent.role_id for agent in member_agents}
    members = [
        {
            "role_id": name_to_role[name],
            "content": ans,
            "trajectory": result["trajectories"].get(name, []),
        }
        for name, ans in result["answers"].items()
    ]
    return database.save_turn(
        conv_id=conv_id,
        question=question,
        members=members,
        verdict=result["review"],
        arbiter_role_id=arbiter_agent.role_id,
    )


if __name__ == '__main__':
    # ---------- 测试阶段：固定角色，不实现用户选择交互 ----------
    # 上线阶段：由用户指定成员角色个数（至少1个）与1个审核员角色，
    # 例如 MEMBER_IDS = ["mathematician", "programmer", "writer", ...]
    # 注意 role_id 拼写见 agents/roles_config.py（如 big_data_architect）
    MEMBER_IDS = ["mathematician", "programmer"]      # 成员角色（测试用，可增删）
    ARBITER_ID = "referee"                            # 审核员角色（指定1个）

    member_agents = [create_agent(rid) for rid in MEMBER_IDS]
    arbiter_agent = create_agent(ARBITER_ID)

    # 初始化数据库，新建一段对话并设为当前对话
    database.init_db()
    conv_id = database.create_conversation("终端测试对话")
    database.set_current_conversation(conv_id)
    print(f"已新建对话 conv_id = {conv_id}")

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

    print("对话已结束！")
