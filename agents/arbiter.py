from .base_agent import BaseAgent
# 审核员
class Arbiter(BaseAgent):
    def __init__(self):
        super().__init__(
            system_prompt="""你是一个严格的评审专家，负责审核其他Agent给出的答案。
            你需要检查：
            1. 推理过程是否严谨
            2. 是否存在明显的逻辑漏洞
            3. 是否有更优的解法
            如果发现问题，需要改进则提出建议。
            如果答案合理，则综合后输出答案。
            思考工程中最多可以调用4次工具，即最多获取四次外部结果。
            若这些工具调用均不能完成任务，如实回复即可.
            """
        )

    def evaluate(self, user_input: str, candidates: dict, trajectories: dict = None) -> str:
        """
        对比多个Agent答案。
        candidate示例:{"math-agent": " 47", "programmer": "47"}
        """
        # 构造对比提示词
        comparison_prompt = ("请对以下针对同一问题的不同答案进行评审:\n"
                             f"问题: {user_input}\n")
        for name, ans in candidates.items():
            comparison_prompt += f"\n -{name}的答案: {ans}\n"
        if trajectories:
            comparison_prompt += "\n 以下是各Agent的思考轨迹供你参考:\n"
            for name, traj in trajectories.items():
                comparison_prompt += f"[{name}的推理过程]\n"
                for step in traj:
                    if step["type"] == "thought":
                        comparison_prompt += f"思考:{step['content']}\n"
                        # print("---------------------")
                    elif step['type'] == "action":
                        comparison_prompt += f"调用工具:{step['tool']}参数：{step['args']}\n"
                        # print("---------------------")
                    elif step['type'] == "observation": # 实际上是工具调用结果
                        comparison_prompt += f"观察到外部环境:{step['result']}\n"
                        # print("---------------------")
        comparison_prompt += "\n判断哪个答案更准确。如果不一致，请尝试给出最终裁决，或生成最终答案。"

        return self.run(comparison_prompt)
