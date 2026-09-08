from agents.mathematician import Mathematician
from agents.programmer import Programmer
from agents.arbiter import Arbiter
from core.tools import Reset_Cache
from agents.roles_config import get_role

if __name__ == '__main__':

    # 成员角色池
    math_config = get_role("mathematician")
    prog_config = get_role("programmer")
    data_architect_config = get_role("big_data_architect")
    writer_config = get_role("writer")
    composer_config = get_role("composer")
    english_teacher_config = get_role("english_teacher")
    philosopher_config = get_role("philosopher")
    psychologist_config = get_role("psychologist")
    hardware_engineer_config = get_role("hardware_engineer")
    video_editor_config = get_role("video_editor")
    secretary_config = get_role("secretary")
    influencer_config = get_role("influencer")
    lawyer_config = get_role("lawyer")
    # 审核员角色池
    referee_config = get_role("referee")
    reviewer_config = get_role("reviewer")
    justice_config = get_role("justice")
    grader_config = get_role("grader")
    auditor_config = get_role("auditor")
    editor_config = get_role("editor")




    math_agent = Mathematician()
    prog_agent = Programmer()
    arbiter_agent = Arbiter()
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
        # 获取当前轮次答案
        ans_math = math_agent.run(question_in)
        ans_prog = prog_agent.run(question_in)
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