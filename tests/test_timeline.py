"""Tests for timeline reconstruction from tool operations."""
from __future__ import annotations

import sqlite3
import json
from pathlib import Path
from bugbench.timeline import Timeline, Anchor, Edit, _strip_read, load_all_timelines


def test_strip_read_clean():
    output = "<path>/a.py</path>\n<type>file</type>\n<content>\n1: def f():\n2:     return 42\n\n(Showing lines 1-2 of 2. file ends)\n</content>"
    got = _strip_read(output)
    assert got == "def f():\n    return 42"


def test_strip_read_rejects_truncated():
    output = "<path>/a.py</path>\n<content>\n1: def f():\n\n(Showing lines 1-50 of 200. file ends)\n</content>"
    got = _strip_read(output)
    assert got is None


def test_timeline_replay():
    tl = Timeline(file_path="foo.py")
    tl.anchors.append(Anchor(100, "write", "def f(): return 1\n"))
    tl.edits.append(Edit(200, "return 1", "return 2", "sess1", "foo.py"))
    
    states = tl.states()
    assert len(states) == 1
    assert states[0].before == "def f(): return 1\n"
    assert states[0].after == "def f(): return 2\n"


def test_load_all_timelines():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE part (data TEXT, time_created INTEGER, session_id TEXT)")
    
    write_payload = {
        "type": "tool",
        "tool": "write",
        "state": {
            "input": {
                "filePath": "/workspace/mod.py",
                "content": "x = 1\n"
            }
        }
    }
    conn.execute("INSERT INTO part VALUES (?, 100, 's1')", (json.dumps(write_payload),))
    
    edit_payload = {
        "type": "tool",
        "tool": "edit",
        "state": {
            "input": {
                "filePath": "/workspace/mod.py",
                "oldString": "x = 1",
                "newString": "x = 2"
            }
        }
    }
    conn.execute("INSERT INTO part VALUES (?, 200, 's1')", (json.dumps(edit_payload),))
    
    timelines = load_all_timelines(conn)
    assert "/workspace/mod.py" in timelines
    tl = timelines["/workspace/mod.py"]
    assert len(tl.anchors) == 1
    assert len(tl.edits) == 1
