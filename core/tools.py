"""tools.py —— 外部工具集（Agent 可以调用的所有"能力"）

模型本身只会"说"，工具才是它能"做"的事（查时间、算数、搜索、读文件……）。

三个关键数据结构（在文件末尾）：
    TOOL_MAP          工具名 → 真正的 Python 函数（执行时按名字查这张表）
    TOOL_DEFINITIONS  工具的"说明书"（名字/描述/参数），发给模型让它知道有哪些工具
    角色白名单         在 roles_config.py 里，决定每个角色能用哪些工具
                       （base_agent 会按白名单过滤 TOOL_DEFINITIONS，实现权限隔离）

写一个新工具的步骤：①写函数 ②加进 TOOL_MAP ③加进 TOOL_DEFINITIONS ④给需要的角色加白名单。

约定：所有工具函数都"失败不抛异常"，而是返回一段错误文本——
      因为工具结果最终是喂给模型的，抛异常会打断整个推理。
"""
import datetime
import math
import json
import os
import sys
import tempfile
import subprocess
import urllib.request
import urllib.parse
from ddgs import DDGS
from deep_translator import GoogleTranslator

# 时间缓存：一轮对话里多次取时间应返回同一个值（避免"第一句问时间、第二句秒数变了"）。
# 由 main.py 每轮开始时调用 Reset_Cache() 清空，保证每轮拿到新鲜时间。
_CACHED_TIME = None

# 获取日期时间（带缓存）
def Get_Current_Date():
    """返回当前日期时间字符串；同一轮内重复调用返回同一值（走缓存）。"""
    global _CACHED_TIME
    if _CACHED_TIME is None:
        _CACHED_TIME = datetime.datetime.now().strftime("%Y年%m月%d日 %H:%M:%S")
    return _CACHED_TIME

def Reset_Cache():
    """清空时间缓存。main.py 在每轮对话开始前调用。"""
    global _CACHED_TIME
    _CACHED_TIME = None

#  简单计算器
def Calculator(expression: str) -> str:
    """计算一个数学表达式（字符串），返回结果文本。
    安全说明：用受限的 eval —— 清空了 __builtins__，只允许 math 里的函数和 abs/round，
    所以模型传进来的表达式碰不到文件、系统命令等危险操作。"""
    allowed_names = {k: v for k, v in math.__dict__.items() if not k.startswith("__")}
    allowed_names.update({"abs": abs, "round": round})
    try:
        result = eval(expression, {"__builtins__": {}}, allowed_names)
        return f"计算结果:{result}"
    except Exception as e:
        return f"计算错误:{str(e)}"

# 日历计算，基准日期的多少天后的日期
def Date_Calculator(days: int, base_date: str = None) -> str:
    """计算某个基准日期前后 N 天的日期。days 正数代表未来、负数代表过去；
    base_date 为空则以今天为基准。"""
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
    """把文本翻译成目标语言（target_lang 如 zh-CN / en / ja）。"""
    try:
        translator = GoogleTranslator(source='auto', target= target_lang)
        translated_text = translator.translate(text)
        return f"翻译结果({target_lang}):{translated_text}"
    except Exception as e:
        return f"翻译失败{str(e)}"

# 实现外部信息检索
def Web_Search(query: str, max_result: int = 3) -> str:
    """联网搜索，返回前 max_result 条结果的标题/摘要/链接。"""
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


def Weather_search(city: str = "") -> str:
    """天气查询：通过城市名获取实时天气与今明两天的预报。
    用 Open-Meteo 免费 API（无需 Key）：先把城市名解析成经纬度，再查天气。"""
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

        # 今明两天预报：把接口返回的各数组按下标逐天拼装
        daily = weather_data.get("daily") or {}
        dates = daily.get("time") or []
        wcodes = daily.get("weathercode") or []
        tmax = daily.get("temperature_2m_max") or []
        tmin = daily.get("temperature_2m_min") or []
        precip = daily.get("precipitation_probability_max") or []
        weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        label_cn = ["今日", "明日"]
        for i in range(min(2, len(dates))):     # 最多取两天
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

# ---------------------- 文件读取（带安全限制） ----------------------

# 工作目录（core 的上一级，即项目根 Multi_Agent_Corperation_And_Arbitration_Framework）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 禁止读取的配置文件/目录名
_DENIED_NAMES = {".env", ".gitignore", ".git", ".idea", "__pycache__"}


def _check_read_path(path: str):
    """校验读取路径是否安全。返回 (是否允许, 错误信息)。
    规则：①必须位于工作目录内；②不得读取配置文件/隐藏文件。
    要点：用 realpath 先解析成真实路径再判断——这样 '../' 和符号链接都绕不过去
    （只做字符串前缀匹配是防不住的）。"""
    try:
        real = os.path.realpath(path)          # 解析真实路径，防 ../ 和符号链接绕过
        root = os.path.realpath(PROJECT_ROOT)
    except Exception as e:
        return False, f"路径解析失败: {str(e)}"

    # ① 必须在工作目录内
    try:
        if os.path.commonpath([root, real]) != root:
            return False, f"路径越出工作目录，只允许读取 {PROJECT_ROOT} 内的文件。"
    except ValueError:
        return False, "路径越出工作目录。"

    # ② 逐段检查：拒绝配置文件与隐藏文件（如 .env、.git）
    rel = os.path.relpath(real, root)
    for part in rel.split(os.sep):
        if part in (".", ".."):
            continue
        if part in _DENIED_NAMES or part.startswith("."):
            return False, f"拒绝读取配置文件或隐藏文件：{part}"
    return True, ""


def File_Read(path: str, max_chars: int = 3000, encoding: str = "utf-8") -> str:
    """文件读取：读取工作目录内的文本或 PDF 文件（超长自动截断）。
    安全限制：仅限工作目录内，且拒绝配置文件/隐藏文件（防止泄露 .env 里的密钥）。"""
    # 安全校验：越界或配置文件一律拒绝
    ok, err = _check_read_path(path)
    if not ok:
        return f"文件读取失败：{err}"
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
    """读取 PDF 的文本内容：用 pypdf 逐页提取，拼起来再截断。
    pypdf 采用"延迟导入"——没装也不能让整个 tools 模块 import 失败，只在使用时报错。
    注意：只能提取"有文本层"的 PDF；扫描件（图片型）提取不到文字，会提示需要 OCR。"""
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
    """IP 定位：查询指定 IP 的地理位置；不传 IP 则查本机公网 IP。
    用 ipwho.is 免费 API（HTTPS、无需 Key）。"""
    try:
        target = ip if ip else ""
        url = f"https://ipwho.is/{urllib.parse.quote(target)}"
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if not data.get("success", False):
            return f"IP查询失败: {data.get('message', '未知错误')}"

        # 逐字段拼接，有才加，避免返回一堆 null
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


def Get_History_Memory(n: int = 4) -> str:
    """【只读工具】读取"当前对话"最近 N 轮的历史记忆，返回可读文本。
    供模型主动调用（如被问"我们前面聊了什么"）。
    "当前对话"从 ContextVar 取（database.get_current_conversation），每个请求各自隔离。
    注意：这是读；写由编排层 main.persist_turn 负责，两件事分开。"""
    try:
        from core import database
    except ImportError:
        import database
    conv_id = database.get_current_conversation()
    if conv_id is None:
        return "当前没有进行中的对话，无法读取历史记忆。"
    turns = database.get_recent_turns(conv_id, n)
    if not turns:
        return "当前对话暂无历史记录。"
    lines = [f"当前对话最近 {len(turns)} 轮历史："]
    for t in turns:
        lines.append(f"\n【第{t['seq']}轮】问：{t['question']}")
        for a in t.get("answers", []):
            lines.append(f"  - {a['role_id']}：{a['content']}")
        if t.get("verdict"):
            lines.append(f"  裁决({t.get('arbiter_role_id') or '审核员'})：{t['verdict']}")
    return "\n".join(lines)


def Rag():
    """[未实现] RAG 检索。计划移交到第二个项目「Paper Mind」实现，此处保留占位。"""
    # 从文本嵌入的向量数据库中拿知识
    pass


def Self_Summary(role_name: str, previous_big: str, recent_smalls: list) -> str:
    """【编排层调用，非模型工具】"大总结"：把某角色的「上一次大总结 + 最近5条小总结」
    压成一条新的、更短的大总结。由 main.maybe_big_summary 每 5 轮调用一次。
    说明：这是一次独立的模型调用，输入输出都很短，所以成本很低。
    注意：它不注册进 TOOL_MAP——模型不能主动调它，只能由系统触发。"""
    # 延迟导入 prepare_message，避免与 llm_clients 循环导入
    try:
        from core.llm_clients import prepare_message
    except ImportError:
        from llm_clients import prepare_message

    # 拼装压缩提示词：角色身份 + 更早的长期记忆 + 最近几轮的小结
    parts = [f"你是「{role_name}」。请把下面你过往的记忆压缩成一段不超过150字的连贯小结，"
             "只保留关键话题、结论与立场，不要展开论述。"]
    if previous_big:
        parts.append(f"\n[更早的长期记忆]\n{previous_big}")
    if recent_smalls:
        joined = "\n".join(f"- {s}" for s in recent_smalls)
        parts.append(f"\n[最近几轮的小结]\n{joined}")
    parts.append("\n请直接输出压缩后的那段小结，不要任何前缀、解释或标签。")

    prompt = "\n".join(parts)
    try:
        response = prepare_message([{"role": "user", "content": prompt}], tool=None)
        text = (response.choices[0].message.content or "").strip()
        # 兜底：万一模型仍带 <summary> 标签，剥掉
        if "<summary>" in text:
            text = text.split("<summary>")[-1].split("</summary>")[0].strip()
        return text[:200]
    except Exception as e:
        # 大总结失败不算致命：返回错误文本，调用方判断非空才写库
        return f"大总结失败: {str(e)}"


def Code_Sandbox(code: str, timeout: int = 10) -> str:
    """代码沙盒：在独立子进程中执行 Python 代码，返回标准输出/错误。

    原理：把代码写成临时脚本 → 用 subprocess 起一个"新的 Python 进程"去跑 →
    父进程通过管道把子进程的输出捞回来。好处是子进程崩溃/死循环不波及主程序。

    诚实说明：这是"运行时隔离 + 超时"，**不是硬安全沙箱**——
    代码仍可读写文件、访问网络、吃 CPU。生产环境应换成 Docker 容器隔离。
    超时(秒)默认10、上限30。"""
    # 限制超时范围，避免调用方传入过大值
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = 10
    timeout = max(1, min(timeout, 30))

    # 在临时目录下执行，避免污染项目目录
    with tempfile.TemporaryDirectory() as workdir:
        script_path = os.path.join(workdir, "_sandbox_run.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(code)
        try:
            proc = subprocess.run(
                [sys.executable, script_path],
                cwd=workdir,
                capture_output=True,     # 捕获子进程的 stdout/stderr
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return f"代码执行超时（超过 {timeout} 秒），已强制终止。"
        except Exception as e:
            return f"代码沙盒执行失败: {str(e)}"

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    parts = []
    if stdout:
        parts.append(f"标准输出:\n{stdout}")
    if stderr:
        parts.append(f"标准错误:\n{stderr}")
    if not parts:
        if proc.returncode != 0:
            parts.append(f"进程异常退出，退出码: {proc.returncode}（无输出）")
        else:
            parts.append("（代码执行成功，但没有输出。记得用 print 打印结果）")
    # 截断过长输出，防止撑爆模型上下文
    result = "\n".join(parts)
    if len(result) > 3000:
        result = result[:3000] + "\n...[输出过长，已截断]"
    return result


# ---------------------- 工具注册表 ----------------------

# TOOL_MAP：工具名 → 真正的函数。base_agent 执行工具时按名字来这里查对应函数。
TOOL_MAP = {
    "get_current_time": Get_Current_Date,
    "calculator": Calculator,
    "date_calculator": Date_Calculator,
    "translate_text": Translate_Text,
    "web_search": Web_Search,
    "weather_search": Weather_search,
    "file_read": File_Read,
    "ip_lookup": Ip_Lookup,
    "get_history_memory": Get_History_Memory,
    "code_sandbox": Code_Sandbox,
}

# TOOL_DEFINITIONS：工具的"说明书"，会原样发给模型，让它知道"有哪些工具、怎么用"。
# 每条 = 名字 + 描述 + 参数 schema（OpenAI function-calling 格式）。
# 描述写得越清楚，模型越能选对工具、传对参数。
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
            "description": "读取工作目录内的文本文件(.txt/.py/.md/.csv等)或PDF(.pdf)。仅限工作目录内，且不能读取配置文件(.env等)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径（须在工作目录内），支持文本文件或PDF"},
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
    },
    {
        "type": "function",
        "function": {
            "name": "get_history_memory",
            "description": "读取当前对话最近若干轮的历史记忆（问题、各成员回答、审核员裁决）",
            "parameters": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "description": "读取最近几轮，默认4"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "code_sandbox",
            "description": "在沙盒中运行一段Python代码并返回执行结果（标准输出/错误）。适合做数值计算、验证算法、数据处理等；代码中用print输出结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "要执行的Python代码"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认10，上限30"}
                },
                "required": ["code"]
            }
        }
    }
]

if __name__ == "__main__":
    ans = Web_Search("美国2025年GDP为？")
    print(ans)