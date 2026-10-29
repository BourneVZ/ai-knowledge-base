# AI 知识库系统

> 基于多 Agent 协作的 AI 技术知识库——自动采集、智能分析、定时推送。

每周六凌晨自动从 GitHub 与 11 个 RSS 源（Hacker News、arXiv、OpenAI / Anthropic / Google AI / HuggingFace 博客、Reddit、机器之心等）采集 AI / LLM / Agent 相关内容，经 LangGraph 工作流分析、评分、审核后沉淀为结构化 JSON 知识条目，并可通过 Telegram 与企业微信推送简报、由 OpenClaw 助手随时问答检索。

## 架构概览

```text
┌─────────────────────────── Agent 层 ────────────────────────────┐
│  .opencode/   OpenCode Agent 角色（采集 / 分析 / 整理）+ 技能     │
│  workflows/   LangGraph 节点 Agent                               │
│               Planner → Collector → Analyzer → Reviewer          │
│                 ↑______ Reviser（审核循环）_______↓               │
│               → Organizer                                        │
│  patterns/    Router / Supervisor 多 Agent 协作模式               │
├────────────────────────── Pipeline 层 ───────────────────────────┤
│  pipeline/    V2 自动化流水线（collect → analyze → organize）     │
│  core/        统一 LLM 客户端（DeepSeek / Qwen / OpenAI）+ 配置   │
│  config.yaml  采集档位（lite / standard / full）与审核策略         │
│  rss_sources.yaml   RSS 源配置（11 源）                           │
│  knowledge/   raw/（原始采集存档，只读）→ articles/（知识条目      │
│               JSON + index.json 检索索引）                        │
│  mcp_knowledge_server.py   MCP 知识检索服务                       │
├─────────────────────────── 工程层 ───────────────────────────────┤
│  safeguards/  CostGuard 预算熔断 + Security 安全防护              │
│  hooks/       JSON schema 校验 + 内容质量评分                     │
│  tests/       pytest 单元 / 集成测试（branch coverage ≥ 80%）     │
│  质量管线     ruff（lint + format）· mypy --strict                │
│  .github/     GitHub Actions 每周六定时采集，自动提交入库          │
├─────────────────────────── 分发层 ───────────────────────────────┤
│  v4-production/distribution/   formatter（Markdown / Telegram /   │
│                企业微信三格式）+ publisher 多渠道异步推送           │
│  v4-production/bot/   知识库交互（搜索 / 订阅 / 权限 / 意图识别）  │
│  v4-production/openclaw/   OpenClaw 常驻助手 Echo（人格 +          │
│                top-rated / category-summary / weekly-digest 技能）│
└──────────────────────────────────────────────────────────────────┘
```

## 快速开始

```bash
# 1. 克隆仓库
git clone https://github.com/BourneVZ/ai-knowledge-base.git
cd ai-knowledge-base

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，至少填写：
#   LLM_PROVIDER=deepseek
#   DEEPSEEK_API_KEY=sk-xxx

# 3. 安装依赖并运行采集流水线
pip install -r requirements.txt
python pipeline/pipeline.py --sources github,rss --limit 20 --verbose
```

**免服务器路线**：把 `LLM_API_KEY` 等配置到仓库的 GitHub Actions Secrets 后，无需本地环境——`weekly-collect.yml` 每周六北京时间 02:00 自动采集、校验并提交入库，也可在 Actions 页面手动触发（workflow_dispatch）。

**进阶模块**：

- V3 LangGraph 工作流（`workflows/`）：需额外安装 `langgraph`；
- V4 推送（`v4-production/distribution/`）：需配置 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` / `WEIXIN_WEBHOOK_URL`；
- V4 交互助手（`v4-production/openclaw/`）：由 OpenClaw 网关接入 Telegram。

## 目录结构

| 目录 / 文件 | 说明 | 引入版本 |
| --- | --- | --- |
| `.opencode/` | OpenCode Agent 角色定义与技能（采集 / 分析 / 整理） | V1 |
| `specs/` | PRD、项目愿景、编码规范等文档 | V1 |
| `knowledge/raw/` | 原始采集存档（只读，禁止覆盖或删除） | V1 |
| `knowledge/articles/` | 结构化知识条目 JSON + `index.json` 检索索引 | V1 / V4 |
| `hooks/` | JSON schema 校验、内容质量评分钩子 | V1 |
| `core/` | 共享包：统一 LLM 客户端、配置、RSS 源读取 | V2 |
| `pipeline/` | V2 自动化流水线（collect → analyze → organize） | V2 |
| `rss_sources.yaml` | 11 个 RSS 源配置 | V2 |
| `mcp_knowledge_server.py` | MCP 知识检索服务 | V2 |
| `.github/workflows/` | 每周定时采集 workflow | V2 |
| `tests/` | 单元测试与集成测试（pytest + coverage） | V2 |
| `patterns/` | Router / Supervisor 多 Agent 协作模式 | V3 |
| `workflows/` | V3 LangGraph 工作流（含审核循环） | V3 |
| `config.yaml` | 采集档位与审核策略配置 | V3 |
| `safeguards/` | CostGuard 预算熔断 + Security 安全防护 | V3 |
| `v4-production/distribution/` | formatter 三格式输出 + publisher 异步推送 | V4 |
| `v4-production/bot/` | 知识库交互 bot（搜索 / 订阅 / 权限 / 意图识别） | V4 |
| `v4-production/openclaw/` | OpenClaw Echo 助手工作区（人格 + 检索技能） | V4 |

## 技术栈

| 类别 | 选型 |
| --- | --- |
| 语言 | Python 3.11+（编码规范按 3.12：ruff lint/format + mypy --strict） |
| Agent 框架 | OpenCode（角色与技能）、LangGraph（工作流）、OpenClaw（常驻助手） |
| 大模型 | DeepSeek（默认 `deepseek-chat`），OpenAI 兼容协议可切换 Qwen / OpenAI |
| 调度 | GitHub Actions（每周六 02:00 定时采集 + 手动触发） |
| 推送渠道 | Telegram Bot API、企业微信群机器人 webhook（aiohttp 异步） |
| 主要依赖 | httpx、aiohttp、pyyaml、python-dotenv |

## 版本历史

- **V1 · 2026-06 · 多 Agent 手动协作** —— OpenCode 定义采集 / 分析 / 整理三类 Agent 角色与 github-trending 技能；建立 AGENTS.md 协作规范、JSON 校验与质量评分钩子；产出首批知识条目。
- **V2 · 2026-07 · 自动化流水线** —— pipeline 四步自动化；GitHub Search + 11 个 RSS 源统一采集；多 provider 统一模型客户端（默认 DeepSeek）；MCP 知识检索服务；GitHub Actions 每周定时采集并自动提交。
- **V3 · 2026-07 ~ 08 · LangGraph 工作流** —— 5 节点工作流（Planner → Collector → Analyzer → Reviewer → Organizer）+ Reviser 审核循环（5 维加权评分、三路条件路由、动态档位策略）；Router / Supervisor 多 Agent 模式；CostGuard 预算熔断与 Security 防护接入 LLM 客户端和工作流；防 LLM 篡改事实字段、raw 数据落盘。
- **V4 · 2026-10 · 分发与交互** —— formatter 分组简报（Markdown / Telegram / 企业微信，短摘要 + 截断 + 索引预览）；publisher 多渠道异步推送；`index.json` 检索索引；知识库交互 bot（搜索 / 订阅 / 权限 / 意图识别）；OpenClaw 助手 Echo + 3 个检索技能。

## 月度成本估算

| 项目 | 估算（人民币 / 月） | 说明 |
| --- | --- | --- |
| LLM API（DeepSeek） | ¥2 – 5 | 每周 1 次采集、约 20 – 30 篇 / 次，单次成本 < ¥1（`BUDGET_YUAN=1.0` 预算熔断兜底） |
| 服务器（GitHub Actions） | ¥0 | 公共仓库免费；私有仓库单次约 5 – 10 分钟，远低于免费额度 |
| OpenClaw 常驻（可选） | ¥20 – 40 | 最低配 VPS；只跑定时采集完全不需要服务器 |
| **合计** | **约 ¥5**（含常驻助手约 ¥25 – 45） | 按 `deepseek-chat` 单价与当前采集量估算 |

## License

[MIT](LICENSE) © 2026 BourneZ
