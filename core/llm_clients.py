"""llm_clients.py —— 大模型客户端（系统与 LLM 对话的唯一出口）

全项目所有"请求模型"的动作都集中在这一个文件，好处是：换模型、调超时、
加重试，都只改这里一处，其他代码不用动。

prepare_message() 是核心：把一段 messages 发给模型，返回模型的回复。
base_agent 的 ReAct 循环就是反复调用它。
"""
import os
from openai import OpenAI
from dotenv import load_dotenv
from core import tools


load_dotenv()                                   # 从 .env 读取配置（如 API Key）
API_KEY = os.getenv('API_KEY')
# 超时与重试（解决"卡死等待"和"偶发失败直接崩"）：
#   timeout=60       单次请求最长 60 秒（默认 600 秒，断网时会卡 10 分钟，太久）
#   max_retries=3    网络错误 / 限流(429) / 服务端错误(5xx) 时自动重试 3 次
client = OpenAI(api_key=API_KEY, base_url="https://api.deepseek.com",
                timeout=60.0, max_retries=3)

# 单次回复的最大输出 token 数。
# 注意：thinking 模式下"思考"和"正文"共享这个上限，所以要留足，否则正文会被截断。
MAX_TOKENS = 20000
MODEL_NAME = "deepseek-flash"


def prepare_message(message, tool=None, reasoning_effort="low"):
    """向模型发一次请求，返回原始 response 对象。

    参数：
        message           - 消息列表（system + 历史问答 + 工具结果）
        tool              - 允许模型调用的工具定义列表；None 表示不带工具（纯对话）
        reasoning_effort  - 思考强度："low"（成员，求快）/"high"（审核员，求深）

    两种情况（带工具 / 不带工具）分别调用，因为 OpenAI 接口对 tools 参数的处理不同。
    """
    if tool:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=message,
            stream=False,                        # 非流式：一次性拿到完整回复
            max_tokens=MAX_TOKENS,
            reasoning_effort=reasoning_effort,
            extra_body={"thinking": {"type": "enabled"}},   # 开启深度思考
            tools= tool,
            tool_choice="auto"                   # 让模型自己决定要不要调工具
        )
    else:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=message,
            stream=False,
            max_tokens=MAX_TOKENS,
            reasoning_effort=reasoning_effort,
            extra_body={"thinking": {"type": "enabled"}},
            tool_choice="auto"
        )
    return response


# ============ 以下为早期测试代码，主流程不使用 ============
# 保留原因：单独运行本文件时（python core/llm_clients.py）可快速验证 API 连通性。
# 生产逻辑请看 base_agent.run()，那才是真正的 ReAct 循环。
messages = [
    {"role": "system", "content": "你是一个专业的开发工程师，能得心应手的处理代码，并且是一个可靠的编程助手"}
]
tool_get_current_time = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前日期和时间",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    }
]

def main():
    """简单的交互式测试：输入问题，演示一次"调工具再作答"的流程。"""
    while True:
        user_content = input("输入: ")
        if user_content.lower() == "exit":  # 退出条件
            break

        messages.append({"role": "user", "content": user_content})
        response = prepare_message(messages, tool_get_current_time)
        assistant_reply = response.choices[0].message

        if assistant_reply.tool_calls:
            for tool_call in assistant_reply.tool_calls:
                if tool_call.function.name == "get_current_time":
                    ans = tools.Get_Current_Date()
                    # 先将对话加入历史
                    messages.append(assistant_reply)
                    # 再将工具调用结果返回模型
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": ans
                        }
                    )
                    # 再次调用模型返回最终结果
                    second_response = client.chat.completions.create(
                        model="deepseek-v4-flash",
                        messages=messages,
                        stream=False,
                    )
                    final_reply = second_response.choices[0].message
                    print(final_reply.content)
                    messages.append({"role": "assistant", "content": final_reply.content})
        else:
            print(assistant_reply.content)
            messages.append({"role": "assistant", "content": assistant_reply.content})


if __name__ == "__main__":
    main()
