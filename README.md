# Multi-Agent Cooperation And Arbitration Framework (多 Agent 协同处理仲裁框架)

> 一个基于 ReAct 范式的多 Agent 协同与仲裁系统，通过“成员独立推理 + 仲裁交叉裁决”机制，模拟员工 + 全知Boss的工作场景。

## 📖 项目简介
本项目手写实现了 ReAct 闭环（不依赖 LangChain），设计成员角色池与仲裁角色池，支持用户指定至少两名成员与一名仲裁者进行交叉裁决。系统内置 10 种工具，支持异常捕获与降级策略，并具备全链路轨迹追踪与持久化存储，支持回放与调试。

## ✨ 核心特性
- 🧠 **手写 ReAct 循环**：手动实现 `Thought → Action → Observation` 闭环，工具动态注册，循环上限可配置（默认 4 轮），防止 Token 过度消耗。
- 🤝 **多 Agent 协作机制**：成员独立推理 + 仲裁交叉裁决，模拟真实团队中“信息受限”的协作场景。
- 🛠️ **工具调用与容错**：内置 10 种工具，设计异常捕获与降级策略，单点故障时返回缓存或提示信息，避免 Agent 中断。
- 🔒 **权限隔离与扩展性**：工具定义与实现分离（`TOOL_MAP + TOOL_DEFINITIONS`），成员角色按白名单调用工具。
- 📊 **全链路可观测性**：记录每次推理轨迹（Thought/Action/Observation），持久化存储至数据库，支持回放与调试。

## 🚀 快速开始

### 1. 创建 Conda 虚拟环境
建议使用 Conda 来管理环境（推荐 Python 3.11 或以上）：
```bash
conda create -n multi_agent python=3.11 -y
conda activate multi_agent
```

### 2. 安装依赖
```bash
pip install -r requirement.txt
```

### 3. 配置API-key
**在项目根目录创建 .env 文件，并填入你的 OpenAI 格式 API Key：**
```env
API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
```

### 4. 运行项目
```bash
python main.py
```

## 📁 项目结构

```text
Multi_Agent_Corperation_And_Arbitration_Framework/
├─ main.py                 # 终端入口、并发调度、对话选择、落库与总结触发
├─ agents/
│  ├─ base_agent.py        # Agent 基类：多轮 ReAct、工具调用、轨迹、滑窗记忆
│  └─ roles_config.py      # 成员与审核员角色注册表、提示词、工具权限
├─ core/
│  ├─ llm_clients.py       # DeepSeek/OpenAI 兼容模型调用封装
│  ├─ tools.py             # 搜索、计算、翻译、天气、文件读取等工具
│  └─ database.py          # SQLite 对话、轮次、答案与长期记忆存储
├─ requirement.txt         # 依赖清单
└─ 项目简介.txt            # 项目目标与功能说明
```

### 🗺️ Roadmap (未来规划)

```text
□ 完成交互式模式（循环迭代，仲裁者给出建议、成员修改直至终止）
□ 尝试将无监督群体学习中的“共识函数”数学表达融入仲裁者逻辑
□ 评估多个基学习器（Agent）的多样性指标
```
