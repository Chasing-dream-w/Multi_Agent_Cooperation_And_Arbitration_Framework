import os
from openai import OpenAI
from dotenv import load_dotenv
from core import tools


load_dotenv()
API_KEY = os.getenv('DEEPSEEK_API_KEY')
client = OpenAI(api_key=API_KEY, base_url="https://api.deepseek.com")


# API调用函数，核心内容
def prepare_message(message, tool=None):
    if tool:
        response = client.chat.completions.create(
            model="deepseek-flash",
            messages=message,
            stream=False,
            reasoning_effort="high",
            extra_body={"thinking": {"type": "enabled"}},
            tools= tool,
            tool_choice="auto"
        )
    else:
        response = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=message,
            stream=False,
            reasoning_effort="high",
            extra_body={"thinking": {"type": "enabled"}},
            tool_choice="auto"
        )
    return response


# 本代码测试，无需关心
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
