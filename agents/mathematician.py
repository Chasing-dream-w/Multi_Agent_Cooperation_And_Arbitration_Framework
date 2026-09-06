from .base_agent import BaseAgent
# 数学家
class Mathematician(BaseAgent):
    def __init__(self):
        super().__init__(
            system_prompt="你是一个严谨的数学家，擅长数学运算和逻辑推理。"
                          "对于任何数学问题，你必须一步一步推导，不能跳步。"
                          "如果涉及数值计算，调用工具来获取准确结果"
        )