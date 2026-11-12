#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Planner（P1）：DeepSeek 结构化输出 + 白名单校验 + 失败回退规则。

与 POC v0.2 同一套思路，工具换成真实链路：
  task_summary_by_assignee / query_tasks / write_file / send_file（dry-run）
Key：环境变量 DEEPSEEK_API_KEY → ~/.dsh/.credentials.yaml 的 refs.DEEPSEEK_API_KEY（不落盘、不打印）。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request

API_URL = "https://api.deepseek.com/chat/completions"
MODEL = os.environ.get("P1_LLM_MODEL", "deepseek-flash")

TOOL_SPECS = {
    "task_summary_by_assignee": {
        "desc": "按负责人汇总任务数量（真实读 agent_im.db 的 tasks + members）",
        "args": {"status": "unfinished|pending|doing|approved|all，未完成=unfinished"},
        "risk": "low",
    },
    "query_tasks": {
        "desc": "查询任务明细（标题/负责人/状态/进度/计划完成）",
        "args": {"status": "unfinished|pending|doing|approved|all", "assignee": "负责人 id，可空字符串", "limit": "整数 1-100"},
        "risk": "low",
    },
    "write_file": {
        "desc": "在 workspace/ 下写文本文件（用于生成汇总报告）",
        "args": {"path": "workspace 下相对文件名，如 summary.md", "content": "文件内容（Markdown）"},
        "risk": "low",
    },
    "send_file": {
        "desc": "把 workspace 下的文件发给指定对象——dry-run（只落 outbox/ + 审计日志），必须人工批准",
        "args": {"target": "收件人称呼，如 老板", "path": "要发送的文件路径（workspace 下）"},
        "risk": "high",
    },
}

HIGH_RISK_WORDS = ("删除", "发布", "付款", "对外", "发送", "发给", "发我", "邮件")

SYSTEM_PROMPT = """你是 AgentOS 的任务规划器。把用户的一句话任务拆成最多 4 个可执行步骤。

环境说明（重要）：
- 数据源是只读 SQLite：agent_im.db。核心表：
  tasks(id, card_id, group_id, title, assignee, status, progress, planned_start, planned_end)、
  members(id, name, dept)、groups(id, name)、cards(id, title, status)。
- tasks.status 只有 pending（待开始）/ doing（进行中）/ approved（已完成）。
  "未完成" = pending + doing；"本周"没有单独字段，按 planned_end/planned_start 判断，若任务日期在近 7 天内就都算。
- assignee 存的是成员 id（g1=小组A·研究，g2=小组B·开发，boss=老板，pm=项目经理）。
- send_file 是 dry-run（只写 outbox/ 审计日志，不真实发送），收件人写"老板"等称呼即可，不需要真实地址。
- 用户没说的细节（例如具体日期范围）按合理默认值补全，不要因此拒绝任务。

可用工具（只能用这些）：
{tools}

输出必须是 JSON 对象，格式：
{{"steps": [{{"id": 1, "tool": "工具名", "args": {{...}}, "title": "一句中文描述", "risk": "low|high", "reason": "为什么这样做（≤20字）"}}]}}

规则：
1. 只做用户要求的动作；"汇总后发我"这类任务要：先 task_summary_by_assignee 取数 → 再 write_file 写成 Markdown 报告 → 再 send_file 发送；
2. 涉及发送的步骤 risk 必须是 high；工具集做不到的动作（删除/修改/联网）不要编造，返回 {{"steps": [], "unsupported": "原因"}}；
3. args 必须符合工具参数定义；
4. 不要输出解释性文字，只输出 JSON。
"""


def load_api_key() -> str | None:
    k = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if k.startswith("sk-"):
        return k
    cred = os.path.join(os.path.expanduser("~"), ".dsh", ".credentials.yaml")
    if os.path.exists(cred):
        try:
            text = open(cred, "r", encoding="utf-8").read()
            m = re.search(r"DEEPSEEK_API_KEY:\s*(sk-[A-Za-z0-9_\-]{16,})", text) or re.search(r"(sk-[A-Za-z0-9_\-]{16,})", text)
            if m:
                return m.group(1).strip()
        except Exception:
            return None
    return None


def rule_plan(task: str) -> list[dict]:
    steps: list[dict] = []
    if any(w in task for w in ("汇总", "统计", "按负责人")):
        steps.append({"id": 1, "tool": "task_summary_by_assignee",
                      "args": {"status": "unfinished"}, "title": "按负责人汇总未完成任务", "risk": "low", "reason": "规则：含汇总"})
    elif any(w in task for w in ("查", "明细", "列表")):
        steps.append({"id": 1, "tool": "query_tasks",
                      "args": {"status": "unfinished", "assignee": "", "limit": 50}, "title": "查询未完成任务明细", "risk": "low", "reason": "规则：含查询"})
    if any(w in task for w in ("写", "保存", "报告")):
        steps.append({"id": len(steps) + 1, "tool": "write_file",
                      "args": {"path": "summary.md", "content": "# 任务汇总（规则 Planner 生成）\n"}, "title": "写汇总报告", "risk": "low", "reason": "规则：含写文件"})
    if any(w in task for w in ("发", "邮件")):
        steps.append({"id": len(steps) + 1, "tool": "send_file",
                      "args": {"target": "老板", "path": "summary.md"}, "title": "把 summary.md 发给老板", "risk": "high", "reason": "规则：对外发送"})
    if not steps:
        steps.append({"id": 1, "tool": "query_tasks",
                      "args": {"status": "unfinished", "assignee": "", "limit": 20}, "title": "兜底：查看未完成任务", "risk": "low", "reason": "规则：无法识别"})
    return steps


def _call_llm(key: str, task: str, timeout: int = 60) -> dict:
    payload = {
        "model": MODEL, "temperature": 0, "max_tokens": 1200,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT.format(tools=json.dumps(TOOL_SPECS, ensure_ascii=False, indent=2))},
            {"role": "user", "content": f"任务：{task}"},
        ],
    }
    req = urllib.request.Request(API_URL, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return {"raw": body["choices"][0]["message"]["content"], "usage": body.get("usage", {})}


STATUS_OK = {"unfinished", "pending", "doing", "approved", "all"}


def _validate(steps: list) -> tuple[list[dict], list[str]]:
    problems: list[str] = []
    out: list[dict] = []
    if not isinstance(steps, list):
        return [], ["steps 不是数组"]
    steps = steps[:4] if len(steps) > 4 else steps
    if len(steps) == 4 and len(out) == 0:
        pass
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict) or s.get("tool") not in TOOL_SPECS:
            problems.append(f"第{i}步工具非法: {s.get('tool') if isinstance(s, dict) else s}")
            continue
        args = s.get("args") or {}
        if not isinstance(args, dict):
            problems.append(f"第{i}步 args 不是对象")
            continue
        tool = s["tool"]
        need = TOOL_SPECS[tool]["args"]
        missing = [k for k in need if k not in args]
        if missing:
            problems.append(f"第{i}步缺参数: {missing}")
            continue
        args = {k: v for k, v in args.items() if k in need}
        if tool in ("task_summary_by_assignee", "query_tasks"):
            st = str(args.get("status", "unfinished")).lower()
            if st not in STATUS_OK:
                st = "unfinished"
            args["status"] = st
        if tool == "query_tasks":
            try:
                args["limit"] = max(1, min(int(args.get("limit", 50)), 100))
            except Exception:
                args["limit"] = 50
            args["assignee"] = str(args.get("assignee", "") or "")
        if tool == "write_file" and not str(args.get("path", "")).strip():
            args["path"] = "summary.md"
        if tool == "send_file" and not str(args.get("target", "")).strip():
            args["target"] = "老板"
        risk = "high" if (tool == "send_file" or any(w in str(s.get("title", "")) for w in HIGH_RISK_WORDS)) else "low"
        out.append({"id": i, "tool": tool, "args": args,
                    "title": str(s.get("title") or f"步骤{i}：{tool}"), "risk": risk,
                    "reason": str(s.get("reason", ""))[:40]})
    return out, problems


def llm_plan(task: str, key: str | None = None, retries: int = 2) -> tuple[list[dict] | None, dict]:
    key = key or load_api_key()
    meta = {"source": "llm", "model": MODEL, "attempts": 0, "problems": [], "unsupported": None, "usage": {}}
    if not key:
        meta["source"] = "rule(fallback:no-key)"
        return None, meta
    for attempt in range(1, retries + 2):
        meta["attempts"] = attempt
        try:
            res = _call_llm(key, task)
            data = json.loads(res["raw"])
            meta["usage"] = res.get("usage", {})
            if data.get("unsupported"):
                meta["unsupported"] = str(data["unsupported"])
                meta["source"] = "llm(unsupported)"
                return [], meta
            steps, problems = _validate(data.get("steps", []))
            meta["problems"] = problems
            if problems and not steps:
                raise ValueError("; ".join(problems))
            return steps, meta
        except Exception as e:
            meta["last_error"] = f"{type(e).__name__}: {e}"
            if attempt <= retries:
                time.sleep(1.2 * attempt)
    meta["source"] = "rule(fallback:llm-error)"
    return None, meta


def make_plan(task: str, mode: str = "auto") -> tuple[list[dict], dict]:
    mode = (mode or "auto").lower()
    if mode == "rule":
        return rule_plan(task), {"source": "rule", "model": "", "attempts": 0, "problems": [], "unsupported": None}
    steps, meta = llm_plan(task)
    if steps is None:
        meta["fallback_steps"] = len(rule_plan(task))
        return rule_plan(task), meta
    return steps, meta
