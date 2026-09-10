import sys
import os
import json

# 获取当前脚本所在目录的绝对路径
current_dir = os.path.dirname(os.path.abspath(__file__))
# 拼接出目标目录(上一级的 core)
parent_dir = os.path.dirname(current_dir)
sys.path.append(parent_dir)

from core.llm_clients import prepare_message
from core.tools import TOOL_DEFINITIONS, TOOL_MAP
from agents.roles_config import get_role

class BaseAgent:
    def __init__(self, system_prompt: str, allowed_tools=None,
                 role_id: str = "", name: str = ""):
        self.system_prompt = system_prompt
        self.role_id = role_id
        self.name = name
        self.allowed_tools = allowed_tools  # None/"*" = 全量；列表 = 白名单
        self.messages = [{"role": "system", "content": system_prompt}]
        self.trajectory = []
        self.turn_start_indices = []
        self.max_steps = 4
        # 关键：按白名单过滤，模型只能"看到"被允许的工具
        self.tool_definitions = self._filter_tool_definitions(TOOL_DEFINITIONS)


    def _filter_tool_definitions(self, all_defs: list) -> list:
        """按 allowed_tools 过滤工具定义；None 或 "*" 表示全量"""
        if self.allowed_tools in (None, "*"):
            return all_defs
        allow = set(self.allowed_tools)
        return [d for d in all_defs if d["function"]["name"] in allow]


    def run(self, user_input: str, max_steps: int = None) -> str:
        """多步ReAct循环：允许模型多次调用工具后再给出最终答案"""
        if max_steps is None:
            max_steps = self.max_steps

        # 每轮只记录当前轮次的思考轨迹
        self.trajectory = []
        # 追加用户消息
        self.turn_start_indices.append(len(self.messages))
        self.messages.append({"role": "user",
                              "content": user_input
        })

        for _ in range(max_steps):
            response = prepare_message(self.messages, tool=self.tool_definitions or None)
            assistant_msg = response.choices[0].message

            # 模型如返回真实推理内容，记录为本轮思考
            reasoning = getattr(assistant_msg, "reasoning_content", None)
            if reasoning:
                self.trajectory.append({"type": "thought", "content": reasoning})

            # 模型不再调用工具，当前内容即为最终答案
            if not assistant_msg.tool_calls:
                reply = assistant_msg.content or ""
                self.messages.append({"role": "assistant", "content": reply})
                self.trajectory.append({"type": "final_answer", "content": reply})
                return reply

            # 执行模型本次请求的全部工具
            self.messages.append(self._build_tool_call_message(assistant_msg))
            for tool_call in assistant_msg.tool_calls:
                tool_name = tool_call.function.name
                action_args = self._parse_tool_args(tool_call)
                self.trajectory.append({
                    "type": "action",
                    "tool": tool_name,
                    "args": action_args,
                    "content": f"工具调用{tool_name}, 参数{action_args}",
                })

                result = self._execute_tool(tool_call)
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })
                self.trajectory.append({
                    "type": "observation",
                    "tool": tool_name,
                    "result": result,
                    "content": f"工具调用结果{result}",
                })

        # 达到最大步数后，基于已有工具结果强制生成最终答案
        final_response = prepare_message(self.messages, tool=None)
        final_reply = final_response.choices[0].message.content or ""
        self.messages.append({"role": "assistant", "content": final_reply})
        self.trajectory.append({"type": "final_answer", "content": final_reply})
        return final_reply


    def _build_tool_call_message(self, assistant_msg) -> dict:
        """将SDK返回的assistant消息转换为API可接受的普通dict"""
        return {
            "role": assistant_msg.role,
            "content": assistant_msg.content,
            "tool_calls": [
                {
                    "id": tool_call.id,
                    "type": tool_call.type or "function",
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                }
                for tool_call in assistant_msg.tool_calls
            ],
        }


    def _parse_tool_args(self, tool_call) -> dict:
        """解析模型返回的工具参数；失败时保留原始参数以便追踪"""
        raw_arguments = tool_call.function.arguments or ""
        try:
            parsed = json.loads(raw_arguments) if raw_arguments else {}
        except json.JSONDecodeError:
            return {"_raw": raw_arguments, "error": "JSON解析失败"}
        return parsed if isinstance(parsed, dict) else {"_raw": parsed, "error": "参数必须为JSON对象"}


    def _execute_tool(self, tool_call) -> str:
        tool_name = tool_call.function.name
        # 白名单兜底：越权工具直接拒绝，结果不给模型
        if self.allowed_tools not in (None, "*") and tool_name not in self.allowed_tools:
            return f"无权调用工具: {tool_name}"
        raw_arguments = tool_call.function.arguments or ""
        try:
            args = json.loads(raw_arguments) if raw_arguments else {}
        except json.JSONDecodeError:
            return f"工具 {tool_name} 参数JSON解析失败: {raw_arguments}"
        if not isinstance(args, dict):
            return f"工具 {tool_name} 参数必须为JSON对象"

        if tool_name not in TOOL_MAP:
            return f"未知工具: {tool_name}"
        try:
            result = TOOL_MAP[tool_name](**args)
        except TypeError as exc:
            return f"工具 {tool_name} 参数错误: {exc}"
        except Exception as exc:
            return f"工具 {tool_name} 执行失败: {exc}"
        return str(result)


    def evaluate(self, user_input: str, candidates: dict, trajectories: dict = None) -> str:
        """对比多个Agent答案。
        candidate示例:{"数学家": "47", "程序员": "47"}
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
                    elif step['type'] == "action":
                        comparison_prompt += f"调用工具:{step['tool']}参数：{step['args']}\n"
                    elif step['type'] == "observation":
                        comparison_prompt += f"观察到外部环境:{step['result']}\n"
        comparison_prompt += "\n判断哪个答案更准确。如果不一致，请尝试给出最终裁决，或生成最终答案。"
        return self.run(comparison_prompt)


    def get_trajectory(self):
        """获取当前轮次思考轨迹"""
        return self.trajectory

    def reset(self):
        """重置对话历史"""
        self.messages = [{"role": "system", "content": self.system_prompt}]
        self.trajectory = []
        self.turn_start_indices = []

    def set_messages_windows(self, window_size: int = 3):
        """按完整轮次滑动历史消息,轨迹由每次run()单独维护"""
        if len(self.turn_start_indices) <= window_size:
            return

        starts = self.turn_start_indices[-window_size:]
        first_index = starts[0]

        system = [m for m in self.messages if m.get("role") == "system"]
        history = self.messages[first_index:]

        self.messages = system + history
        self.turn_start_indices = [len(system) + (s - first_index) for s in starts]


def create_agent(role_id: str) -> BaseAgent:
    """从 roles_config 角色注册表创建 Agent 实例"""
    cfg = get_role(role_id)
    return BaseAgent(
        system_prompt=cfg["system_prompt"],
        allowed_tools=cfg["allowed_tools"],
        role_id=cfg["role_id"],
        name=cfg["name"],
    )
