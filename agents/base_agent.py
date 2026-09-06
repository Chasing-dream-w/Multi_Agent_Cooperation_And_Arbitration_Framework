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

    def run(self, user_input: str) -> str:
        """单次ReAct循环"""
        # 每轮只记录当前轮次的思考轨迹
        self.trajectory = []
        # 追加用户消息
        self.turn_start_indices.append(len(self.messages))
        self.messages.append({"role": "user",
                              "content": user_input
        })
        # 记录当前轮次的Thought
        self.trajectory.append({"type": "thought",
                                "content": f"用户问:{user_input}"
        })
        # 调用LLM
        response = prepare_message(self.messages, tool=TOOL_DEFINITIONS)
        assistant_msg = response.choices[0].message

        # 判断是否有工具调用
        if assistant_msg.tool_calls:
            # 执行工具
            self.messages.append({
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
            })
            for tool_call in assistant_msg.tool_calls:
                tool_name = tool_call.function.name
                try:
                    args = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
                except json.JSONDecodeError:
                    args = {}
                # 将工具调用Action记录到trajectory中
                self.trajectory.append({"type": "action",
                                        "tool": tool_name,
                                        "args": args,
                                        "content": f"工具调用{tool_name}, 参数{args}"
                })

                if tool_name in TOOL_MAP:
                    result = TOOL_MAP[tool_name](**args)

                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result
                    })
                    # 将工具调用结果记录到trajectory
                    self.trajectory.append({"type": "observation",
                                            "tool": tool_name,
                                            "result": result,
                                            "content": f"工具调用结果{result}"
                    })

            # 再次调用LLM获取最终回复
            final_response = prepare_message(self.messages, tool=None)
            final_reply = final_response.choices[0].message.content
            self.messages.append({"role": "assistant", "content": final_reply})
            # 思维链记录最终输出
            self.trajectory.append({"type": "final_answer",
                                    "content":final_reply

            })
            return final_reply
        else:
            # 无工具调用，直接返回
            reply = assistant_msg.content
            self.messages.append({"role": "assistant",
                                  "content": reply
            })
            self.trajectory.append({
                "type": "final_answer",
                "content": reply
            })
            return reply

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
