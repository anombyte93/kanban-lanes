"""kanban_live.py: the read-only live view watcher agents run inside a Workflow.

Every subcommand runs against two real board schemas (tests/fixtures/hermes_schema_*.sql):
upstream Hermes Agent, and a fork that adds per-run model columns. Temp board and session
databases under a fake HERMES_HOME; no agent processes. The workflow tests run
workflows/kanban-watch.js under node with inert agents.
"""
import json
import argparse
import io
import os
import re
import runpy
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "kanban_live.py"
WORKFLOW = ROOT / "workflows" / "kanban-watch.js"
LIVE_PATH = '"${KANBAN_LANES_HOME:-$HOME/.claude/kanban-lanes}/scripts/kanban_live.py"'
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def make_board(path: Path, flavour: str) -> None:
    with sqlite3.connect(path) as con:
        con.executescript((FIXTURES / f"hermes_schema_{flavour}.sql").read_text())


def insert(con: sqlite3.Connection, table: str, **row) -> None:
    """Insert only the columns this schema has, so one row literal fits both schemas."""
    have = {r[1] for r in con.execute(f"pragma table_info({table})")}
    row = {k: v for k, v in row.items() if k in have}
    con.execute(f"insert into {table}({','.join(row)}) values({','.join('?' * len(row))})", tuple(row.values()))


def make_store(path: Path, sessions, messages, active_column=True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.execute("create table sessions(id text primary key, source text not null, title text,"
                    " started_at real not null)")
        con.execute("create table messages(id integer primary key autoincrement, session_id text not null,"
                    " role text not null, content text, tool_calls text, tool_name text, timestamp real not null"
                    + (", active integer not null default 1)" if active_column else ")"))
        con.executemany("insert into sessions values(?,?,?,?)", sessions)
        con.executemany("insert into messages(session_id, role, content, tool_calls, tool_name, timestamp)"
                        " values(?,?,?,?,?,?)", messages)


class KanbanLiveContract:
    """Run against both schemas; subclasses set FLAVOUR."""

    FLAVOUR = ""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.hermes = self.home / ".hermes"
        board = self.hermes / "kanban" / "boards" / "b1"
        board.mkdir(parents=True)
        self.board_db = board / "kanban.db"
        self.now = now = int(time.time())
        make_board(self.board_db, self.FLAVOUR)
        with sqlite3.connect(self.board_db) as con:
            insert(con, "task_links", parent_id="t_run", child_id="t_wait")
            insert(con, "task_events", task_id="t_wait", kind="created",
                   payload=json.dumps({"creator_task_id": "t_run"}), created_at=now)
            insert(con, "task_events", task_id="t_run", kind="heartbeat", created_at=now)
            for task in (
                dict(id="t_run", title="Running card", assignee="worker", status="running", created_at=now - 60,
                     started_at=now - 50, last_heartbeat_at=now, worker_pid=1, current_run_id=1),
                dict(id="t_wait", title="Queued card", assignee="worker", status="ready", created_at=now - 60),
                dict(id="t_done", title="Done card", assignee="worker", status="done", created_at=now - 60,
                     started_at=now - 50, completed_at=now - 5, current_run_id=2),
            ):
                insert(con, "tasks", **task)
            insert(con, "task_runs", id=1, task_id="t_run", status="running", model_used="model-large",
                   provider_used="provider-a", started_at=now - 50, summary="")
            insert(con, "task_runs", id=2, task_id="t_done", status="done", outcome="completed",
                   model_used="model-small", provider_used="provider-b", fallback_from="model-large",
                   started_at=now - 50, ended_at=now - 5, summary="ok")
        self.state_db = self.hermes / "profiles" / "worker" / "state.db"
        make_store(self.state_db, *self.store_rows(now))

    def store_rows(self, now):
        # How each flavour names a worker session; the first user turn is the dispatcher's prompt.
        if self.FLAVOUR == "fork":
            titles = {"t_run": "work kanban task t_run", "t_done": "work kanban task t_done"}
        else:
            titles = {"t_run": "Running card", "t_done": "Done card"}
        calls = json.dumps([{"function": {"name": "terminal", "arguments": "{\"command\": \"make test\"}"}}])
        sessions = [("s1", "kanban", titles["t_run"], now - 49), ("s2", "kanban", titles["t_done"], now - 49)]
        messages = [
            ("s1", "user", "work kanban task t_run", None, None, now),
            ("s1", "assistant", "Running the test suite.", calls, None, now),
            ("s1", "tool", "42 passed", None, "terminal", now),
            ("s2", "user", "work kanban task t_done", None, None, now),
            ("s2", "assistant", "All done.", None, None, now),
        ]
        return sessions, messages

    def live_env(self, **env_extra):
        env = dict(os.environ, HERMES_HOME=str(self.hermes), HOME=str(self.home))
        env.pop("XDG_CACHE_HOME", None)
        env.pop("HERMES_KANBAN_HOME", None)
        env.update(env_extra)
        return env

    def run_live(self, *args, **env_extra):
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=self.live_env(**env_extra),
                              capture_output=True, text=True, timeout=30)

    def add_events(self, task, *events):
        with sqlite3.connect(self.board_db) as con:
            for kind, payload in events:
                insert(con, "task_events", task_id=task, kind=kind,
                       payload=None if payload is None else json.dumps(payload), created_at=int(time.time()))

    def block(self, task, column_reason="Legacy block reason unknown"):
        """Mark a card blocked. The fork's triggers need a non-empty tasks.block_reason, so the
        fork gets ``column_reason`` (the migration placeholder by default); upstream has no column."""
        with sqlite3.connect(self.board_db) as con:
            if self.FLAVOUR == "fork":
                con.execute("update tasks set status = 'blocked', block_reason = ? where id = ?",
                            (column_reason, task))
            else:
                con.execute("update tasks set status = 'blocked' where id = ?", (task,))

    def snapshot_reason(self, task="t_run"):
        out = self.run_live("snapshot", "--board", "b1", "--task", task)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stderr, "")
        return json.loads(out.stdout)["block_reason"]

    # snapshot

    def test_snapshot_reads_the_model_that_ran_or_null(self):
        out = self.run_live("snapshot", "--board", "b1", "--task", "t_run")
        self.assertEqual(out.returncode, 0, out.stderr)
        card = json.loads(out.stdout)
        self.assertEqual(card["run"]["id"], 1)
        self.assertEqual(card["run"]["status"], "running")
        self.assertIn("model_used", card["run"])
        self.assertIn("provider_used", card["run"])
        self.assertIn("fallback_from", card["run"])
        expected = "model-large" if self.FLAVOUR == "fork" else None
        self.assertEqual(card["run"]["model_used"], expected)

    def test_snapshot_skips_heartbeats_for_last_event(self):
        card = json.loads(self.run_live("snapshot", "--board", "b1", "--task", "t_wait").stdout)
        self.assertEqual(card["last_event"]["kind"], "created")
        self.assertIsNone(json.loads(self.run_live("snapshot", "--board", "b1", "--task", "t_run").stdout)["last_event"])

    def test_snapshot_reads_block_reason_or_null(self):
        with sqlite3.connect(self.board_db) as con:
            insert(con, "tasks", id="t_blocked", title="Blocked card", assignee="worker", status="blocked",
                   created_at=self.now, block_reason="needs a person")
        card = json.loads(self.run_live("snapshot", "--board", "b1", "--task", "t_blocked").stdout)
        self.assertIn("block_reason", card)
        self.assertEqual(card["block_reason"], "needs a person" if self.FLAVOUR == "fork" else None)
        self.assertIsNone(json.loads(self.run_live("snapshot", "--board", "b1", "--task", "t_run").stdout)["block_reason"])

    def test_snapshot_block_reason_is_the_current_one_after_an_auto_block(self):
        # Created blocked, unblocked, claimed, then auto-blocked: the new reason lands in the
        # column (fork) and in the gave_up payload, never in a new `blocked` event.
        auto = "auto-blocked after 2 timed_out failure(s): elapsed 1821s > limit 1800s"
        self.block("t_run", auto)
        self.add_events("t_run", ("blocked", {"reason": "initial_status"}), ("unblocked", None),
                        ("claimed", {"run_id": 1}), ("timed_out", {}), ("gave_up", {"block_reason": auto}))
        self.add_events("t_done", ("blocked", {"reason": "stale reason"}))
        out = self.run_live("snapshot", "--board", "b1", "--task", "t_run", "--task", "t_done")
        self.assertEqual(out.returncode, 0, out.stderr)
        cards = [json.loads(line) for line in out.stdout.splitlines()]
        self.assertEqual(cards[0]["block_reason"], auto)
        self.assertIsNone(cards[1]["block_reason"])  # t_done is done: its old block is history
        # With the migration placeholder in the column, the gave_up payload still wins over
        # the creation-time block event.
        self.block("t_run")
        self.assertEqual(self.snapshot_reason(), auto)

    def test_snapshot_block_reason_skips_a_block_event_that_an_unblock_or_claim_made_stale(self):
        self.block("t_run")
        self.add_events("t_run", ("blocked", {"reason": "old reason"}), ("unblocked", None), ("claimed", {}))
        self.assertIsNone(self.snapshot_reason())
        self.add_events("t_run", ("blocked", {"reason": "latest reason"}))
        self.assertEqual(self.snapshot_reason(), "latest reason")

    def test_snapshot_keeps_a_long_block_reason_and_marks_a_cut(self):
        ask = "Please authorise a documented baseline-failure exception or arrange an independent repair. "
        self.block("t_run", ask * 6)
        self.add_events("t_run", ("blocked", {"reason": ask * 6}))
        self.assertEqual(self.snapshot_reason(), ask * 6)
        self.block("t_run", "x" * 2500)
        self.add_events("t_run", ("blocked", {"reason": "x" * 2500}))
        self.assertEqual(self.snapshot_reason(), "x" * 2000 + "… [cut: 2500 characters in full]")

    def test_snapshot_block_reason_falls_back_to_comment_then_null(self):
        # Upstream has no tasks.block_reason column: with no block event and no comment the
        # reason reads null, cleanly. The fork's placeholder reads the same.
        self.block("t_run")
        self.assertIsNone(self.snapshot_reason())
        with sqlite3.connect(self.board_db) as con:
            insert(con, "task_comments", task_id="t_run", author="worker", body="old comment", created_at=1)
            insert(con, "task_comments", task_id="t_run", author="worker", body="最新" * 400, created_at=2)
        self.assertEqual(self.snapshot_reason(), "最新" * 400)
        self.add_events("t_run", ("blocked", None))
        with sqlite3.connect(self.board_db) as con:
            con.execute("update task_events set payload = 'not JSON' where kind = 'blocked'")
        self.assertEqual(self.snapshot_reason(), "最新" * 400)
        if self.FLAVOUR == "fork":
            self.block("t_run", "column reason")
            self.assertEqual(self.snapshot_reason(), "column reason")

    def test_snapshot_missing_card_is_not_found(self):
        out = self.run_live("snapshot", "--board", "b1", "--task", "t_ghost")
        self.assertEqual(out.returncode, 4)
        self.assertIn("NOT_FOUND", out.stdout)

    # follow

    def test_follow_shows_the_worker_then_resumes_from_cursor(self):
        out = self.run_live("follow", "--board", "b1", "--task", "t_run", "--max-seconds", "1", "--interval", "1")
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertIn("session s1", out.stdout)
        self.assertIn("agent: Running the test suite.", out.stdout)
        self.assertIn("-> terminal", out.stdout)
        self.assertIn("<- terminal: 42 passed", out.stdout)
        again = self.run_live("follow", "--board", "b1", "--task", "t_run", "--max-seconds", "1", "--interval", "1")
        self.assertNotIn("Running the test suite.", again.stdout)
        other = self.run_live("follow", "--board", "b1", "--task", "t_run", "--max-seconds", "1",
                              "--interval", "1", "--cursor-key", "second-watcher")
        self.assertIn("Running the test suite.", other.stdout)

    def test_interrupted_compact_follow_replays_unemitted_batch_on_resume(self):
        command = ("follow", "--board", "b1", "--task", "t_run", "--compact",
                   "--cursor-key", "interrupted", "--max-seconds", "60", "--interval", "10")
        process = subprocess.Popen([sys.executable, str(SCRIPT), *command], env=self.live_env(),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(1)
            self.assertIsNone(process.poll(), "follow must still be buffering")
        finally:
            process.kill()
            stdout, stderr = process.communicate(timeout=10)
        self.assertIn("session s1", stdout, stderr)
        self.assertNotIn("Running the test suite.", stdout)
        resumed = self.run_live(*command, "--max-seconds", "0")
        self.assertEqual(resumed.returncode, 3, resumed.stderr)
        self.assertIn("agent: Running the test suite.", resumed.stdout)
        self.assertIn('→ terminal {"command": "make test"}', resumed.stdout)
        again = self.run_live(*command, "--max-seconds", "0")
        self.assertEqual(again.returncode, 3, again.stderr)
        self.assertNotIn("Running the test suite.", again.stdout)
        self.assertNotIn("→ terminal", again.stdout)

    def test_compact_follow_caps_bytes_and_consumes_hidden_messages(self):
        with sqlite3.connect(self.state_db) as con:
            for i in range(40):
                calls = json.dumps([{"function": {"name": "terminal", "arguments": f"tool-{i} " + "界" * 200}}])
                con.execute(
                    "insert into messages(session_id, role, content, tool_calls, timestamp) values(?,?,?,?,?)",
                    ("s1", "assistant", f"message-{i} " + "界" * 400, calls, time.time()))
                con.execute(
                    "insert into messages(session_id, role, content, tool_name, timestamp) values(?,?,?,?,?)",
                    ("s1", "tool", "tool result must disappear", "terminal", time.time()))
        command = ("follow", "--board", "b1", "--task", "t_run", "--compact", "--max-bytes", "1500",
                   "--backlog", "200", "--max-seconds", "0")
        out = self.run_live(*command)
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertLessEqual(len(out.stdout.encode()), 1500)
        lines = out.stdout.splitlines()
        footer = lines[-1]
        self.assertRegex(footer, r"^… \d+ lines not shown \(the cursor has moved past them\)$")
        hidden = int(footer.split()[1])
        # 41 tool calls, one collapsed-assistant count, one latest message, plus
        # the card/session/timeout lines. Every suppressed line is accounted for.
        self.assertEqual(hidden + len(lines) - 1, 46)
        # The cap drops the oldest tool calls, never the latest assistant text or the newest call.
        self.assertIn("40 earlier assistant messages collapsed", out.stdout)
        agent = [line for line in lines if "agent: " in line]
        self.assertEqual(len(agent), 1)
        self.assertIn("agent: message-39 ", agent[0])
        self.assertIn("→ terminal tool-39 ", out.stdout)
        self.assertNotIn("tool-0 ", out.stdout)
        self.assertNotIn("make test", out.stdout)
        self.assertLess(lines.index(next(line for line in lines if "tool-39" in line)), lines.index(agent[0]))
        self.assertNotIn("tool result must disappear", out.stdout)
        self.assertNotIn("42 passed", out.stdout)
        again = self.run_live(*command)
        self.assertEqual(again.returncode, 3, again.stderr)
        self.assertNotIn("→ terminal", again.stdout)
        self.assertNotIn("message-", again.stdout)

    def test_compact_follow_keeps_latest_assistant_and_collapses_older_text(self):
        with sqlite3.connect(self.state_db) as con:
            con.execute(
                "insert into messages(session_id, role, content, timestamp) values(?,?,?,?)",
                ("s1", "assistant", "Latest line\n" + "x" * 500, time.time()))
        out = self.run_live("follow", "--board", "b1", "--task", "t_run", "--compact", "--max-seconds", "0")
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertIn('→ terminal {"command": "make test"}', out.stdout)
        self.assertIn("1 earlier assistant messages", out.stdout)
        self.assertNotIn("Running the test suite.", out.stdout)
        self.assertNotIn("<-", out.stdout)
        assistant = next(line for line in out.stdout.splitlines() if "agent: " in line)
        self.assertEqual(assistant.split("agent: ", 1)[1], ("Latest line " + "x" * 500)[:300])

    def test_verbose_follow_is_byte_identical_to_the_default_output(self):
        default = self.run_live("follow", "--board", "b1", "--task", "t_done", "--from-start")
        verbose = self.run_live("follow", "--board", "b1", "--task", "t_done", "--from-start", "--verbose")
        self.assertEqual(verbose.returncode, 0, verbose.stderr)
        self.assertEqual(verbose.stdout, default.stdout)
        with sqlite3.connect(self.state_db) as con:
            stamp = con.execute("select timestamp from messages where session_id = 's2'"
                                " and role = 'assistant'").fetchone()[0]
        model = "model-small (fallback from model-large)" if self.FLAVOUR == "fork" else "?"
        card = (f"t_done [done] Done card | worker on {model}"
                f" | started {time.strftime('%H:%M:%S', time.localtime(self.now - 50))}")
        self.assertEqual(verbose.stdout, "\n".join([
            card, f"session s2 in {self.state_db}",
            f"[{time.strftime('%H:%M:%S', time.localtime(stamp))}] agent: All done.",
            "FINAL " + card, "",
        ]))

    def test_compact_drops_heartbeat_tool_calls(self):
        with sqlite3.connect(self.state_db) as con:
            calls = json.dumps([{"function": {"name": "kanban_heartbeat", "arguments": "heartbeat note"}}])
            con.execute(
                "insert into messages(session_id, role, tool_calls, timestamp) values(?,?,?,?)",
                ("s1", "assistant", calls, time.time()))
        out = self.run_live("follow", "--board", "b1", "--task", "t_run", "--compact", "--max-seconds", "0")
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertIn("→ terminal", out.stdout)
        self.assertNotIn("heartbeat", out.stdout)

    def test_follow_byte_cap_preserves_final_even_at_minimum_budget(self):
        for mode in ("--compact", "--verbose"):
            with self.subTest(mode=mode):
                out = self.run_live("follow", "--board", "b1", "--task", "t_done", mode,
                                    "--from-start", "--max-bytes", "128")
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertLessEqual(len(out.stdout.encode()), 128)
                self.assertIn("FINAL t_done [done]", out.stdout)
                self.assertIn("3 lines not shown", out.stdout)

    def test_follow_rejects_a_byte_cap_below_the_status_reserve(self):
        out = self.run_live("follow", "--board", "b1", "--task", "t_done", "--max-bytes", "127")
        self.assertEqual(out.returncode, 2)
        self.assertIn("--max-bytes must be at least 128", out.stderr)

    def test_compact_follow_holds_the_compression_note_and_continues_in_the_child(self):
        with sqlite3.connect(self.state_db) as con:
            con.execute("alter table sessions add column parent_session_id text")
            con.execute("alter table sessions add column end_reason text")
            con.execute("update sessions set end_reason = 'compression' where id = 's1'")
            con.execute("insert into sessions(id, source, title, started_at, parent_session_id)"
                        " values('s1c', 'kanban', null, ?, 's1')", (self.now - 20,))
            con.execute("insert into messages(session_id, role, content, tool_calls, tool_name, timestamp)"
                        " values('s1c', 'assistant', 'Carrying on after compression.', null, null, ?)", (self.now,))
        out = self.run_live("follow", "--board", "b1", "--task", "t_run", "--compact", "--max-seconds", "0")
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertIn("session s1 continues in s1c", out.stdout)
        self.assertIn("agent: Carrying on after compression.", out.stdout)
        self.assertIn("1 earlier assistant messages collapsed", out.stdout)

    def test_follow_exits_zero_on_a_terminal_card(self):
        out = self.run_live("follow", "--board", "b1", "--task", "t_done", "--max-seconds", "5", "--interval", "1")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("FINAL t_done [done]", out.stdout)
        if self.FLAVOUR == "fork":
            self.assertIn("on model-small (fallback from model-large)", out.stdout)
        else:
            self.assertIn("on ?", out.stdout)

    def test_follow_cursor_lives_under_xdg_cache(self):
        xdg = self.home / "xdg"
        self.run_live("follow", "--board", "b1", "--task", "t_run", "--max-seconds", "1", "--interval", "1",
                      XDG_CACHE_HOME=str(xdg))
        self.assertTrue((xdg / "kanban-lanes" / "live" / "b1-t_run.cursor").exists())
        self.run_live("follow", "--board", "b1", "--task", "t_done", "--max-seconds", "1", "--interval", "1")
        self.assertTrue((self.home / ".cache" / "kanban-lanes" / "live" / "b1-t_done.cursor").exists())

    def test_follow_without_a_session_store_still_tracks_the_card(self):
        self.state_db.unlink()
        out = self.run_live("follow", "--board", "b1", "--task", "t_run", "--max-seconds", "1", "--interval", "1")
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertIn("no session store for worker", out.stdout)
        done = self.run_live("follow", "--board", "b1", "--task", "t_done", "--max-seconds", "5", "--interval", "1")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("FINAL t_done [done]", done.stdout)

    def test_follow_reads_a_store_without_the_active_column(self):
        self.state_db.unlink()
        make_store(self.state_db, *self.store_rows(self.now), active_column=False)
        out = self.run_live("follow", "--board", "b1", "--task", "t_run", "--max-seconds", "1", "--interval", "1")
        self.assertIn("agent: Running the test suite.", out.stdout)

    def test_follow_ignores_the_session_of_an_earlier_run(self):
        # Run 1 crashed 140s ago; run 2 started 10s ago and has not written its session yet.
        with sqlite3.connect(self.board_db) as con:
            con.execute("update task_runs set status = 'crashed', started_at = ?, ended_at = ? where id = 1",
                        (self.now - 200, self.now - 140))
            insert(con, "task_runs", id=3, task_id="t_run", status="running", started_at=self.now - 10, summary="")
            con.execute("update tasks set current_run_id = 3, started_at = ? where id = 't_run'", (self.now - 10,))
        title = "work kanban task t_run" if self.FLAVOUR == "fork" else "Running card"
        old = [("s_old", "kanban", title, self.now - 199)]
        old_msgs = [("s_old", "user", "work kanban task t_run", None, None, self.now - 199),
                    ("s_old", "assistant", "Run one transcript.", None, None, self.now - 150)]
        self.state_db.unlink()
        make_store(self.state_db, old, old_msgs)
        out = self.run_live("follow", "--board", "b1", "--task", "t_run", "--max-seconds", "1", "--interval", "1")
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertNotIn("s_old", out.stdout)
        self.assertNotIn("Run one transcript.", out.stdout)
        self.state_db.unlink()
        make_store(self.state_db, old + [("s_new", "kanban", title, self.now - 9)],
                   old_msgs + [("s_new", "user", "work kanban task t_run", None, None, self.now - 9),
                               ("s_new", "assistant", "Run two transcript.", None, None, self.now - 8)])
        out = self.run_live("follow", "--board", "b1", "--task", "t_run", "--max-seconds", "1", "--interval", "1")
        self.assertIn("session s_new", out.stdout)
        self.assertIn("Run two transcript.", out.stdout)
        self.assertNotIn("Run one transcript.", out.stdout)

    def test_follow_moves_to_the_session_compression_rotated_to(self):
        with sqlite3.connect(self.state_db) as con:
            con.execute("alter table sessions add column parent_session_id text")
            con.execute("alter table sessions add column end_reason text")
            con.execute("update sessions set end_reason = 'compression' where id = 's1'")
            con.execute("insert into sessions(id, source, title, started_at, parent_session_id)"
                        " values('s1c', 'kanban', null, ?, 's1')", (self.now - 20,))
            con.executemany("insert into messages(session_id, role, content, tool_calls, tool_name, timestamp)"
                            " values(?,?,?,?,?,?)",
                            [("s1c", "user", "Summary of the earlier turns.", None, None, self.now),
                             ("s1c", "assistant", "Carrying on after compression.", None, None, self.now)])
        out = self.run_live("follow", "--board", "b1", "--task", "t_run", "--max-seconds", "1", "--interval", "1")
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertIn("agent: Running the test suite.", out.stdout)
        self.assertIn("session s1 continues in s1c", out.stdout)
        self.assertIn("agent: Carrying on after compression.", out.stdout)

    def test_follow_survives_a_store_without_a_messages_table(self):
        self.state_db.unlink()
        self.state_db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.state_db) as con:
            con.execute("create table sessions(id text primary key, source text not null, title text,"
                        " started_at real not null)")
            title = "work kanban task t_run" if self.FLAVOUR == "fork" else "Running card"
            con.execute("insert into sessions values('s1', 'kanban', ?, ?)", (title, self.now - 49))
        out = self.run_live("follow", "--board", "b1", "--task", "t_run", "--max-seconds", "2", "--interval", "1")
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertNotIn("Traceback", out.stderr)
        self.assertEqual(out.stdout.count("has no messages table"), 1)

    # wait-start

    def test_wait_start_times_out_while_queued(self):
        out = self.run_live("wait-start", "--board", "b1", "--task", "t_wait", "--max-seconds", "1", "--interval", "1")
        self.assertEqual(out.returncode, 3)
        self.assertIn("t_wait [ready] Queued card", out.stdout)
        self.assertEqual(self.run_live("wait-start", "--board", "b1", "--task", "t_run").returncode, 0)

    def test_polling_calls_never_sleep_past_their_deadline(self):
        # A 1-second budget with a 5-second interval must end near 1 second, not after 5.
        for command in (("wait-start", "--task", "t_wait"),
                        ("follow", "--task", "t_run", "--compact"),
                        ("events", "--watch", "t_run=running", "--since", str(int(time.time()) + 10))):
            with self.subTest(command=command[0]):
                started = time.monotonic()
                out = self.run_live(command[0], "--board", "b1", *command[1:], "--max-seconds", "1", "--interval", "5")
                self.assertEqual(out.returncode, 3, out.stderr)
                self.assertLess(time.monotonic() - started, 4)

    def test_compact_follow_prints_the_quiet_line(self):
        out = self.run_live("follow", "--board", "b1", "--task", "t_run", "--compact",
                            "--quiet-warn", "1", "--max-seconds", "3", "--interval", "1")
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertIn("no new worker messages\n", out.stdout)

    def test_compact_quiet_signal_survives_a_full_or_minimum_byte_cap(self):
        for cap in (128, 1500):
            out = self.run_live("follow", "--board", "b1", "--task", "t_run", "--compact",
                                "--quiet-warn", "0", "--max-seconds", "1", "--interval", "1",
                                "--from-start", "--max-bytes", str(cap))
            self.assertEqual(out.returncode, 3, out.stderr)
            self.assertIn("no new worker messages\n", out.stdout)
            self.assertIn("still running", out.stdout)
            self.assertLessEqual(len(out.stdout.encode()), cap)

    # scout

    def test_scout_reports_new_cards_and_quiescence(self):
        since = str(int(time.time()) - 3600)
        out = self.run_live("scout", "--board", "b1", "--known", "t_run", "--since", since, "--max-seconds", "1")
        self.assertEqual(out.returncode, 0)
        self.assertEqual({c["id"] for c in json.loads(out.stdout)["new"]}, {"t_wait", "t_done"})
        out = self.run_live("scout", "--board", "b1", "--known", "t_done", "--since", str(int(time.time()) + 10),
                            "--max-seconds", "1")
        self.assertEqual(out.returncode, 2)

    def test_never_writes_to_the_board_or_session_store(self):
        before = (self.board_db.stat().st_mtime_ns, self.state_db.stat().st_mtime_ns)
        self.run_live("follow", "--board", "b1", "--task", "t_run", "--max-seconds", "1", "--interval", "1")
        self.run_live("scout", "--board", "b1", "--known", "t_run", "--since", "0", "--max-seconds", "1")
        self.run_live("cards", "--board", "b1")
        self.assertEqual(before, (self.board_db.stat().st_mtime_ns, self.state_db.stat().st_mtime_ns))

    # active / cards

    def test_active_lists_queued_and_running_only(self):
        out = self.run_live("active", "--board", "b1")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual({c["id"] for c in json.loads(out.stdout)["cards"]}, {"t_run", "t_wait"})

    def test_cards_carry_parents_creator_and_model(self):
        out = self.run_live("cards", "--board", "b1", "--task", "t_wait", "--task", "t_run", "--task", "t_ghost")
        self.assertEqual(out.returncode, 4)
        data = json.loads(out.stdout)
        by_id = {c["id"]: c for c in data["cards"]}
        self.assertEqual(by_id["t_wait"]["parents"], ["t_run"])
        self.assertEqual(by_id["t_wait"]["creator"], "t_run")
        self.assertEqual(by_id["t_run"]["model"], "model-large" if self.FLAVOUR == "fork" else "")
        self.assertEqual(data["missing"], ["t_ghost"])
        self.assertEqual(data["count"], 2)

    def test_cards_without_task_lists_live_cards(self):
        out = self.run_live("cards", "--board", "b1")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual({c["id"] for c in json.loads(out.stdout)["cards"]}, {"t_run", "t_wait"})

    def test_graph_titles_are_capped_without_truncating_snapshot_titles(self):
        title = "界" * 200
        with sqlite3.connect(self.board_db) as con:
            con.execute("update tasks set title = ? where id = 't_wait'", (title,))
        cards = json.loads(self.run_live("cards", "--board", "b1", "--task", "t_wait").stdout)
        self.assertEqual(cards["cards"][0]["title"], title[:120])
        events = json.loads(self.run_live("events", "--board", "b1", "--watch", "t_run=running",
                                          "--since", "0", "--max-seconds", "0").stdout)
        self.assertEqual(events["new"][0]["title"], title[:120])
        self.assertEqual(events["count_new"], len(events["new"]))
        snapshot = json.loads(self.run_live("snapshot", "--board", "b1", "--task", "t_wait").stdout)
        self.assertEqual(snapshot["title"], title)

    # events

    def test_events_report_a_start_without_ending_the_call(self):
        quiet = self.run_live("events", "--board", "b1", "--watch", "t_run=running", "--since",
                              str(int(time.time()) + 10), "--max-seconds", "1", "--interval", "1")
        self.assertEqual(quiet.returncode, 3)
        self.assertEqual(json.loads(quiet.stdout)["changed"], [])
        # A start alone must not end the round (each one would spend one of the caller's rounds).
        started = self.run_live("events", "--board", "b1", "--watch", "t_run=ready", "--since",
                                str(int(time.time()) + 10), "--max-seconds", "0", "--interval", "0")
        self.assertEqual(started.returncode, 3)
        data = json.loads(started.stdout)
        self.assertTrue(data["timeout"])
        self.assertEqual(data["count_new"], 0)
        self.assertEqual([c["id"] for c in data["changed"]], ["t_run"])

    def test_events_never_report_a_known_card_as_new(self):
        # t_wait (filed by t_run) is already known: watching t_run must not report it again.
        out = self.run_live("events", "--board", "b1", "--watch", "t_run=running", "--known", "t_run,t_wait,t_done",
                            "--since", "0", "--max-seconds", "0", "--interval", "0")
        self.assertEqual(out.returncode, 3, out.stdout)
        self.assertEqual(json.loads(out.stdout)["new"], [])

    def test_events_report_cards_a_watched_card_filed(self):
        out = self.run_live("events", "--board", "b1", "--watch", "t_run=running", "--since", "0", "--max-seconds", "1")
        self.assertEqual(out.returncode, 0)
        self.assertEqual([c["id"] for c in json.loads(out.stdout)["new"]], ["t_wait"])

    def test_events_end_when_every_card_finished(self):
        out = self.run_live("events", "--board", "b1", "--watch", "t_done=running", "--since",
                            str(int(time.time()) + 10), "--max-seconds", "1")
        self.assertEqual(out.returncode, 2)
        self.assertTrue(json.loads(out.stdout)["all_finished"])
        self.assertEqual(json.loads(out.stdout)["count_new"], 0)

    # where the board lives

    def test_unknown_board_is_unreadable(self):
        self.assertEqual(self.run_live("snapshot", "--board", "nope", "--task", "t_run").returncode, 5)

    def test_default_board_lives_at_the_hermes_home_root(self):
        make_board(self.hermes / "kanban.db", self.FLAVOUR)
        with sqlite3.connect(self.hermes / "kanban.db") as con:
            insert(con, "tasks", id="t_root", title="Root card", assignee="worker", status="todo", created_at=self.now)
        out = self.run_live("snapshot", "--task", "t_root")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIsNone(json.loads(out.stdout)["run"])

    def test_profile_hermes_home_resolves_to_its_root(self):
        profile_home = str(self.hermes / "profiles" / "worker")
        out = self.run_live("snapshot", "--board", "b1", "--task", "t_run", HERMES_HOME=profile_home)
        self.assertEqual(out.returncode, 0, out.stderr)
        follow = self.run_live("follow", "--board", "b1", "--task", "t_run", "--max-seconds", "1",
                               "--interval", "1", HERMES_HOME=profile_home)
        self.assertIn("agent: Running the test suite.", follow.stdout)

    def test_a_real_root_inside_a_directory_named_profiles_stays_the_root(self):
        root = self.home / "profiles" / "hermes-main"
        (root / "kanban" / "boards").mkdir(parents=True)
        (self.hermes / "kanban" / "boards" / "b1").rename(root / "kanban" / "boards" / "b1")
        out = self.run_live("snapshot", "--board", "b1", "--task", "t_run", HERMES_HOME=str(root))
        self.assertEqual(out.returncode, 0, out.stdout)

    def test_profile_home_under_a_root_with_a_marker_file_resolves_to_it(self):
        root = self.home / "hermes-root"
        (root / "kanban" / "boards").mkdir(parents=True)
        (root / "config.yaml").write_text("")
        (self.hermes / "kanban" / "boards" / "b1").rename(root / "kanban" / "boards" / "b1")
        out = self.run_live("snapshot", "--board", "b1", "--task", "t_run",
                            HERMES_HOME=str(root / "profiles" / "worker"))
        self.assertEqual(out.returncode, 0, out.stdout)

    def test_kanban_home_override_moves_boards_not_session_stores(self):
        other = self.home / "boards-elsewhere"
        (other / "kanban" / "boards").mkdir(parents=True)
        (self.hermes / "kanban" / "boards" / "b1").rename(other / "kanban" / "boards" / "b1")
        out = self.run_live("follow", "--board", "b1", "--task", "t_run", "--max-seconds", "1",
                            "--interval", "1", HERMES_KANBAN_HOME=str(other))
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertIn("agent: Running the test suite.", out.stdout)


    # the kanban-watch workflow, run under node with inert agents

    HARNESS = r"""
const fs = require('fs');
const {spawnSync} = require('child_process');
const [scriptPath, cfgJson] = process.argv.slice(2);
const cfg = JSON.parse(cfgJson);
const source = fs.readFileSync(scriptPath, 'utf8').replace('export const meta', 'const meta');
const calls = [];
const logs = [];
// live: run the cards/events command the prompt names against the temp board, as a watcher
// would. KANBAN_LANES_HOME points at this checkout, so the prompt's own script path resolves.
const runLive = prompt => {
  const cmd = prompt.match(/`([^`]*kanban_live\.py[^`]*)`/)[1]
    .replace(/--max-seconds \d+/g, '--max-seconds 0 --interval 0');
  const out = spawnSync('sh', ['-c', cmd], {encoding: 'utf8', timeout: 5000});
  if (out.error) throw out.error;
  return {cmd, code: out.status, data: JSON.parse(out.stdout || '{}'), stderr: out.stderr};
};
let eventsSeen = 0;
const agent = async (prompt, options) => {
  const call = {prompt, ...options};
  calls.push(call);
  const req = options.schema.required;
  if (req.includes('now')) {
    if (cfg.live) { const r = runLive(prompt); call.exit = r.code; return r.data; }
    return {now: cfg.now === undefined ? 1 : cfg.now, cards: cfg.initial,
            count: cfg.count === undefined ? cfg.initial.length : cfg.count};
  }
  if (req.includes('changed')) {
    await new Promise(r => setTimeout(r, 5));
    if (cfg.live) { const r = runLive(prompt); call.exit = r.code; call.result = r.data; return r.data; }
    if (cfg.executeEvents) { const r = runLive(prompt); call.exit = r.code; return r.data; }
    const scripted = (cfg.events || [])[eventsSeen++];
    return scripted || {changed: [], new: [], all_finished: true};
  }
  if (req.includes('status')) return {status: cfg.waitStatus || 'running'};
  if (req.includes('reason')) return {reason: 'needs input'};
  const id = prompt.match(/Kanban card (t_\w+)/)[1];
  return {id, verdict: (cfg.verdicts || {})[id] || 'DONE', model_that_ran: 'worker', summary: 'ok'};
};
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
new AsyncFunction('args', 'agent', 'log', source)(cfg.args || {}, agent, message => logs.push(message))
  .then(result => console.log(JSON.stringify(cfg.captureLogs ? {calls, logs, result} : calls)))
  .catch(e => {console.error(e); process.exit(1)});
"""

    DEFAULT_INITIAL = [{"id": "t_wait", "title": "Queued", "status": "ready"},
                       {"id": "t_run", "title": "Working", "status": "running"},
                       {"id": "t_done", "title": "Done", "status": "done"}]

    def run_workflow_process(self, **cfg):
        # Exercise the real workflow orchestration with inert agents; live=True runs the
        # cards/events commands for real against the temp board, never a real one.
        if shutil.which("node") is None:
            self.skipTest("node is not installed")
        cfg.setdefault("initial", self.DEFAULT_INITIAL)
        env = self.live_env(KANBAN_LANES_HOME=str(ROOT))
        return subprocess.run(["node", "-", str(WORKFLOW), json.dumps(cfg)],
                              input=self.HARNESS, capture_output=True, text=True, timeout=120, env=env)

    def run_workflow(self, **cfg):
        out = self.run_workflow_process(**cfg)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def workflow_calls(self, verbose=False, terminal_only=False):
        initial = [{"id": "t_done", "title": "Done", "status": "done"}] if terminal_only else self.DEFAULT_INITIAL
        return self.run_workflow(initial=initial, args={"verbose": verbose}, verdicts={"t_run": "BLOCKED"})

    @staticmethod
    def watch_pairs(prompt):
        return shlex.split(prompt.split("--watch ", 1)[1].split("`", 1)[0])[0].split(",")

    def test_workflow_runs_the_installed_script_path(self):
        for call in self.workflow_calls():
            commands = re.findall(r"`([^`]*kanban_live\.py[^`]*)`", call["prompt"])
            self.assertTrue(commands, call["prompt"])
            for command in commands:
                self.assertTrue(command.startswith(f"python3 {LIVE_PATH} "), command)

    def test_workflow_rejects_shell_metacharacters_in_board_slug(self):
        canary = self.home / "board-canary"
        for board in (f"b1; touch {canary}; #", "", "b1\n", "../b1", None):
            with self.subTest(board=board):
                out = self.run_workflow_process(live=True, args={"board": board})
                self.assertEqual(out.returncode, 1, out.stderr)
                self.assertIn("Invalid board slug", out.stderr)
                self.assertFalse(canary.exists())

    def test_workflow_drops_and_logs_bad_ids_at_every_card_ingress(self):
        bad = f"t_bad; touch {self.home / 'card-canary'}; #"
        data = self.run_workflow(
            captureLogs=True,
            args={"board": "valid-board_1", "tasks": [bad, "t_run"],
                  "phases": [{"title": "Build", "tasks": [bad]}]},
            initial=[{"id": bad, "title": "Bad", "status": "ready"},
                     {"id": "t_run", "title": "Good", "status": "running"}],
            verdicts={"t_run": "STILL_RUNNING"},
            events=[{"changed": [{"id": bad, "status": "ready"}],
                     "new": [{"id": bad, "title": "Bad child", "status": "ready"}], "timeout": True}])
        dropped = [line for line in data["logs"] if "invalid card id" in line.lower() and bad in line]
        self.assertEqual(len(dropped), 6)  # phase, both requested lists, initial, changed, new
        for call in data["calls"]:
            self.assertNotIn(bad, call["prompt"])
        self.assertFalse((self.home / "card-canary").exists())
        self.assertTrue(any("t_run" in call["prompt"] for call in data["calls"]))
        only_bad = self.run_workflow(captureLogs=True, args={"tasks": [bad, "t_bad_id", "t_run\n"]})
        self.assertEqual(only_bad["calls"], [])  # never fall back to watching the whole board
        self.assertEqual(only_bad["result"]["cards"], [])
        self.assertEqual(len(only_bad["logs"]), 3)

    def test_workflow_quotes_shell_arguments_including_watch_and_known_lists(self):
        canary = self.home / "quoted-canary"
        # Even a schema-violating timestamp containing a quote must remain one
        # literal argument when the real shell executes the generated command.
        since = f"0; touch {canary}; # ' $(touch {canary})"
        calls = self.run_workflow(
            now=since, executeEvents=True, args={"board": "b1", "tasks": ["t_run"]},
            initial=[{"id": "t_run", "title": "Good", "status": "running"}],
            verdicts={"t_run": "STILL_RUNNING"})
        self.assertFalse(canary.exists())
        for call in calls + self.workflow_calls():
            for command in re.findall(r"`([^`]*kanban_live\.py[^`]*)`", call["prompt"]):
                argv = shlex.split(command)
                for flag in ("--board", "--task", "--watch", "--known", "--since"):
                    if flag in argv:
                        value = argv[argv.index(flag) + 1]
                        quoted = "'" + value.replace("'", "'\\''") + "'"
                        self.assertIn(f"{flag} {quoted}", command)
                if "--since" in argv:
                    self.assertEqual(argv[argv.index("--since") + 1], since if call in calls else "1")

    def test_workflow_uses_cheap_idle_rows_compact_follow_and_snapshot_flags(self):
        for verbose in (False, True):
            with self.subTest(verbose=verbose):
                calls = self.workflow_calls(verbose)
                self.assertEqual(len(calls), 7)
                for call in calls:
                    prompt = call["prompt"]
                    live = " follow --board " in prompt
                    self.assertEqual(call["model"], "sonnet" if live else "haiku")
                    self.assertEqual(call["effort"], "low")
                    self.assertEqual(call["agentType"], "kanban-watcher")
                    self.assertNotIn("hermes kanban", prompt)
                    if live:
                        self.assertIn("--verbose" if verbose else "--compact", prompt)
                        self.assertIn("--max-seconds 270", prompt)
                        self.assertIn("up to 300 times", prompt)
                        self.assertIn("Reply with nothing between calls; just run the command again.", prompt)
                    elif " wait-start " in prompt or " events " in prompt:
                        self.assertIn("--max-seconds 270", prompt)
                        self.assertIn("up to 169 times" if " wait-start " in prompt else "up to 13 times", prompt)
                    if call["schema"]["required"] == ["reason"]:
                        self.assertIn(" snapshot ", prompt)
                        self.assertIn("block_reason", prompt)
                events = next(c for c in calls if " events " in c["prompt"])
                self.assertNotIn("t_done=", events["prompt"])
                self.assertIn("t_run=running", events["prompt"])
                self.assertIn("--known 't_wait,t_run,t_done' ", events["prompt"])

    def test_workflow_keeps_known_terminal_children_out_of_new_across_rounds(self):
        # t_run is still running; t_child, which t_run filed after the watch began, is already done.
        now = int(time.time())
        with sqlite3.connect(self.board_db) as con:
            con.execute("delete from task_links")
            con.execute("delete from task_events")
            con.execute("update tasks set status = 'done' where id = 't_wait'")
            insert(con, "tasks", id="t_child", title="Child", assignee="worker", status="done",
                   created_at=now + 100, started_at=now + 100, completed_at=now + 100)
            insert(con, "task_events", task_id="t_child", kind="created",
                   payload=json.dumps({"creator_task_id": "t_run"}), created_at=now + 100)
        calls = self.run_workflow(live=True, args={"board": "b1"}, verdicts={"t_run": "STILL_RUNNING"})
        events = [c for c in calls if " events " in c["prompt"]]
        self.assertEqual([c["exit"] for c in events][:2], [0, 3], [c.get("result") for c in events])
        self.assertEqual([c["id"] for c in events[0]["result"]["new"]], ["t_child"])
        # Every later round runs to its time budget: the known child is never "new" again.
        self.assertTrue(all(c["exit"] == 3 and c["result"]["new"] == [] for c in events[1:]))
        self.assertEqual(len(events), 12 + 2)
        known = shlex.split(events[1]["prompt"].split("--known ", 1)[1].split("`", 1)[0])[0]
        self.assertIn("t_child", known.split(","))

    def test_workflow_keeps_blocked_and_review_cards_in_the_watch(self):
        # Blocked and review cards come back (unblock, changes requested) and may file children.
        calls = self.run_workflow(
            verdicts={"t_run": "BLOCKED", "t_wait": "REVIEW"},
            events=[{"changed": [], "new": [], "timeout": True}, {"changed": [], "new": [], "timeout": True}])
        events = [c for c in calls if " events " in c["prompt"]]
        self.assertEqual(len(events), 3)
        self.assertEqual(sorted(self.watch_pairs(events[-1]["prompt"])), ["t_run=blocked", "t_wait=review"])

    def test_workflow_normalises_agent_reported_statuses_before_the_shell(self):
        calls = self.run_workflow(
            waitStatus="[Running]",
            initial=[{"id": "t_wait", "title": "Queued", "status": "[ready]"},
                     {"id": "t_run", "title": "Working", "status": "running (started)"}],
            verdicts={"t_wait": "STILL_RUNNING", "t_run": "STILL_RUNNING"},
            events=[{"changed": [{"id": "t_wait", "title": "Queued", "status": "[running]"}], "new": [],
                     "timeout": True},
                    {"changed": [], "new": [{"id": "t_new", "title": "New", "status": "ready*"}], "timeout": True}])
        events = [c for c in calls if " events " in c["prompt"]]
        for call in events:
            for pair in self.watch_pairs(call["prompt"]):
                self.assertRegex(pair, r"^t_\w+=[a-z]+$")
        self.assertEqual(self.watch_pairs(events[0]["prompt"]), ["t_wait=ready", "t_run=running"])
        self.assertEqual(sorted(self.watch_pairs(events[-1]["prompt"])), ["t_new=ready", "t_run=running", "t_wait=running"])
        # The queued card's row saw "[Running]" as a start and went on to its follow row.
        self.assertTrue(any(" follow --board " in c["prompt"] and "t_wait" in c["prompt"] for c in calls))

    def test_workflow_follow_asks_for_a_quiet_line_inside_one_call(self):
        # The wedge hint needs the quiet line to fire within one 270-second follow call.
        follows = [c for c in self.workflow_calls() if " follow --board " in c["prompt"]]
        self.assertTrue(follows)
        for call in follows:
            self.assertEqual(call["prompt"].count("--max-seconds 270 --quiet-warn 240"), 2)

    def test_workflow_cursor_is_run_scoped_and_shared_only_within_that_run(self):
        keys = []
        for now in (100, 101):
            calls = self.run_workflow(now=now)
            run_keys = set()
            for call in calls:
                for command in re.findall(r"`([^`]*kanban_live\.py[^`]*)`", call["prompt"]):
                    argv = shlex.split(command)
                    if "--cursor-key" in argv:
                        run_keys.add(argv[argv.index("--cursor-key") + 1])
            self.assertEqual(len(run_keys), 1)
            keys.append(run_keys.pop())
        self.assertNotEqual(*keys)

    def test_workflow_logs_card_count_losses(self):
        data = self.run_workflow(captureLogs=True, count=4,
                                 events=[{"changed": [], "new": [], "count_new": 2, "all_finished": True}])
        self.assertTrue(any("board read lost 1 cards" in line for line in data["logs"]))
        self.assertTrue(any("board read lost 2 cards" in line for line in data["logs"]))

    def test_workflow_keeps_max_hours_zero(self):
        rounds = [{"changed": [], "new": [], "timeout": True}] * 20
        calls = self.run_workflow(args={"max_hours": 0}, verdicts={"t_run": "BLOCKED"}, events=rounds)
        self.assertEqual(len([c for c in calls if " events " in c["prompt"]]), 2)

    def test_workflow_does_not_launch_events_when_all_known_cards_are_terminal(self):
        calls = self.workflow_calls(terminal_only=True)
        self.assertFalse(any(" events " in c["prompt"] for c in calls))


class UpstreamSchemaTests(KanbanLiveContract, unittest.TestCase):
    FLAVOUR = "upstream"

    def follow_with_sessions(self, sessions, messages, task="t_run"):
        self.state_db.unlink()
        make_store(self.state_db, sessions, messages)
        return self.run_live("follow", "--board", "b1", "--task", task, "--max-seconds", "1", "--interval", "1")

    def test_session_found_by_the_kanban_task_id_fallback_title(self):
        out = self.follow_with_sessions([("u1", "kanban", "Kanban task t_run", self.now - 49)],
                                        [("u1", "assistant", "From the fallback title.", None, None, self.now)])
        self.assertIn("agent: From the fallback title.", out.stdout)

    def test_session_found_by_card_title_with_a_retry_suffix(self):
        out = self.follow_with_sessions([("u1", "kanban", "Running card #2", self.now - 49)],
                                        [("u1", "assistant", "From the retried title.", None, None, self.now)])
        self.assertIn("agent: From the retried title.", out.stdout)

    def test_long_card_title_matches_its_truncated_session_title(self):
        long_title = "Port the importer " * 10
        with sqlite3.connect(self.board_db) as con:
            con.execute("update tasks set title = ? where id = 't_run'", (long_title,))
        session_title = " ".join(long_title.split())[:95].rstrip() + "…"
        out = self.follow_with_sessions([("u1", "kanban", session_title, self.now - 49)],
                                        [("u1", "assistant", "From the long title.", None, None, self.now)])
        self.assertIn("agent: From the long title.", out.stdout)

    def test_session_found_by_the_first_user_turn_whatever_its_title(self):
        out = self.follow_with_sessions(
            [("u1", "kanban", "Something the model chose", self.now - 49)],
            [("u1", "user", "work kanban task t_run", None, None, self.now),
             ("u1", "assistant", "From the prompt match.", None, None, self.now)])
        self.assertIn("agent: From the prompt match.", out.stdout)

    def test_a_card_id_prefix_does_not_match_another_cards_session(self):
        out = self.follow_with_sessions(
            [("u1", "kanban", "Unrelated", self.now - 49)],
            [("u1", "user", "work kanban task t_runner", None, None, self.now),
             ("u1", "assistant", "Wrong card.", None, None, self.now)])
        self.assertNotIn("Wrong card.", out.stdout)

    def test_an_old_session_with_the_same_card_title_is_ignored(self):
        out = self.follow_with_sessions([("u1", "kanban", "Running card", self.now - 86400)],
                                        [("u1", "assistant", "Yesterday's card.", None, None, self.now - 86400)])
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertNotIn("Yesterday's card.", out.stdout)

    def test_a_same_titled_cards_session_is_not_followed(self):
        # Upstream keeps titles unique per store: the twin card's worker session is "Running card #2".
        # t_run's own session is not written yet, so only the twin's session carries the title.
        with sqlite3.connect(self.board_db) as con:
            insert(con, "tasks", id="t_twin", title="Running card", assignee="worker", status="running",
                   created_at=self.now - 60, started_at=self.now - 45)
        out = self.follow_with_sessions(
            [("u2", "kanban", "Running card #2", self.now - 44)],
            [("u2", "user", "work kanban task t_twin", None, None, self.now - 44),
             ("u2", "assistant", "Twin card transcript.", None, None, self.now - 43)])
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertNotIn("session u2", out.stdout)
        self.assertNotIn("Twin card transcript.", out.stdout)

    def test_card_title_with_invisible_characters_matches_its_sanitised_session_title(self):
        with sqlite3.connect(self.board_db) as con:
            con.execute("update tasks set title = ? where id = 't_run'", ("Running\u200b card\x07\u202e",))
        out = self.follow_with_sessions([("u1", "kanban", "Running card", self.now - 49)],
                                        [("u1", "assistant", "From the sanitised title.", None, None, self.now)])
        self.assertIn("agent: From the sanitised title.", out.stdout)

    def test_default_assignee_reads_the_root_session_store(self):
        with sqlite3.connect(self.board_db) as con:
            con.execute("update tasks set assignee = 'default' where id = 't_run'")
        make_store(self.hermes / "state.db", *self.store_rows(self.now))
        out = self.run_live("follow", "--board", "b1", "--task", "t_run", "--max-seconds", "1", "--interval", "1")
        self.assertIn("agent: Running the test suite.", out.stdout)


class ForkSchemaTests(KanbanLiveContract, unittest.TestCase):
    FLAVOUR = "fork"


class QuietProgressTests(unittest.TestCase):
    def test_all_agent_wait_examples_wake_inside_the_cache_window(self):
        for path in (ROOT / "examples" / "mixed-wave.js", ROOT / "agents" / "kanban-watcher.md",
                     ROOT / "docs" / "mixed-waves.md"):
            waits = re.findall(r"--wait (\d+)", path.read_text())
            self.assertTrue(waits, path)
            self.assertEqual(set(waits), {"270"}, path)

    def test_heartbeat_only_calls_do_not_hide_a_wedged_worker(self):
        module = runpy.run_path(str(SCRIPT))
        follow = module["cmd_follow"]
        clock = [1000.0]
        card = {"id": "t_run", "title": "Working", "status": "running", "assignee": "worker"}
        calls = json.dumps([{"function": {"name": "functions.kanban_heartbeat"}}])
        rows = [{"id": 1, "role": "assistant", "content": "", "tool_calls": calls,
                 "tool_name": None, "timestamp": 1000},
                {"id": 2, "role": "tool", "content": "ok", "tool_calls": None,
                 "tool_name": "kanban_heartbeat", "timestamp": 1000}]
        def nap(deadline, interval):
            clock[0] = min(deadline, clock[0] + interval)
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(board="b", task="t_run", max_seconds=270, interval=15,
                                      max_bytes=128, compact=True, cursor_key="run1", from_start=False,
                                      backlog=15, quiet_warn=240)
            output = io.StringIO()
            with patch.dict(follow.__globals__, read_card=lambda *a: card,
                            session_store=lambda *a: Path(tmp), find_session=lambda *a: "s",
                            compression_child=lambda *a: None, new_messages=lambda *a: rows,
                            cursor_path=lambda *a: Path(tmp) / "test.cursor", nap=nap), \
                    patch("time.time", side_effect=lambda: clock[0]), patch("sys.stdout", output):
                self.assertEqual(follow(args), 3)
            self.assertIn("no new worker messages\n", output.getvalue())
            self.assertIn("still running", output.getvalue())
            self.assertLessEqual(len(output.getvalue().encode()), 128)


class MinimalBoardTests(unittest.TestCase):
    """A board with only a tasks table (no runs, events or links) never crashes a read."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hermes = Path(self.tmp.name) / ".hermes"
        self.hermes.mkdir()
        with sqlite3.connect(self.hermes / "kanban.db") as con:
            con.execute("create table tasks(id text primary key, title text, assignee text, status text,"
                        " created_at int)")
            con.execute("insert into tasks values('t1', 'Bare card', 'worker', 'done', ?)", (int(time.time()),))

    def run_live(self, *args):
        env = dict(os.environ, HERMES_HOME=str(self.hermes), HOME=self.tmp.name)
        env.pop("XDG_CACHE_HOME", None)
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=env,
                              capture_output=True, text=True, timeout=30)

    def test_every_read_degrades_to_nulls(self):
        card = json.loads(self.run_live("snapshot", "--task", "t1").stdout)
        self.assertIsNone(card["run"])
        self.assertIsNone(card["last_event"])
        self.assertIsNone(card["current_run_id"])
        cards = json.loads(self.run_live("cards", "--task", "t1").stdout)["cards"]
        self.assertEqual(cards, [{"id": "t1", "title": "Bare card", "status": "done", "assignee": "worker",
                                  "parents": [], "creator": None, "model": ""}])
        follow = self.run_live("follow", "--task", "t1", "--max-seconds", "1", "--interval", "1")
        self.assertEqual(follow.returncode, 0, follow.stderr)
        self.assertIn("FINAL t1 [done]", follow.stdout)


if __name__ == "__main__":
    unittest.main()
