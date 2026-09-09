from core.tools import Reset_Cache
from concurrent.futures import ThreadPoolExecutor
from agents.base_agent import create_agent


if __name__ == '__main__':

    math_agent = create_agent("mathematician")
    prog_agent = create_agent("programmer")
    arbiter_agent = create_agent("referee")

    ans_math = ""
    ans_prog = ""
    while True:
        # 设置对话历史上限，默认为4轮
        math_agent.set_messages_windows()
        prog_agent.set_messages_windows()
        arbiter_agent.set_messages_windows()
        
        question_in = input("请输入(空值回车以结束对话):")
        if question_in == '':
            break
        # 重置时间参数
        Reset_Cache()
        # 并发调用
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_math = executor.submit(math_agent.run, question_in)
            future_prog = executor.submit(prog_agent.run, question_in)
            ans_math = future_math.result()
            ans_prog = future_prog.result()
        # 获取思考轨迹
        traj_math = math_agent.get_trajectory()
        traj_prog = prog_agent.get_trajectory()

        # # 打印思考轨迹
        print("#" * 50)
        print("数学家思考轨迹:")
        for step in traj_math:
            print(f"{step['content']}")
        print("#" * 50)
        print("程序员思考轨迹:")
        for step in traj_prog:
            print(f"{step['content']}")
        print("#" * 50)

        #将思考轨迹加入判别
        review = arbiter_agent.evaluate(
            user_input= question_in,
            candidates=
            {
                "数学家": ans_math,
                "程序员": ans_prog
            },
            trajectories=
            {
                "数学家": traj_math,
                "程序员": traj_prog
            }
        )
        print(review)

    print("对话已结束！")