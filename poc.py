#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AgentOS P1 · 真实链路 POC：真实库只读 + LLM 规划 + 人工审批 + dry-run 发送 + 中断恢复。

链路：一句话任务 → DeepSeek Planner 出步骤 → 只读查 agent_im.db → 写汇总报告 →（审批）→ dry-run 发送。
证据：evidence/ 下每次运行的原始输出（含 exit code）。

用法：
  python poc.py run    --task "查一下本周未完成的 tasks，按负责人汇总后发给我"
  python poc.py resume --thread <id> --decision approve|reject
  python poc.py state  --thread <id>
  python poc.py demo
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from planner import make_plan
from real_tools import TOOLS, db_path

ROOT = os.path.dirname(os.path.abspath(__file__))
CKPT = os.path.join(ROOT, "checkpoints.sqlite")
EVID = os.path.join(ROOT, "evidence")
AUDIT = os.path.join(ROOT, "outbox", "audit.log")


def ts() -> str:
    import datetime
    return datetime.datetime.now().strftime("%H:%M:%S")


class State(TypedDict, total=False):
    task: str
    steps: list[dict]
    cursor: int
    logs: list[str]
    results: list[dict]
    status: str
    result: str


def plan_node(state: State) -> dict:
    task = state.get("task", "")
    mode = os.environ.get("P1_PLANNER", "auto")
    steps, meta = make_plan(task, mode)
    if not steps and meta.get("unsupported"):
        note = f"不支持：{meta['unsupported']}"
        return {"steps": [], "cursor": 0, "status": "unsupported",
                "logs": [f"[{ts()}] 计划（{meta.get('source')}）：不执行 —— {note}"], "result": note}
    detail = f"{meta.get('source')}/{meta.get('model','-')}"
    if meta.get("problems"):
        detail += f"（校验提示：{'; '.join(meta['problems'])}）"
    if meta.get("fallback_steps"):
        detail += f"（LLM 失败回退规则：{meta.get('last_error')}）"
    logs = [f"[{ts()}] 计划（{detail}）：{len(steps)} 步 -> " + "；".join(s["title"] for s in steps),
            f"[{ts()}] 数据源（只读）：{db_path()}"]
    return {"steps": steps, "cursor": 0, "logs": logs, "results": [], "status": "running"}


def execute_node(state: State) -> dict:
    steps = state.get("steps", [])
    cursor = state.get("cursor", 0)
    logs = list(state.get("logs", []))
    if cursor >= len(steps):
        return {"status": "done"}
    step = steps[cursor]
    if step.get("risk") == "high":
        decision = interrupt({
            "type": "approval_request", "step": step["id"], "title": step["title"],
            "tool": step["tool"], "args": step["args"],
            "question": "是否批准执行？approve=批准 / reject=拒绝（当前为 dry-run 发送，不会真实外发）",
        })
        decision = str(decision or "").strip().lower()
        logs.append(f"[{ts()}] 人工确认：{decision or '(空)'} <- 步骤 {step['id']} {step['title']}")
        if decision not in ("approve", "approved", "y", "yes"):
            logs.append(f"[{ts()}] 已拒绝，流程终止（未执行任何动作）")
            return {"logs": logs, "status": "rejected", "result": "用户拒绝，未执行"}
    results = list(state.get("results", []))
    args = dict(step["args"])
    # write_file：LLM 只能给模板（它看不到工具结果），报告正文用本轮的查询结果自动合成
    if step["tool"] == "write_file" and results:
        body = [f"# {state.get('task', '任务')} · 汇总报告", "",
                f"> 生成时间：{ts()}｜数据源：{db_path()}（只读）", "",
                "## 一、执行结果", ""]
        for r in results:
            body += [f"### {r['title']}", "", r["output"], ""]
        body += ["## 二、说明", "", "- 本报告由 AgentOS P1 自动生成：LLM 负责规划，数据来自只读查询。", ""]
        args["content"] = "\n".join(body)
        logs.append(f"[{ts()}] 报告正文由执行结果自动合成（LLM 只决定写哪个文件）")
    if step["tool"] == "send_file":
        logs.append(f"[{ts()}] 投递通道：{os.environ.get('P1_SEND_CHANNEL', 'auto')}（auto=优先 telegram，失败回退 local；离线也能送达）")
    fn = TOOLS[step["tool"]]
    try:
        out = fn(**args)
        out_text = str(out)
        preview = out_text if len(out_text) <= 600 else out_text[:600] + "…"
        logs.append(f"[{ts()}] 执行 {step['tool']}：{step['title']}\n    {preview}")
        if step["tool"] == "write_file":
            logs.append(f"[{ts()}] 产出文件：workspace/{args.get('path')}")
        if step["tool"] in ("task_summary_by_assignee", "query_tasks"):
            results.append({"tool": step["tool"], "title": step["title"], "output": out_text})
    except Exception as e:
        logs.append(f"[{ts()}] 步骤 {step['id']} 失败：{e}")
        return {"logs": logs, "status": "error", "result": str(e)}
    return {"logs": logs, "results": results, "cursor": cursor + 1}


def report_node(state: State) -> dict:
    status = state.get("status", "")
    if status not in ("rejected", "error", "unsupported"):
        status = "done"
    logs = state.get("logs", [])
    summary = f"任务「{state.get('task','')}」结束：{status}，共 {len(state.get('steps',[]))} 步"
    return {"logs": logs + [f"[{ts()}] {summary}"], "result": summary, "status": status}


def plan_route(state: State) -> str:
    return "report" if state.get("status") == "unsupported" else "execute"


def route(state: State) -> str:
    if state.get("status") in ("rejected", "error", "done", "unsupported"):
        return "report"
    if state.get("cursor", 0) >= len(state.get("steps", [])):
        return "report"
    return "execute"


def build_graph(checkpointer=None):
    g = StateGraph(State)
    g.add_node("plan", plan_node)
    g.add_node("execute", execute_node)
    g.add_node("report", report_node)
    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", plan_route, {"execute": "execute", "report": "report"})
    g.add_conditional_edges("execute", route, {"execute": "execute", "report": "report"})
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer)


def open_graph():
    con = sqlite3.connect(CKPT, check_same_thread=False)
    saver = SqliteSaver(con)
    try:
        saver.setup()
    except Exception:
        pass
    return build_graph(saver), con


def cmd_run(args):
    graph, con = open_graph()
    try:
        cfg = {"configurable": {"thread_id": args.thread}}
        out = graph.invoke({"task": args.task}, cfg)
        if "__interrupt__" in out:
            intr = out["__interrupt__"][0].value
            print(json.dumps({"status": "interrupted", "thread": args.thread, "pending": intr,
                              "logs": out.get("logs", [])}, ensure_ascii=False, indent=2))
            print(f"\n[提示] 新进程恢复：python poc.py resume --thread {args.thread} --decision approve")
            sys.exit(2)
        print("\n".join(out.get("logs", [])))
        print(f"\n结果：{out.get('result','')}（status={out.get('status')}）")
    finally:
        con.close()


def cmd_resume(args):
    graph, con = open_graph()
    try:
        cfg = {"configurable": {"thread_id": args.thread}}
        out = graph.invoke(Command(resume=args.decision), cfg)
        print("\n".join(out.get("logs", [])))
        print(f"\n结果：{out.get('result','')}（status={out.get('status')}）")
        if os.path.exists(AUDIT):
            print("\n审计日志（outbox/audit.log）：")
            print(open(AUDIT, encoding="utf-8").read().strip()[-800:])
    finally:
        con.close()


def cmd_state(args):
    graph, con = open_graph()
    try:
        snap = graph.get_state({"configurable": {"thread_id": args.thread}})
        v = snap.values
        print(json.dumps({"thread": args.thread, "next": list(snap.next), "status": v.get("status"),
                          "cursor": v.get("cursor"), "steps": [s["title"] for s in v.get("steps", [])],
                          "logs": v.get("logs", [])}, ensure_ascii=False, indent=2))
    finally:
        con.close()


def _cap(cmd):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([sys.executable, os.path.abspath(__file__)] + cmd, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=env)
    return p.returncode, (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")


def cmd_demo(args):
    os.makedirs(EVID, exist_ok=True)
    results = []

    def save(name, code, text):
        with open(os.path.join(EVID, name), "w", encoding="utf-8") as f:
            f.write(text)
        results.append((name, code))

    # 场景 A：真实汇总 → 写报告 → 审批 → dry-run 发送（跨进程）
    tA = "p1A-" + uuid.uuid4().hex[:6]
    code, out = _cap(["run", "--task", "查一下本周未完成的 tasks，按负责人汇总后发给我", "--thread", tA])
    save("01_llm_plan_interrupt.txt", code, out + f"\n\n[exit={code}]  ← 期望 2：发送前中断等审批")
    code2, out2 = _cap(["resume", "--thread", tA, "--decision", "approve"])
    save("02_resume_approve_dryrun.txt", code2, out2 + f"\n\n[exit={code2}]  ← 新进程批准并 dry-run 发送")

    # 场景 B：只读明细查询（自动完成，不审批）
    tB = "p1B-" + uuid.uuid4().hex[:6]
    code3, out3 = _cap(["run", "--task", "查看未完成任务的明细", "--thread", tB])
    save("03_readonly_query.txt", code3, out3 + f"\n\n[exit={code3}]")

    # 场景 C：工具集做不到的任务 → 拒绝执行
    tC = "p1C-" + uuid.uuid4().hex[:6]
    code4, out4 = _cap(["run", "--task", "删除所有已完成的任务", "--thread", tC])
    save("04_unsupported_refuse.txt", code4, out4 + f"\n\n[exit={code4}]  ← 期望 0：识别不支持并拒绝")

    # 场景 D：状态可查
    code5, out5 = _cap(["state", "--thread", tA])
    save("05_state_after_resume.txt", code5, out5 + f"\n\n[exit={code5}]")

    print("P1 演示完成，证据写入 evidence/：")
    for n, c in results:
        print(f"  - {n} (exit={c})")
    print("\n提示：A 场景由两个独立进程完成（01 中断 → 02 恢复），验证跨进程 checkpoint。")


def main():
    ap = argparse.ArgumentParser(description="AgentOS P1 真实链路 POC")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--task", required=True)
    r.add_argument("--thread", default=None)
    r.add_argument("--planner", default=os.environ.get("P1_PLANNER", "auto"), choices=["auto", "llm", "rule"])
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("resume")
    s.add_argument("--thread", required=True)
    s.add_argument("--decision", required=True, choices=["approve", "reject"])
    s.set_defaults(fn=cmd_resume)
    st = sub.add_parser("state")
    st.add_argument("--thread", required=True)
    st.set_defaults(fn=cmd_state)
    d = sub.add_parser("demo")
    d.set_defaults(fn=cmd_demo)
    args = ap.parse_args()
    mode = getattr(args, "planner", None) or os.environ.get("P1_PLANNER", "auto")
    os.environ["P1_PLANNER"] = mode
    if getattr(args, "thread", None) is None and args.cmd == "run":
        args.thread = "p1-" + uuid.uuid4().hex[:8]
    print(f"[planner={mode}]", file=sys.stderr)
    args.fn(args)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
