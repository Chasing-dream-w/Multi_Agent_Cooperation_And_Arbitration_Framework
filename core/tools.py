import datetime
import math
import json
import urllib.request
import urllib.parse
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

def Weather_search(city: str = "") -> str:
    """天气查询：通过城市名获取实时天气与今明两天的预报"""
    # 说明：使用 Open-Meteo 免费 API，无需 Key。
    # 先用城市名解析经纬度，再查询实时天气和今明两天预报。
    # WMO 天气代码 -> 中文描述
    wmo_map = {
        0: "晴", 1: "基本晴朗", 2: "局部多云", 3: "阴",
        45: "雾", 48: "雾凇",
        51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
        56: "冻毛毛雨", 57: "强冻毛毛雨",
        61: "小雨", 63: "中雨", 65: "大雨",
        66: "冻雨", 67: "强冻雨",
        71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
        80: "小阵雨", 81: "阵雨", 82: "强阵雨",
        85: "小阵雪", 86: "强阵雪",
        95: "雷暴", 96: "雷暴伴小冰雹", 99: "雷暴伴大冰雹",
    }
    try:
        # 1. 地理编码：城市名 -> 经纬度
        geo_url = ("https://geocoding-api.open-meteo.com/v1/search?"
                   + urllib.parse.urlencode({"name": city, "count": 1, "language": "zh", "format": "json"}))
        with urllib.request.urlopen(geo_url, timeout=15) as resp:
            geo_data = json.loads(resp.read().decode("utf-8"))

        results = geo_data.get("results")
        if not results:
            return f"未找到城市: {city}，请检查城市名是否准确。"
        loc = results[0]
        lat = loc["latitude"]
        lon = loc["longitude"]
        city_name = loc.get("name", city)
        country = loc.get("country", "")

        # 2. 查询实时天气 + 今明两天预报（forecast_days=2）
        weather_url = ("https://api.open-meteo.com/v1/forecast?"
                       + urllib.parse.urlencode({
                           "latitude": lat, "longitude": lon,
                           "current_weather": "true",
                           "daily": "weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                           "forecast_days": 2,
                           "timezone": "auto",
                       }))
        with urllib.request.urlopen(weather_url, timeout=15) as resp:
            weather_data = json.loads(resp.read().decode("utf-8"))

        lines = [f"{city_name}({country})天气："]
        # 实时天气（可选返回段）
        current = weather_data.get("current_weather") or {}
        if current:
            temp = current.get("temperature")
            wcode = current.get("weathercode")
            desc = wmo_map.get(wcode, f"未知(代码{wcode})")
            lines.append(f"实时: {desc}，{temp}°C")

        # 今明两天预报
        daily = weather_data.get("daily") or {}
        dates = daily.get("time") or []
        wcodes = daily.get("weathercode") or []
        tmax = daily.get("temperature_2m_max") or []
        tmin = daily.get("temperature_2m_min") or []
        precip = daily.get("precipitation_probability_max") or []
        weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        label_cn = ["今日", "明日"]
        for i in range(min(2, len(dates))):
            d = datetime.date.fromisoformat(dates[i])
            w = wmo_map.get(wcodes[i], f"未知(代码{wcodes[i]})")
            p = precip[i] if i < len(precip) and precip[i] is not None else "-"
            lines.append(
                f"{label_cn[i]}({d.month}月{d.day}日 {weekday_cn[d.weekday()]}): {w}，"
                f"{tmin[i]}~{tmax[i]}°C，降水概率{p}%"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"天气查询失败: {str(e)}。提示：请检查网络连接。"

def File_Read(path: str, max_chars: int = 3000, encoding: str = "utf-8") -> str:
    """文件读取：读取指定路径的文本或PDF文件内容（受max_chars长度限制）"""
    # PDF 是二进制格式，需走专门的文本提取（pypdf），其余按文本读取
    if path.lower().endswith(".pdf"):
        return _Read_PDF(path, max_chars)
    try:
        with open(path, "r", encoding=encoding) as f:
            content = f.read()
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n...[内容过长，已截断，共{len(content)}字符]"
        return f"文件 {path} 内容:\n{content}"
    except FileNotFoundError:
        return f"文件读取失败: 文件不存在 {path}"
    except IsADirectoryError:
        return f"文件读取失败: {path} 是一个目录，请指定具体文件。"
    except UnicodeDecodeError:
        return (f"文件读取失败: {path} 不是有效的{encoding}文本。"
                f"可能是二进制文件或编码不符，可尝试指定encoding参数。")
    except Exception as e:
        return f"文件读取失败: {str(e)}"

def _Read_PDF(path: str, max_chars: int = 3000) -> str:
    """PDF文件读取：使用pypdf逐页提取文本（受max_chars长度限制）"""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ("PDF读取失败: 未安装pypdf库，请先执行 pip install pypdf。")
    try:
        reader = PdfReader(path)
        parts = []
        for i, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            parts.append(f"[第{i}页]\n{text}")
        content = "\n".join(parts).strip()
        if not content:
            return f"PDF读取失败: {path} 未提取到文本，可能是扫描版PDF（图片），需要OCR支持。"
        total_chars = len(content)
        if total_chars > max_chars:
            content = content[:max_chars] + f"\n...[内容过长，已截断，共{total_chars}字符]"
        return f"文件 {path} 内容(PDF共{len(reader.pages)}页):\n{content}"
    except Exception as e:
        return f"PDF读取失败: {str(e)}"

def Ip_Lookup(ip: str = None) -> str:
    """IP定位：查询指定IP的地理位置；不传IP时查询本机公网IP位置"""
    # 说明：使用 ipwho.is 免费 API（支持HTTPS，无需Key），返回中文描述。
    try:
        target = ip if ip else ""
        url = f"https://ipwho.is/{urllib.parse.quote(target)}"
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if not data.get("success", False):
            return f"IP查询失败: {data.get('message', '未知错误')}"

        query_ip = data.get("ip", ip or "本机")
        parts = [f"IP {query_ip} 位置信息:"]
        if data.get("country"):      parts.append(f"国家/地区: {data['country']}")
        if data.get("region"):       parts.append(f"省份/州: {data['region']}")
        if data.get("city"):         parts.append(f"城市: {data['city']}")
        if data.get("latitude") is not None and data.get("longitude") is not None:
            parts.append(f"经纬度: {data['latitude']}, {data['longitude']}")
        if data.get("connection", {}).get("isp"):
            parts.append(f"运营商: {data['connection']['isp']}")
        return "\n".join(parts)
    except Exception as e:
        return f"IP查询失败: {str(e)}。提示：请检查网络连接。"

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
    "weather_search": Weather_search,
    "file_read": File_Read,
    "ip_lookup": Ip_Lookup,
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
    },
    {
        "type": "function",
        "function": {
            "name": "weather_search",
            "description": "查询指定城市的实时天气",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名，如 北京、上海、伦敦"}
                },
                "required": ["city"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_read",
            "description": "读取本地文件内容：支持文本文件(.txt/.py/.md/.csv等)与PDF(.pdf)，自动截断超长内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径，支持文本文件或PDF"},
                    "max_chars": {"type": "integer", "description": "返回内容最大长度，默认3000"},
                    "encoding": {"type": "string", "description": "文本文件编码，默认utf-8（PDF无需指定）"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ip_lookup",
            "description": "查询IP地址的地理位置，不传IP时查询本机公网IP",
            "parameters": {
                "type": "object",
                "properties": {
                    "ip": {"type": "string", "description": "要查询的IP地址，可省略"}
                },
                "required": []
            }
        }
    }
]

if __name__ == "__main__":
    ans = Web_Search("美国2025年GDP为？")
    print(ans)