from core.tools import Reset_Cache
from concurrent.futures import ThreadPoolExecutor
from agents.base_agent import create_agent


if __name__ == '__main__':

    """成员角色池"""
    math_agent = create_agent("mathematician")     # 数学家
    prog_agent = create_agent("programmer")        # 程序员
    data_arch_agent = create_agent("big_data_acrhitect")       # 大数据架构师
    writer_agent = create_agent("writer")          # 文学家
    composer_agent = create_agent("composer")      # 作曲家
    english_teacher_agent = create_agent("english_teacher")    # 英语老师
    philosophy_agent = create_agent("philosopher")     # 哲学家
    psychologist_agent = create_agent("psychologist")      # 心理学家
    hardware_engineer_agent = create_agent("hardware_engineer")      # 硬件工程师
    video_editor_agent = create_agent("video_editor")    # 剪辑师
    secretary_agent = create_agent("secretary")          # 秘书
    lawyer_agent = create_agent("lawyer")          # 律师
    influencer_agent = create_agent("influencer")      # 网红

    """审核员角色池"""
    arbiter_agent = create_agent("referee")        # 裁判
    reviewer_agent = create_agent("reviewer")      # 审稿人
    editor_agent = create_agent("editor")          # 编辑
    grader_agent = create_agent("grader")          # 阅卷老师
    justice_agent = create_agent("justice")        # 大法官

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