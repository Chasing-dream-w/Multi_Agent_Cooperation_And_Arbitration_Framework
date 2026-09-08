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

class BaseAgent:
    def __init__(self, system_prompt: str):
        self.system_prompt = system_prompt
        self.messages = [{"role": "system", "content": system_prompt}]
        self.trajectory = []
        self.turn_start_indices = []
        self.max_steps = 4

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
            response = prepare_message(self.messages, tool=TOOL_DEFINITIONS)
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
        """安全执行工具调用，所有失败都以文本结果返回给模型"""
        tool_name = tool_call.function.name
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
