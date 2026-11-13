"""The scripted command-line run shows tasks spawning, continuing and writing audit rows."""
from __future__ import annotations

import sqlite3
import subprocess
import sys


def test_scripted_run(tmp_path):
    db = tmp_path / "demo.db"
    run = subprocess.run(
        [sys.executable, "-m", "harness.dev.cli", "--tasks", "12", "--db", str(db)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr
    out = run.stdout
    assert "after wait:  12 completed" in out
    assert "You asked 2 questions so far" in out  # the continuation restored its context
    assert "status awaiting_confirmation" in out
    assert "Done: the owner is now bob." in out
    assert "tool_call tool_result" in out and out.count("done") >= 1

    connection = sqlite3.connect(db)
    try:
        statuses = dict(connection.execute("SELECT status, count(*) FROM agent_tasks GROUP BY status"))
        efforts = {row[0] for row in connection.execute("SELECT reasoning_effort FROM llm_calls")}
        links = connection.execute(
            "SELECT count(*) FROM agent_tasks WHERE parent_task_id IS NOT NULL"
        ).fetchone()[0]
        unfinished = connection.execute(
            "SELECT count(*) FROM llm_calls WHERE status = 'started'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert statuses["completed"] >= 15 and statuses["awaiting_confirmation"] == 1
    assert efforts == {"low", "medium"}
    assert links == 2
    assert unfinished == 0
