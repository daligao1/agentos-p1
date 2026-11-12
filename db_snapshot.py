#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读快照：用于验收时证明"运行前后数据库没被改"。

输出 JSON：表行数、integrity_check、文件大小、修改时间、data_version、sha256（能读到时）。
用法：python db_snapshot.py [db_path]
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT = os.path.join(ROOT, "..", "..", "..", "tools", "agent-im", "data", "agent_im.db")
TABLES = ("tasks", "cards", "members", "groups", "messages", "budget", "events")


def main() -> None:
    db = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else DEFAULT)
    out = {"db": db, "exists": os.path.exists(db)}
    if os.path.exists(db):
        st = os.stat(db)
        out.update({"size": st.st_size, "mtime_ns": st.st_mtime_ns})
        try:
            with open(db, "rb") as f:
                out["sha256"] = hashlib.sha256(f.read()).hexdigest()
        except OSError as e:
            out["sha256"] = None
            out["sha256_error"] = f"{type(e).__name__}: {e}"
        try:
            con = sqlite3.connect("file:" + db + "?mode=ro", uri=True, timeout=5)
            con.execute("PRAGMA query_only = 1")
            out["data_version"] = con.execute("PRAGMA data_version").fetchone()[0]
            out["integrity"] = con.execute("PRAGMA integrity_check").fetchone()[0]
            out["counts"] = {}
            for t in TABLES:
                try:
                    out["counts"][t] = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                except sqlite3.Error as e:
                    out["counts"][t] = f"ERR:{e}"
            con.close()
        except sqlite3.Error as e:
            out["sqlite_error"] = str(e)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
