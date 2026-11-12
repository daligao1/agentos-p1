"""离线单元测试（CI 用，不联网、不需要真实 agent_im.db）。"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile

# --- 在 import 被测模块之前准备独立测试库与投递通道 ---
_TMP = tempfile.mkdtemp(prefix="agentos-test-")
_DB = os.path.join(_TMP, "agent_im.db")
_con = sqlite3.connect(_DB)
_con.executescript(
    """
    CREATE TABLE members(id TEXT PRIMARY KEY, name TEXT, role TEXT, dept TEXT, token TEXT, created_at TEXT);
    CREATE TABLE tasks(id INTEGER PRIMARY KEY, card_id TEXT, group_id TEXT, title TEXT, assignee TEXT,
                       status TEXT, progress INTEGER, planned_start TEXT, planned_end TEXT,
                       sort INTEGER, created_at TEXT, updated_at TEXT);
    """
)
_con.executemany(
    "INSERT INTO members(id,name,role,dept,token,created_at) VALUES(?,?,?,?,?,?)",
    [("g1", "小组A·研究", "worker", "小组A", "t1", "2026-01-01"),
     ("g2", "小组B·开发", "worker", "小组B", "t2", "2026-01-01")],
)
_con.executemany(
    "INSERT INTO tasks(id,card_id,group_id,title,assignee,status,progress,planned_start,planned_end,sort,created_at,updated_at)"
    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
    [
        (1, "T-1", "G", "任务1", "g1", "pending", 0, "2026-11-08", "2026-11-10", 0, "x", "x"),
        (2, "T-1", "G", "任务2", "g1", "pending", 0, "2026-11-08", "2026-11-10", 1, "x", "x"),
        (3, "T-2", "G", "任务3", "g2", "doing", 50, "2026-11-08", "2026-11-10", 2, "x", "x"),
        (4, "T-2", "G", "任务4", "g2", "doing", 30, "2026-11-08", "2026-11-10", 3, "x", "x"),
        (5, "T-3", "G", "任务5", "g2", "approved", 100, "2026-11-08", "2026-11-10", 4, "x", "x"),
    ],
)
_con.commit()
_con.close()
os.environ["AGENT_DB"] = _DB
os.environ["P1_SEND_CHANNEL"] = "local"

import real_tools  # noqa: E402
from planner import _validate, rule_plan  # noqa: E402

real_tools.INBOX = os.path.join(_TMP, "inbox")
real_tools.OUTBOX = os.path.join(_TMP, "outbox")
real_tools.AUDIT = os.path.join(real_tools.OUTBOX, "audit.log")


def test_rule_plan_summary() -> None:
    steps = rule_plan("查一下未完成的 tasks，按负责人汇总后发给我")
    tools = [s["tool"] for s in steps]
    assert "task_summary_by_assignee" in tools
    assert any(s["risk"] == "high" for s in steps), "发送类步骤必须标为 high"


def test_validate_rejects_unknown_tool() -> None:
    steps, problems = _validate([{"tool": "rm_rf", "args": {}, "title": "删除"}])
    assert steps == []
    assert problems


def test_validate_forces_high_risk_on_send() -> None:
    steps, problems = _validate([
        {"tool": "send_file", "args": {"target": "老板", "path": "a.md"}, "title": "发送", "risk": "low"}
    ])
    assert not problems
    assert steps[0]["risk"] == "high"


def test_summary_by_assignee() -> None:
    out = real_tools.task_summary_by_assignee("unfinished")
    assert "共 4 条" in out
    assert "小组B·开发" in out and "| 3 |" in out
    assert "小组A·研究" in out and "| 2 |" in out


def test_query_tasks_returns_rows() -> None:
    out = real_tools.query_tasks("unfinished", "", 10)
    assert "返回 4 条" in out


def test_write_and_deliver_local() -> None:
    msg = real_tools.write_file("report.md", "# 报告\n")
    assert "已写入" in msg
    out = real_tools.send_file("老板", "report.md")
    assert "local" in out
    files = os.listdir(real_tools.INBOX)
    assert files, "收件箱应有投递文件"
    with open(real_tools.AUDIT, encoding="utf-8") as f:
        assert "[local]" in f.read()


def test_db_unchanged_after_reads() -> None:
    before = hashlib.sha256(open(_DB, "rb").read()).hexdigest()
    real_tools.task_summary_by_assignee("all")
    real_tools.query_tasks("all", "", 10)
    after = hashlib.sha256(open(_DB, "rb").read()).hexdigest()
    assert before == after, "只读工具不应修改数据库"
