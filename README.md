# AgentOS P1 · 真实链路（LangGraph + DeepSeek）

> 一句话任务 → LLM 规划 → **只读**查询真实 SQLite 库 → 自动生成报告 → 人工审批 → 多通道投递（失败自动回退本地收件箱）。
> 设计目标：可中断、可恢复、可审计，**不写业务库**。

![Python](https://img.shields.io/badge/python-3.12+-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![LangGraph](https://img.shields.io/badge/langgraph-1.2.20-orange)

## 特性

| 能力 | 实现 |
|---|---|
| 任务规划 | DeepSeek `deepseek-flash`（JSON 模式）+ 白名单校验 + 规则回退 |
| 编排 | LangGraph 状态图 `plan → execute ⇄ execute → report`，SQLite checkpoint 可跨进程恢复 |
| 人工确认 | 高风险步骤 `interrupt()`，`approve / reject` 后继续或终止 |
| 只读数据 | SQLite `mode=ro` + `PRAGMA query_only=1`，固定 SQL + 参数化，**无写库工具** |
| 投递 | `auto`：Telegram → 本地 agent-im → 本地收件箱（失败自动回退，零丢件） |
| 留痕 | `outbox/audit.log` 审计 + `evidence/` 场景证据 |

## 快速开始

```bash
# 1) 环境（Windows；Python 3.12+）
powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1

# 2) 演示（5 个场景：中断/恢复/只读/拒绝/状态）
.venv/Scripts/python.exe poc.py demo

# 3) 手动跑一条任务（发送前会中断，退出码 2）
.venv/Scripts/python.exe poc.py run --task "查一下本周未完成的 tasks，按负责人汇总后发给我"
.venv/Scripts/python.exe poc.py resume --thread <thread> --decision approve
```

数据源通过环境变量指定（只读）：

```bash
export AGENT_DB=/path/to/agent_im.db   # 需含 tasks / members 表
```

## 投递通道

| 通道 | 环境变量 | 落地 |
|---|---|---|
| `auto`（默认） | — | Telegram → agentim → local 依次回退 |
| `local` | — | `收件箱/<日期>/`（离线可用） |
| `telegram` | `P1_TG_BOT_TOKEN` + `P1_TG_CHAT_ID` | Telegram |
| `agentim` | `P1_AGENTIM_BASE` + `P1_AGENTIM_TOKEN` + `P1_AGENTIM_GROUP` | 本地 agent-im 原型 |

## 目录

```
├─ poc.py                  # 图 / 审批 / 恢复 / CLI
├─ planner.py              # LLM 规划 + 白名单校验 + 规则回退
├─ real_tools.py           # 只读查询 / 写报告 / 多通道投递
├─ db_snapshot.py          # 只读快照（运行前后比对）
├─ setup.ps1               # 一键环境（uv + 兼容补丁 + 冒烟测试）
├─ tests/test_offline.py   # 离线单元测试（CI 用）
├─ .github/workflows/ci.yml
└─ docs/                   # GitHub Pages 落地页
```

## 用到的开源项目（许可已核对）

| 项目 | 用途 | 许可 |
|---|---|---|
| [langgraph](https://github.com/langchain-ai/langgraph) | 状态图编排 / checkpoint | MIT |
| [LangGraph checkpoint-sqlite](https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-sqlite) | SQLite 持久化 | MIT |
| [pytest](https://github.com/pytest-dev/pytest) | 单元测试 | MIT |
| [ruff](https://github.com/astral-sh/ruff) | Lint | MIT |
| [actions/checkout](https://github.com/actions/checkout) | CI 拉代码 | MIT |
| [actions/setup-python](https://github.com/actions/setup-python) | CI 配 Python | MIT |

## 已知限制

1. 只读：写库类动作（删除/修改）当前明确拒绝。
2. 单机 SQLite checkpoint，不支持多实例。
3. Telegram 需自备 bot token；无网络时自动回退本地收件箱。
4. Windows 上 `ormsgpack>=1.12` / `xxhash 4.x` 的 wheel 可能 DLL 加载失败，`requirements.txt` 已固定可用版本。
