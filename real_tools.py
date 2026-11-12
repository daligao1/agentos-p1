#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""真实工具层（P1/P2-lite）：只读访问 agent_im.db + 多通道投递（离线可用）。

通道（环境变量 P1_SEND_CHANNEL）：
- auto（默认）：telegram 配置齐全就用 telegram，否则/失败自动回退 local
- local：把文件投递到项目内 `收件箱/<日期>/`，并写审计日志（离线可用，零网络）
- telegram：调 Telegram Bot API sendMessage（需 P1_TG_BOT_TOKEN + P1_TG_CHAT_ID；失败自动回退 local）
- agentim：POST 到本机 agent-im 原型服务（需 P1_AGENTIM_BASE + P1_AGENTIM_TOKEN + P1_AGENTIM_GROUP；失败回退 local）

安全约束：
- SQLite 只读打开（mode=ro + query_only），本模块没有写库工具；
- 所有查询固定 SQL + 参数化，LLM 不能传任意 SQL；
- 发送必先经过图里的 interrupt 审批（send_file 强制 risk=high）。
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import sqlite3
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(ROOT, "workspace")
OUTBOX = os.path.join(ROOT, "outbox")
AUDIT = os.path.join(OUTBOX, "audit.log")
INBOX = os.path.join(ROOT, "收件箱")

_AGENT_DB_CANDIDATES = [
    os.environ.get("AGENT_DB", ""),
    os.path.join(ROOT, "..", "..", "..", "tools", "agent-im", "data", "agent_im.db"),
]


def db_path() -> str:
    for p in _AGENT_DB_CANDIDATES:
        if p and os.path.exists(os.path.abspath(p)):
            return os.path.abspath(p)
    raise FileNotFoundError("找不到 agent_im.db（可用环境变量 AGENT_DB 指定）")


def _con():
    con = sqlite3.connect("file:" + db_path() + "?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = 1")
    return con


def _now() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def _stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _audit(line: str) -> None:
    os.makedirs(OUTBOX, exist_ok=True)
    with open(AUDIT, "a", encoding="utf-8") as f:
        f.write(f"{_now()} {line}\n")


STATUS_LABEL = {"pending": "待开始", "doing": "进行中", "approved": "已完成", "done": "已完成"}


def _status_where(status: str) -> tuple[str, list]:
    s = (status or "unfinished").strip().lower()
    if s in ("unfinished", "open", "未完成"):
        return "status NOT IN ('approved','done','completed')", []
    if s == "all":
        return "1=1", []
    if s in ("pending", "doing", "approved", "done"):
        if s == "done":
            return "status IN ('approved','done','completed')", []
        return "status = ?", [s]
    raise ValueError(f"不支持的 status：{status}（可选 unfinished/pending/doing/approved/all）")


def task_summary_by_assignee(status: str = "unfinished") -> str:
    """按负责人汇总任务数（真实数据来自 agent_im.db 的 tasks + members）。"""
    where, params = _status_where(status)
    sql = f"""
        SELECT t.assignee AS assignee, COALESCE(m.name, t.assignee) AS name,
               COALESCE(m.dept, '-') AS dept,
               SUM(CASE WHEN t.status='pending' THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN t.status='doing'   THEN 1 ELSE 0 END) AS doing,
               SUM(CASE WHEN t.status IN ('approved','done','completed') THEN 1 ELSE 0 END) AS done,
               COUNT(*) AS total
        FROM tasks t LEFT JOIN members m ON m.id = t.assignee
        WHERE {where}
        GROUP BY t.assignee, m.name, m.dept
        ORDER BY total DESC, t.assignee
    """
    con = _con()
    try:
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()
    total = sum(r["total"] for r in rows)
    lines = [f"按负责人汇总（status={status}，共 {total} 条）：", "",
             "| 负责人 | 姓名 | 部门 | 待开始 | 进行中 | 已完成 | 合计 |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['assignee']} | {r['name']} | {r['dept']} | {r['pending']} | {r['doing']} | {r['done']} | {r['total']} |")
    if not rows:
        lines.append("| - | - | - | 0 | 0 | 0 | 0 |")
    return "\n".join(lines)


def query_tasks(status: str = "unfinished", assignee: str = "", limit: int = 50) -> str:
    """查询任务明细（只读、参数化）。"""
    where, params = _status_where(status)
    if assignee:
        where += " AND t.assignee = ?"
        params.append(assignee)
    limit = max(1, min(int(limit or 50), 100))
    sql = f"""
        SELECT t.id, t.title, t.assignee, COALESCE(m.name, t.assignee) AS name, t.status,
               t.progress, t.planned_end
        FROM tasks t LEFT JOIN members m ON m.id = t.assignee
        WHERE {where} ORDER BY t.id LIMIT {limit}
    """
    con = _con()
    try:
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()
    lines = [f"任务明细（status={status}，返回 {len(rows)} 条）：", "",
             "| id | 标题 | 负责人 | 状态 | 进度 | 计划完成 |", "|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['id']} | {r['title']} | {r['name']}({r['assignee']}) | {STATUS_LABEL.get(r['status'], r['status'])} | {r['progress']}% | {r['planned_end'] or '-'} |")
    if not rows:
        lines.append("| - | 无数据 | - | - | - | - |")
    return "\n".join(lines)


def write_file(path: str, content: str) -> str:
    os.makedirs(WORK, exist_ok=True)
    full = os.path.abspath(os.path.join(WORK, path))
    if not full.startswith(os.path.abspath(WORK) + os.sep):
        raise PermissionError(f"越界写入被拒绝：{path}")
    with open(full, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    return f"已写入 workspace/{os.path.relpath(full, WORK)}（{len(content)} 字符）"


# ---------------------------------------------------------------- 投递通道
def _resolve_src(path: str) -> str:
    p = str(path).replace("\\", "/").strip()
    if p.lower().startswith("workspace/"):
        p = p[len("workspace/"):]
    src = os.path.abspath(p) if os.path.isabs(p) else os.path.abspath(os.path.join(WORK, p))
    if not os.path.exists(src):
        raise FileNotFoundError(f"要发送的文件不存在：{path}（解析为 {src}）")
    return src


def _deliver_local(target: str, src: str) -> str:
    day = datetime.date.today().isoformat()
    dst_dir = os.path.join(INBOX, day)
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, f"{_stamp()}-{target}-{os.path.basename(src)}")
    shutil.copyfile(src, dst)
    rel = os.path.relpath(dst, ROOT)
    _audit(f"[local] 投递 {os.path.basename(src)} -> {target}，文件 {rel}")
    return f"[local] 已投递到收件箱：{rel}（离线可用，双击即可查看）"


def _deliver_telegram(target: str, src: str) -> str:
    bot = os.environ.get("P1_TG_BOT_TOKEN", "").strip()
    chat = os.environ.get("P1_TG_CHAT_ID", "").strip()
    if not bot or not chat:
        raise RuntimeError("Telegram 未配置（缺 P1_TG_BOT_TOKEN / P1_TG_CHAT_ID）")
    text = open(src, encoding="utf-8").read()
    if len(text) > 3500:
        text = text[:3500] + "\n…（报告过长已截断，全文见收件箱文件）"
    payload = json.dumps({"chat_id": chat, "text": f"[AgentOS P1] 发给 {target}\n\n{text}",
                          "disable_web_page_preview": True}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{bot}/sendMessage", data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"Telegram 网络不可用（{type(e).__name__}: {e}）")
    if not body.get("ok"):
        raise RuntimeError(f"Telegram 返回失败：{body}")
    _audit(f"[telegram] 发送 {os.path.basename(src)} -> {target}（chat_id={chat}）")
    return f"[telegram] 已发送给 {target}（chat_id={chat}）"


def _deliver_agentim(target: str, src: str) -> str:
    base = os.environ.get("P1_AGENTIM_BASE", "http://127.0.0.1:8787").rstrip("/")
    token = os.environ.get("P1_AGENTIM_TOKEN", "").strip()
    group = os.environ.get("P1_AGENTIM_GROUP", "G-PRJ1").strip()
    if not token:
        raise RuntimeError("agent-im 未配置（缺 P1_AGENTIM_TOKEN）")
    text = open(src, encoding="utf-8").read()[:3800]
    payload = json.dumps({"body": f"[AgentOS P1] 发给 {target}\n\n{text}", "msgtype": "text"},
                         ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"{base}/api/groups/{group}/messages", data=payload,
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"agent-im 服务不可用（{type(e).__name__}: {e}）")
    if not body.get("ok", True):
        raise RuntimeError(f"agent-im 返回失败：{body}")
    _audit(f"[agentim] 发送 {os.path.basename(src)} -> {base} group={group}")
    return f"[agentim] 已投递到本地 agent-im group={group}（{base}）"


def send_file(target: str, path: str) -> str:
    """审批通过后调用：按 P1_SEND_CHANNEL 投递；任何通道失败都回退 local（离线可送达）。"""
    src = _resolve_src(path)
    channel = (os.environ.get("P1_SEND_CHANNEL") or "auto").strip().lower()
    tried: list[str] = []
    if channel in ("auto", "telegram"):
        try:
            return _deliver_telegram(target, src)
        except Exception as e:
            tried.append(f"telegram: {e}")
            _audit(f"[fallback] telegram 不可用 -> local（{e}）")
    if channel in ("auto", "agentim"):
        try:
            return _deliver_agentim(target, src)
        except Exception as e:
            tried.append(f"agentim: {e}")
            _audit(f"[fallback] agentim 不可用 -> local（{e}）")
    msg = _deliver_local(target, src)
    if tried:
        msg += "\n    （备用通道尝试记录：" + "；".join(tried) + "）"
    return msg


# 白名单：planner 只能从这四个里选
TOOLS = {
    "task_summary_by_assignee": task_summary_by_assignee,
    "query_tasks": query_tasks,
    "write_file": write_file,
    "send_file": send_file,
}
