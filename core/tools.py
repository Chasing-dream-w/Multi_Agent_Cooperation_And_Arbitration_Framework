import datetime
import math
from ddgs import DDGS
from deep_translator import GoogleTranslator

_CACHED_TIME = None

# 获取日期
def Get_Current_Date():
    global _CACHED_TIME
    if _CACHED_TIME is None:
        _CACHED_TIME = datetime.datetime.now().strftime("%Y年%m月%d日 %H:%M:%S")
    return _CACHED_TIME

def Reset_Cache():
    global _CACHED_TIME
    _CACHED_TIME = None

#  简单计算器
def Calculator(expression: str) -> str:
    """基本的数学运算"""
    allowed_names = {k: v for k, v in math.__dict__.items() if not k.startswith("__")}
    allowed_names.update({"abs": abs, "round": round})
    try:
        result = eval(expression, {"__builtins__": {}}, allowed_names)
        return f"计算结果:{result}"
    except Exception as e:
        return f"计算错误:{str(e)}"

# 日历计算，基准日期的多少天后的日期
def Date_Calculator(days: int, base_date: str = None) -> str:
    """计算指定天数前后的日期"""
    try:
        if base_date:
            # 解析传入的日期字符串
            start = datetime.datetime.strptime(base_date, "%Y-%m-%d")
        else:
            # 使用当前日期
            start = datetime.datetime.now()
        # 计算目标日期
        target = start + datetime.timedelta(days=days)
        return f"{days}天后的日期是: {target.strftime('%Y年%m月%d日')}"
    except Exception as e:
        return f"日期计算错误: {str(e)}"

# 简单翻译
def Translate_Text(text: str, target_lang: str = "zh-CN") -> str:
    try:
        translator = GoogleTranslator(source='auto', target= target_lang)
        translated_text = translator.translate(text)
        return f"翻译结果({target_lang}):{translated_text}"
    except Exception as e:
        return f"翻译失败{str(e)}"

# 实现外部信息检索
def Web_Search(query: str, max_result: int = 3) -> str:
    try:
        with DDGS() as ddgs:
            results = []
            for r in ddgs.text(query, max_results=max_result):
                results.append(f"标题:{r['title']}\n 摘要:{r['body']}\n 链接:{r['href']}")
            if len(results) == 0:
                return "未搜索到相关信息"
            return "\n---\n".join(results)
    except Exception as e:
        return f"搜索失败: {str(e)}。提示：请检查网络连接。"

def Get_History_Memory():
    # 从本地数据库中获取历史记忆
    pass

def Rag():
    # 从文本嵌入的向量数据库中拿知识
    pass

def Weather_search():
    # 天气查询
    pass

def File_Read():
    # 文件读取
    pass

def Self_Summary():
    # 对话历史自我总结
    pass

def Code_Sandbox():
    # 代码沙盒
    pass

# 函数哈希
TOOL_MAP = {
    "get_current_time": Get_Current_Date,
    "calculator": Calculator,
    "date_calculator": Date_Calculator,
    "translate_text": Translate_Text,
    "web_search": Web_Search,
}

# 函数功能
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前日期和时间",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "计算数学表达式",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "数学表达式字符串"}
                },
                "required": ["expression"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "date_calculator",
            "description": "计算多少天后的日期",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "天数（正数代表未来，负数代表过去）"},
                    "base_date": {"type": "string", "description": "基准日期，格式YYYY-MM-DD，默认今天"}
                },
                "required": ["days"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索实时信息",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "translate_text",
            "description": "翻译文本到指定语言",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "待翻译文本"},
                    "target_lang": {"type": "string", "description": "目标语言代码，如 zh-CN, en, ja"}
                },
                "required": ["text", "target_lang"]
            }
        }
    }
]

if __name__ == "__main__":
    ans = Web_Search("美国2025年GDP为？")
    print(ans)