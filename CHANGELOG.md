# Changelog

## v0.1.0（2026-11-12）

首个可用版本：

- LangGraph 状态图：`plan → execute ⇄ execute → report`，SQLite checkpoint 支持跨进程恢复
- DeepSeek（deepseek-flash）结构化规划 + 白名单校验 + 规则回退
- 只读工具：`task_summary_by_assignee` / `query_tasks`（mode=ro + query_only + 参数化）
- 人工确认门：`send_file` 强制中断等待 approve/reject
- 投递：`auto`（Telegram → 本地 agent-im → 本地收件箱，失败自动回退）
- 审计：`outbox/audit.log`；验收脚本 `run_acceptance.ps1`（14 项）
- CI：ruff + pytest（GitHub Actions）
