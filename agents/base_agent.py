"""base_agent.py —— Agent 基类（所有角色的共同"大脑"）

一个 BaseAgent 实例 = 一个角色（数学家/裁判/……）。它的职责是：

    run(问题) —— 跑一轮 "ReAct 循环"（Reason + Act）：
        反复问模型 → 若模型要调用工具就执行、把结果喂回模型 → 直到模型给出最终答案。
    也就是"让模型一边想一边查资料，最后作答"。

角色的差异（人格、权限、思考强度）全部由外部传入，基类本身不写死任何角色 ——
这样加一个角色 = 在 roles_config.py 加一条配置，不用改这个文件。

关键数据成员：
    self.messages     与大模型交互的完整消息列表（system + 多轮 user/assistant/tool）
    self.trajectory   本轮"思考轨迹"（thought/action/observation），供展示与仲裁参考
    self.summary      本轮回答里切出的自我小结（不进 messages，只供落库）
    self.tool_definitions  该角色"能看到"的工具定义（已按白名单过滤）
"""
import sys
import os
import json
import re

# 把项目根目录加入模块搜索路径，保证从任何位置运行都能 import core.*
# （__file__ 是本文件路径，它的上两级就是项目根）
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.append(parent_dir)

from core.llm_clients import prepare_message
from core.tools import TOOL_DEFINITIONS, TOOL_MAP
from agents.roles_config import get_role

class BaseAgent:
    def __init__(self, system_prompt: str, allowed_tools=None,
                 role_id: str = "", name: str = "", reasoning_effort: str = "low"):
        # ---- 角色身份（由 create_agent 从配置注入）----
        self.system_prompt = system_prompt          # 角色人格提示词（决定"它是谁"）
        self.role_id = role_id                      # 角色唯一标识，如 "mathematician"
        self.name = name                            # 中文名，如 "数学家"（用于展示）
        self.allowed_tools = allowed_tools          # None/"*"=全量；列表=白名单
        self.reasoning_effort = reasoning_effort    # 思考强度：成员 low / 审核员 high

        # ---- 运行时状态 ----
        # messages 第一条永远是 system（角色设定），之后是逐轮的 user/assistant/tool
        self.messages = [{"role": "system", "content": system_prompt}]
        self.trajectory = []            # 本轮思考轨迹，每次 run() 重置
        self.turn_start_indices = []     # 记录每一轮消息的起始下标，供滑动窗口裁剪用
        self.max_steps = 4               # ReAct 循环最多跑几步（防止无限调工具）
        self.summary = ""                # 本轮自我小结（从回答的 <summary> 标签切出）

        # 关键：按白名单过滤工具 —— 模型"看不到"无权工具，从源头限制权限
        self.tool_definitions = self._filter_tool_definitions(TOOL_DEFINITIONS)


    def _filter_tool_definitions(self, all_defs: list) -> list:
        """按 allowed_tools 白名单过滤工具定义。
        返回该角色"能看到"的工具子集；None 或 "*" 表示不限制（全量）。"""
        if self.allowed_tools in (None, "*"):
            return all_defs
        allow = set(self.allowed_tools)
        return [d for d in all_defs if d["function"]["name"] in allow]


    @staticmethod
    def _split_summary(text: str) -> tuple:
        """从模型回答里切出 <summary>...</summary> 小结。
        返回 (干净答案, 小结)。没有标签时小结为空串、答案原样返回。
        为什么要切：小结只给系统记忆用，不能让用户看到，也不能留在答案里。"""
        if text and "<summary>" in text and "</summary>" in text:
            match = re.search(r"<summary>(.*?)</summary>", text, flags=re.DOTALL)
            if match:
                summary = match.group(1).strip()[:100]   # 截断到 100 字
                clean = (text[:match.start()] + text[match.end():]).strip()
                # 兜底：若切完答案为空（模型只输出了小结），让答案回落为小结
                return (clean if clean else summary), summary
        return (text or "").strip(), ""



    def run(self, user_input: str, max_steps: int = None) -> str:
        """ReAct 循环：让模型"边想边调工具"，最后给出答案。返回干净答案文本。

        每一步做什么：
            ① 把当前 messages 发给模型
            ② 如果模型要求调用工具 → 执行工具、把结果作为一条 tool 消息喂回去，继续循环
            ③ 如果模型不再要求工具 → 它给的内容就是最终答案，切出小结后返回

        最多循环 max_steps 次（默认4）：若一直调工具停不下来，最后强制让模型
        基于已有结果作答，避免死循环。

        异常兜底：任何异常（API失败/超时等）都不上抛，返回「[角色 作答失败]」占位文本，
        保证一个角色出问题不会拖垮整轮对话。
        """
        if max_steps is None:
            max_steps = self.max_steps

        # 本轮开始：清空轨迹与小结（它们只描述"本轮"）
        self.trajectory = []
        self.summary = ""
        # 记录本轮消息的起始下标（供 set_messages_windows 按轮裁剪），再追加用户消息
        self.turn_start_indices.append(len(self.messages))
        self.messages.append({"role": "user",
                              "content": user_input
        })

        try:
            for _ in range(max_steps):
                # ① 请求模型：带上"该角色可见的工具"（tool_definitions）和思考强度
                response = prepare_message(self.messages, tool=self.tool_definitions or None,
                                           reasoning_effort=self.reasoning_effort)
                assistant_msg = response.choices[0].message

                # 模型的"思考过程"（深度思考模式下模型会额外返回 reasoning_content）
                reasoning = getattr(assistant_msg, "reasoning_content", None)
                if reasoning:
                    self.trajectory.append({"type": "thought", "content": reasoning})

                # ③ 模型没有要求调用工具 → 这就是最终答案，收工
                if not assistant_msg.tool_calls:
                    raw = assistant_msg.content or ""
                    reply, self.summary = self._split_summary(raw)   # 切出小结
                    # 历史里保留"含 <summary> 标签的原始输出"，而不是切干净的答案：
                    # 否则模型下一轮会模仿自己"不带标签"的历史，从第3轮起就不再输出小结了。
                    # "对用户隐藏小结"靠展示层/落库层实现，不靠污染模型的历史。
                    self.messages.append({"role": "assistant", "content": raw.strip() or reply})
                    self.trajectory.append({"type": "final_answer", "content": reply})
                    return reply

                # ② 模型要求调工具：先把"模型要调工具"这条 assistant 消息记进历史
                self.messages.append(self._build_tool_call_message(assistant_msg))
                for tool_call in assistant_msg.tool_calls:
                    tool_name = tool_call.function.name
                    action_args = self._parse_tool_args(tool_call)
                    # 记录"动作"到轨迹
                    self.trajectory.append({
                        "type": "action",
                        "tool": tool_name,
                        "args": action_args,
                        "content": f"工具调用{tool_name}, 参数{action_args}",
                    })

                    # 真正执行工具（内部会做权限校验），再把结果作为 tool 消息喂回模型
                    result = self._execute_tool(tool_call)
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    })
                    # 记录"观察结果"到轨迹
                    self.trajectory.append({
                        "type": "observation",
                        "tool": tool_name,
                        "result": result,
                        "content": f"工具调用结果{result}",
                    })

            # 循环用尽（模型一直在调工具）：关掉工具、强制让它基于已有结果给最终答案
            final_response = prepare_message(self.messages, tool=None,
                                             reasoning_effort=self.reasoning_effort)
            raw = final_response.choices[0].message.content or ""
            final_reply, self.summary = self._split_summary(raw)
            # 同上：历史保留含标签的原始输出
            self.messages.append({"role": "assistant", "content": raw.strip() or final_reply})
            self.trajectory.append({"type": "final_answer", "content": final_reply})
            return final_reply

        except Exception as e:
            # 任何异常都不抛出，而是降级为一句失败文本
            fail_msg = f"[{self.name} 作答失败：{type(e).__name__}: {e}]"
            # 补一条 assistant 消息，保持"user 必有 assistant 回复"的结构对称，
            # 否则后续轮次的消息序列会不完整、影响模型理解
            self.messages.append({"role": "assistant", "content": fail_msg})
            self.trajectory.append({"type": "final_answer", "content": fail_msg})
            return fail_msg


    def _build_tool_call_message(self, assistant_msg) -> dict:
        """把 SDK 返回的 assistant 消息（含 tool_calls）转成普通 dict。
        SDK 返回的是对象，不能直接放进 messages 列表，需要手动拆成
        {'role','content','tool_calls'} 结构，才能作为历史继续发回模型。"""
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
        """解析模型给出的工具参数（是一段 JSON 字符串）。
        解析失败也不崩，包成带错误标记的 dict 返回，便于追踪问题。"""
        raw_arguments = tool_call.function.arguments or ""
        try:
            parsed = json.loads(raw_arguments) if raw_arguments else {}
        except json.JSONDecodeError:
            return {"_raw": raw_arguments, "error": "JSON解析失败"}
        return parsed if isinstance(parsed, dict) else {"_raw": parsed, "error": "参数必须为JSON对象"}


    def _execute_tool(self, tool_call) -> str:
        """执行一个工具调用，返回文本结果。所有失败都转成文本，不抛异常。
        两道防线：
            ① 权限：不在白名单的工具直接拒绝（即使模型硬调也拦下）
            ② 容错：参数错误/工具不存在/执行异常，都返回可读的错误文本给模型
        """
        tool_name = tool_call.function.name
        # ① 权限兜底：越权工具直接拒绝，结果不给模型
        if self.allowed_tools not in (None, "*") and tool_name not in self.allowed_tools:
            return f"无权调用工具: {tool_name}"
        # 解析参数
        raw_arguments = tool_call.function.arguments or ""
        try:
            args = json.loads(raw_arguments) if raw_arguments else {}
        except json.JSONDecodeError:
            return f"工具 {tool_name} 参数JSON解析失败: {raw_arguments}"
        if not isinstance(args, dict):
            return f"工具 {tool_name} 参数必须为JSON对象"

        # ② 从注册表找到函数并执行
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
        """审核员专用：对比多个成员的答案，产出裁决文本。

        做法是"拼一段评审提示词，然后走一次普通 run()"——审核员本质上
        也是在回答一个问题（问题是"这些答案谁对"），只是提示词由系统拼好。
        参数：
            user_input   - 原始问题
            candidates   - {成员名: 答案}
            trajectories - {成员名: 思考轨迹}（可选，供审核员参考推理过程）
        """
        # 拼接"问题 + 各成员答案"的评审提示词
        comparison_prompt = ("请对以下针对同一问题的不同答案进行评审:\n"
                             f"问题: {user_input}\n")
        for name, ans in candidates.items():
            comparison_prompt += f"\n -{name}的答案: {ans}\n"
        # 附上各成员的思考轨迹，帮助审核员判断"答案是怎么来的"
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
        """返回本轮思考轨迹（thought / action / observation 的列表）。"""
        return self.trajectory

    def load_memory(self, memory_text: str) -> None:
        """把一段长期记忆注入到上下文里（恢复历史对话时用）。
        用 system 角色注入是有意的 —— set_messages_windows() 裁剪时只保留
        system 消息，所以记忆不会被滑动窗口裁掉。空文本则不注入。"""
        if not memory_text:
            return
        self.messages.append({
            "role": "system",
            "content": f"以下是你在这段对话中的长期记忆，请在后续回答中作为背景参考：\n{memory_text}",
        })


    def reset(self):
        """重置回初始状态：清空所有对话历史与轨迹（角色设定保留）。"""
        self.messages = [{"role": "system", "content": self.system_prompt}]
        self.trajectory = []
        self.turn_start_indices = []
        self.summary = ""

    def set_messages_windows(self, window_size: int = 3):
        """滑动窗口：只保留最近 window_size 轮的消息，防止上下文无限增长。

        要点：
          - 按"轮次"裁剪，不是按消息条数（一轮可能包含 user/assistant/tool 多条）
          - system 消息全部保留（角色设定 + 注入的长期记忆都在这里，绝不能裁）
          - turn_start_indices 记录每轮起点，裁剪后要同步修正这些下标
        """
        # 轮数没超上限，无需裁剪
        if len(self.turn_start_indices) <= window_size:
            return

        starts = self.turn_start_indices[-window_size:]   # 最近 N 轮的起点
        first_index = starts[0]                            # 要保留的第一条消息下标

        system = [m for m in self.messages if m.get("role") == "system"]   # 保留全部 system
        history = self.messages[first_index:]              # 保留最近 N 轮

        self.messages = system + history
        # 因为前面插入了若干 system 消息，历史消息的下标整体后移，需重算起点
        self.turn_start_indices = [len(system) + (s - first_index) for s in starts]


def create_agent(role_id: str) -> BaseAgent:
    """工厂函数：按 role_id 从角色配置创建 Agent 实例。
    角色的一切差异（人格提示词、工具权限、思考强度）都由配置决定，
    所以"新增一个角色"只需在 roles_config.py 加一条，不用改这里的代码。"""
    cfg = get_role(role_id)
    # 审核员池（allowed_tools == "*"）用高强度思考，成员用低强度：
    # 成员重在快速给出视角，审核员重在深度评审。
    is_arbiter = cfg["allowed_tools"] == "*"
    return BaseAgent(
        system_prompt=cfg["system_prompt"],
        allowed_tools=cfg["allowed_tools"],
        role_id=cfg["role_id"],
        name=cfg["name"],
        reasoning_effort="high" if is_arbiter else "low",
    )
