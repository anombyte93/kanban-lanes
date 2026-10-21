#!/usr/bin/env python3
"""Read-only live view of Hermes Kanban cards, for watcher agents in a Workflow.

The worker's own conversation (assistant text, tool calls, tool results) lives in the
assignee profile's session store, ``$HERMES_HOME/profiles/<assignee>/state.db``, in a
session titled ``work kanban task <card id>`` (forks), or titled after the card or
``Kanban task <card id>`` (upstream Hermes Agent). Only a session started during the card's
current run counts, and a session whose first user turn names another card never does. When
context compression ends the session and starts a child session, ``follow`` moves to the
child. This script tails it, so a watcher subagent's transcript shows the card's agent working,
step by step.

Which board: ``--board B`` (default ``default``) under ``$HERMES_KANBAN_HOME``, else under the
Hermes root from ``$HERMES_HOME`` (default ~/.hermes; a profile home resolves to its root).
Nothing else is read: not ``HERMES_KANBAN_DB``, not ``HERMES_KANBAN_BOARD`` and not the
``<root>/kanban/current`` file, so a worker's pinned board or a non-default current board must
be named with ``--board``.

Works on upstream Hermes Agent boards and on forks that add columns: optional columns
(task_runs.model_used / provider_used / fallback_from, tasks.block_reason) are read only when
present and come back null otherwise; a missing table or session store is skipped, never fatal.

Subcommands (all read-only; nothing here writes to a board or a session store):

  snapshot   --board B --task T [--task T...]        one JSON line per card
  wait-start --board B --task T --max-seconds N      exit 0 once the card left the queue
  follow     --board B --task T --max-seconds N      print new worker messages as they land;
             [--compact | --verbose] [--max-bytes N]  exit 0 once the card is terminal. --compact
                                                     prints tool calls and the latest worker text
                                                     only, capped at 1500 bytes by default;
                                                     --verbose (the default) prints the full stream
  active     --board B                                JSON {now, cards} for every queued or running card
  cards      --board B [--task T...]                  JSON {now, cards} with parents, creator and model;
                                                     no --task = every queued or running card
  events     --board B --watch id=status,... [--known a,b] --since EPOCH --max-seconds N
                                                     exit 0 + JSON once a watched card files/parents a card
                                                     that is in neither --watch nor --known; 2 = all watched
                                                     cards finished. "changed" (queued watched cards that
                                                     started) rides along on every result; a start alone
                                                     does not end the call.
  scout      --board B --known a,b --since EPOCH --max-seconds N
                                                     exit 0 + JSON when new cards appear,
                                                     2 when every known card is terminal

Exit codes: 0 reached, 2 nothing left to watch (scout), 3 time budget used (call again),
4 not found, 5 board unreadable. A timeout is normal: the caller runs the same command again.
The follow cursor lives in ${XDG_CACHE_HOME:-~/.cache}/kanban-lanes/live/, so a repeat call
continues where the last one stopped instead of reprinting. A compact call saves it only after
it prints, so a call that is killed first replays its batch on the next call.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path


_HERMES_HOME_MARKERS = ("config.yaml", ".env", "state.db")


def _is_profiles_dir(profiles: Path) -> bool:
    """True when ``profiles`` is provably ``<hermes root>/profiles``, by upstream's test: the root
    is named .hermes, holds a marker file, has a profiles/.deleted directory, or is ~/.hermes."""
    root = profiles.parent
    if root.name == ".hermes":
        return True
    try:
        if (profiles / ".deleted").is_dir() or any((root / m).exists() for m in _HERMES_HOME_MARKERS):
            return True
        return root.resolve() == (Path.home() / ".hermes").resolve()
    except OSError:
        return False


def _hermes_root() -> Path:
    """$HERMES_HOME (default ~/.hermes). A profile home (<root>/profiles/<name>) resolves to its
    root, because boards and every profile's session store live under the root. A real root that
    merely sits in a directory named ``profiles`` stays where it is."""
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
    if home.parent.name == "profiles" and _is_profiles_dir(home.parent):
        return home.parent.parent
    return home


HERMES = _hermes_root()
KANBAN_HOME = Path(os.environ.get("HERMES_KANBAN_HOME") or HERMES).expanduser()
CURSOR_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "kanban-lanes" / "live"
QUEUED = {"triage", "todo", "ready", "scheduled"}
TERMINAL = {"done", "blocked", "archived", "review"}
SNIP = 400
REASON_MAX = 2000
LEGACY_REASON = "Legacy block reason unknown"
# Events after which an older `blocked` event no longer describes why the card is blocked.
BLOCK_RESETS = ("unblocked", "claimed", "gave_up")
SESSION_SKEW = 10  # seconds a worker session may appear to start before its run (clock skew)


def board_db(board: str) -> Path:
    if board in ("", "default"):
        return KANBAN_HOME / "kanban.db"
    return KANBAN_HOME / "kanban" / "boards" / board / "kanban.db"


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Column names of ``table``; empty when the table does not exist."""
    return {row[1] for row in conn.execute(f"pragma table_info({table})")}


def select_list(conn: sqlite3.Connection, table: str, wanted) -> str:
    """Select list naming every wanted column, ``null as <name>`` for any the table lacks.

    ``wanted`` holds column names or ``(name, expression)`` pairs; upstream Hermes Agent has
    no task_runs.model_used / provider_used / fallback_from, which forks add."""
    have = table_columns(conn, table)
    parts = []
    for item in wanted:
        name, expr = item if isinstance(item, tuple) else (item, item)
        parts.append(f"{expr} as {name}" if name in have else f"null as {name}")
    return ", ".join(parts)


# Every tasks column read goes through this tuple, so a column one schema lacks reads null.
# Never name a tasks column in raw SQL instead. block_reason (fork-only) has its own reader.
TASK_FIELDS = ("id", "title", "assignee", "status", "created_at", "started_at", "completed_at",
               "last_heartbeat_at", "worker_pid", "current_run_id")
RUN_FIELDS = ("id", "status", "outcome", "model_used", "provider_used", "fallback_from",
              "started_at", "ended_at", ("summary", f"substr(coalesce(summary, ''), 1, {SNIP})"))
MESSAGE_FIELDS = ("id", "role", "content", "tool_calls", "tool_name", "timestamp")


def latest_run(conn: sqlite3.Connection, task: str, fields=RUN_FIELDS) -> dict | None:
    """The card's newest task_runs row as a dict (missing columns null); None without runs."""
    if not table_columns(conn, "task_runs"):
        return None
    row = conn.execute(
        f"select {select_list(conn, 'task_runs', fields)} from task_runs"
        " where task_id = ? order by id desc limit 1",
        (task,),
    ).fetchone()
    return dict(row) if row else None


def read_card(board: str, task: str) -> dict | None:
    with connect(board_db(board)) as conn:
        row = conn.execute(
            f"select {select_list(conn, 'tasks', TASK_FIELDS)} from tasks where id = ?",
            (task,),
        ).fetchone()
        if row is None:
            return None
        card = dict(row)
        card["run"] = latest_run(conn, task)
        event = conn.execute(
            "select kind, created_at from task_events where task_id = ?"
            " and kind != 'heartbeat' order by id desc limit 1",
            (task,),
        ).fetchone() if table_columns(conn, "task_events") else None
        card["last_event"] = dict(event) if event else None
        card["block_reason"] = clip_reason(block_reason(conn, task)) if card["status"] == "blocked" else None
    return card


def block_reason(conn: sqlite3.Connection, task: str) -> str | None:
    """Why the card is blocked now, in the order `hermes kanban show` trusts it.

    The tasks.block_reason column (fork-only) is current: auto-blocks write it and no new
    `blocked` event, so it wins unless it is the migration placeholder. Next comes the newest
    block-history event, but only when it is a `blocked` event or a `gave_up` carrying a
    block_reason: an `unblocked` or `claimed` after a block makes that block stale. Then the
    newest comment. Upstream boards have no column, so they start at the events; with none of
    the three the reason is None.
    """
    if "block_reason" in table_columns(conn, "tasks"):
        column = conn.execute("select block_reason from tasks where id = ?", (task,)).fetchone()[0]
        if column and str(column).strip() != LEGACY_REASON:
            return str(column)
    if {"task_id", "kind", "payload"} <= table_columns(conn, "task_events"):
        marks = ",".join("?" * (len(BLOCK_RESETS) + 1))
        event = conn.execute(
            f"select kind, payload from task_events where task_id = ? and kind in ({marks})"
            " order by id desc limit 1", (task, "blocked", *BLOCK_RESETS),
        ).fetchone()
        if event and event["kind"] in ("blocked", "gave_up"):
            try:
                payload = json.loads(event["payload"]) or {}
                reason = payload.get("reason" if event["kind"] == "blocked" else "block_reason")
            except (ValueError, TypeError, AttributeError):
                reason = None
            if reason:
                return str(reason)
    if {"task_id", "body"} <= table_columns(conn, "task_comments"):
        comment = conn.execute(
            "select body from task_comments where task_id = ? order by id desc limit 1", (task,),
        ).fetchone()
        if comment and comment[0]:
            return str(comment[0])
    return None


def clip_reason(reason: str | None) -> str | None:
    """Keep the whole reason; a rare very long one is cut with a marker saying so."""
    if not reason or len(reason) <= REASON_MAX:
        return reason
    return reason[:REASON_MAX] + f"… [cut: {len(reason)} characters in full]"


def hhmm(epoch: float | None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(epoch)) if epoch else "-"


def card_line(card: dict) -> str:
    run = card.get("run") or {}
    model = run.get("model_used") or "?"
    if run.get("fallback_from"):
        model += f" (fallback from {run['fallback_from']})"
    return (
        f"{card['id']} [{card['status']}] {card['title'][:90]} | {card.get('assignee') or 'unassigned'}"
        f" on {model} | started {hhmm(card.get('started_at'))}"
    )


def session_store(assignee: str | None) -> Path | None:
    if not assignee:
        return None
    path = HERMES / "profiles" / assignee / "state.db"
    if not path.exists() and assignee == "default":
        path = HERMES / "state.db"  # the default profile's store sits at the root
    return path if path.exists() else None


# Upstream SessionDB.sanitize_title strips these before it stores a title.
_TITLE_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TITLE_INVISIBLE = re.compile(r"[\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\ufffc\ufff9-\ufffb]")
_CARD_MENTION = re.compile(r"kanban task ([\w-]+)")


def worker_session_titles(task: str, card_title: str | None = None) -> list[str]:
    """Lower-cased titles a card's worker session can carry, most specific first.

    Forks title it ``work kanban task <id>``. Upstream Hermes Agent titles it after the card
    (control and invisible characters stripped, whitespace collapsed, cut to 95 characters plus
    ``…``, ``#N`` appended on a clash) or ``Kanban task <id>`` when the board could not be read."""
    titles = [f"work kanban task {task}", f"kanban task {task}"]
    name = _TITLE_INVISIBLE.sub("", _TITLE_CONTROL.sub("", card_title or ""))
    name = " ".join(name.split())
    if name:
        if len(name) > 96:
            name = name[:95].rstrip() + "…"
        titles.append(name)
    return [t.lower() for t in titles]


def _match_recent_session(conn: sqlite3.Connection, task: str, card_title: str | None,
                          since: float | None, source_filter: str) -> str | None:
    """Upstream fallback: a kanban session started in this run whose first user turn names the
    card (the worker's prompt is ``work kanban task <id>``), else one titled after the card
    whose first user turn is not written yet."""
    cols = table_columns(conn, "sessions")
    params: list = []
    where = source_filter
    if since:
        where += " and started_at >= ?"
        params.append(since - SESSION_SKEW)
    rows = conn.execute(
        f"select id, {'title' if 'title' in cols else 'null as title'} from sessions"
        f" where 1 = 1{where} order by started_at desc limit 50",
        params,
    ).fetchall()
    first_turns: dict[str, str] = {}
    if {"session_id", "role", "content"} <= table_columns(conn, "messages"):
        for row in rows:
            first = conn.execute(
                "select content from messages where session_id = ? and role = 'user' order by id limit 1",
                (row["id"],),
            ).fetchone()
            if first and isinstance(first[0], str) and first[0].strip():
                first_turns[row["id"]] = first[0].lower()
                named = _CARD_MENTION.findall(first_turns[row["id"]])
                if task.lower() in named:
                    return row["id"]
    titles = worker_session_titles(task, card_title)
    if len(titles) > 2 and since:  # a card title is not unique: only trust it within this run
        name = titles[2]
        for row in rows:
            if row["id"] in first_turns:
                continue  # its first user turn does not name this card: another card's worker
            title = (row["title"] or "").lower()
            if title == name or re.fullmatch(re.escape(name) + r" #\d+", title):
                return row["id"]
    return None


def find_session(store: Path, task: str, card_title: str | None = None,
                 since: float | None = None) -> str | None:
    """The card's worker session. With ``since`` (the run's start) only a session started in
    this run counts, so a crashed or reclaimed run's session is never followed again."""
    try:
        with connect(store) as conn:
            cols = table_columns(conn, "sessions")
            if not {"id", "started_at"} <= cols:
                return None
            source = " and source = 'kanban'" if "source" in cols else ""
            params = list(worker_session_titles(task))
            in_run = ""
            if since:
                in_run = " and started_at >= ?"
                params.append(since - SESSION_SKEW)
            row = conn.execute(
                f"select id from sessions where lower(title) in (?, ?){source}{in_run}"
                " order by started_at desc limit 1",
                params,
            ).fetchone() if "title" in cols else None
            return row["id"] if row else _match_recent_session(conn, task, card_title, since, source)
    except sqlite3.Error:
        return None


def compression_child(store: Path, session: str) -> str | None:
    """The session that context compression continued ``session`` in, else None.

    Upstream ends the old session with end_reason 'compression' and starts a child whose
    parent_session_id names it; branch, delegate and tool children are not continuations."""
    try:
        with connect(store) as conn:
            cols = table_columns(conn, "sessions")
            if not {"id", "parent_session_id", "end_reason", "started_at"} <= cols:
                return None
            row = conn.execute("select end_reason from sessions where id = ?", (session,)).fetchone()
            if not row or row[0] != "compression":
                return None
            only = " and coalesce(source, '') != 'tool'" if "source" in cols else ""
            if "model_config" in cols:
                only += (" and instr(coalesce(model_config, ''), '_delegate_from') = 0"
                         " and instr(coalesce(model_config, ''), '_branched_from') = 0")
            child = conn.execute(
                f"select id from sessions where parent_session_id = ?{only} order by started_at, id limit 1",
                (session,),
            ).fetchone()
            return child[0] if child else None
    except sqlite3.Error:
        return None


def render(msg: sqlite3.Row) -> str | None:
    stamp = hhmm(msg["timestamp"])
    role = msg["role"]
    text = (msg["content"] or "").strip().replace("\n", " ")
    if role == "assistant":
        lines = []
        if text:
            lines.append(f"[{stamp}] agent: {text[:SNIP]}")
        for call in json.loads(msg["tool_calls"] or "[]"):
            fn = call.get("function", call)
            args = str(fn.get("arguments", ""))[:160].replace("\n", " ")
            lines.append(f"[{stamp}]   -> {fn.get('name', '?')} {args}")
        return "\n".join(lines) or None
    if role == "tool":
        return f"[{stamp}]   <- {msg['tool_name'] or 'tool'}: {text[:200]}"
    return None


class FollowOutput:
    """Bounded output, reserving space for the exit status and overflow count.

    Header and verbose lines stream as they arrive. Compact tool calls are held until
    finish(), so the cap keeps the newest calls and always leaves room for the latest
    assistant text and the collapsed count; the oldest calls go into the overflow count.
    """

    STATUS_RESERVE = 128

    def __init__(self, max_bytes: int | None):
        self.max_bytes = max_bytes
        self.used = 0
        self.hidden = 0
        self.assistants = 0
        self.latest = None
        self.calls: list[str] = []

    @staticmethod
    def size(line: str) -> int:
        return len((line + "\n").encode("utf-8"))

    def put(self, line: str):
        print(line, flush=True)
        self.used += self.size(line)

    def emit(self, text: str):
        if self.max_bytes is None:
            print(text, flush=True)
            return
        for line in text.splitlines():
            if not self.hidden and self.used + self.size(line) <= self.max_bytes - self.STATUS_RESERVE:
                self.put(line)
            else:
                self.hidden += 1

    def hold(self, line: str):
        """A compact line that may be dropped (oldest first) when the cap is hit."""
        self.calls.append(line)

    def compact(self, rows: list[sqlite3.Row]):
        for row in rows:
            if row["role"] != "assistant":
                continue
            text = (row["content"] or "").strip().replace("\n", " ")
            if text:
                self.assistants += 1
                self.latest = f"[{hhmm(row['timestamp'])}] agent: {text[:300]}"
            for call in json.loads(row["tool_calls"] or "[]"):
                fn = call.get("function", call)
                if fn.get("name", "").rsplit(".", 1)[-1] == "kanban_heartbeat":
                    continue
                args = str(fn.get("arguments", "")).replace("\n", " ")[:80]
                self.hold(f"→ {fn.get('name', '?')} {args}")

    def flush_compact(self):
        collapsed = f"… {self.assistants - 1} earlier assistant messages collapsed" if self.assistants > 1 else None
        if self.max_bytes is None:
            for line in self.calls + [collapsed, self.latest]:
                if line:
                    print(line, flush=True)
            return
        room = self.max_bytes - self.STATUS_RESERVE - self.used
        latest = self.latest
        if latest and self.size(latest) > room:
            raw = latest.encode("utf-8")
            latest = raw[:max(room - 4, 0)].decode("utf-8", errors="ignore") + "…" if room >= 48 else None
            if latest is None:
                self.hidden += 1
        room -= self.size(latest) if latest else 0
        if collapsed and self.size(collapsed) > room:
            collapsed, self.hidden = None, self.hidden + 1
        room -= self.size(collapsed) if collapsed else 0
        kept: list[str] = []
        for line in reversed(self.calls):
            if self.size(line) > room:
                break
            kept.append(line)
            room -= self.size(line)
        self.hidden += len(self.calls) - len(kept)
        for line in reversed(kept):
            self.put(line)
        for line in (collapsed, latest):
            if line:
                self.put(line)

    def finish(self, status: str):
        self.flush_compact()
        if self.max_bytes is None:
            print(status, flush=True)
            return
        footer = f"… {self.hidden} lines not shown (the cursor has moved past them)" if self.hidden else ""
        available = self.max_bytes - self.used - len((footer + "\n").encode("utf-8")) - 1
        raw = status.encode("utf-8")
        if len(raw) > available:
            status = raw[:available - 3].decode("utf-8", errors="ignore") + "…"
        print(status, flush=True)
        if footer:
            print(footer, flush=True)


def cursor_path(board: str, task: str, key: str = "") -> Path:
    suffix = f"-{key}" if key else ""
    return CURSOR_DIR / f"{board or 'default'}-{task}{suffix}.cursor"


_warned: set[str] = set()


def _warn_once(key: str, text: str, say=print) -> None:
    if key not in _warned:
        _warned.add(key)
        say(text)


def new_messages(store: Path, session: str, after: int, say=print) -> list[sqlite3.Row]:
    """The session's messages after ``after``; [] (and one line saying so, through ``say``) when
    the store has no readable messages table."""
    try:
        with connect(store) as conn:
            cols = table_columns(conn, "messages")
            if not {"id", "session_id"} <= cols:
                _warn_once(str(store), f"session store {store} has no messages table;"
                                       " following the card's status only", say)
                return []
            active = " and active = 1" if "active" in cols else ""
            return conn.execute(
                f"select {select_list(conn, 'messages', MESSAGE_FIELDS)} from messages"
                f" where session_id = ? and id > ?{active} order by id",
                (session, after),
            ).fetchall()
    except sqlite3.Error as exc:
        _warn_once(str(store), f"session store {store} unreadable ({exc}); following the card's status only", say)
        return []


def nap(deadline: float, interval: float) -> None:
    """Sleep one poll interval, but never past the call's deadline."""
    time.sleep(min(interval, max(0.0, deadline - time.time())))


def cmd_snapshot(args) -> int:
    code = 0
    for task in args.task:
        card = read_card(args.board, task)
        if card is None:
            print(json.dumps({"id": task, "status": "NOT_FOUND"}))
            code = 4
        else:
            print(json.dumps(card, ensure_ascii=False, separators=(",", ":")))
    return code


def graph_card(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    card = {k: row[k] for k in ("id", "title", "status", "assignee")}
    card["parents"] = [r[0] for r in conn.execute(
        "select parent_id from task_links where child_id = ? order by parent_id", (row["id"],))
    ] if table_columns(conn, "task_links") else []
    created = conn.execute(
        "select payload from task_events where task_id = ? and kind = 'created' order by id limit 1",
        (row["id"],),
    ).fetchone() if table_columns(conn, "task_events") else None
    try:
        card["creator"] = (json.loads(created[0]) or {}).get("creator_task_id") if created and created[0] else None
    except (ValueError, TypeError, AttributeError):
        card["creator"] = None
    run = latest_run(conn, row["id"], ("model_used", "fallback_from"))
    card["model"] = (run["model_used"] or "") if run else ""
    if run and run["fallback_from"]:
        card["model"] += f" (fallback from {run['fallback_from']})"
    return card


def board_cards(board: str, tasks: list[str] | None) -> list[dict]:
    with connect(board_db(board)) as conn:
        if tasks:
            rows = conn.execute(
                f"select id, title, status, assignee from tasks where id in ({','.join('?' * len(tasks))})",
                tasks,
            ).fetchall()
        else:
            live = sorted(QUEUED | {"running"})
            rows = conn.execute(
                f"select id, title, status, assignee from tasks where status in ({','.join('?' * len(live))})"
                " order by created_at",
                live,
            ).fetchall()
        return [graph_card(conn, r) for r in rows]


def cmd_cards(args) -> int:
    cards = board_cards(args.board, args.task)
    found = {c["id"] for c in cards}
    missing = [t for t in (args.task or []) if t not in found]
    print(json.dumps({"now": int(time.time()), "cards": cards, "missing": missing}))
    return 4 if missing else 0


def cmd_events(args) -> int:
    watch = dict(pair.split("=", 1) for pair in args.watch.split(",") if "=" in pair)
    # Every card the caller already has, watched or not: a known child is never "new" again.
    known = set(watch) | {t for t in args.known.split(",") if t}
    deadline = time.time() + args.max_seconds
    while True:
        try:
            with connect(board_db(args.board)) as conn:
                current = board_cards(args.board, list(watch))
                fresh_rows = conn.execute(
                    "select id, title, status, assignee from tasks where created_at >= ? and status != 'archived'",
                    (args.since,),
                ).fetchall()
                fresh = [graph_card(conn, r) for r in fresh_rows if r["id"] not in known]
        except sqlite3.Error as exc:
            print(json.dumps({"error": f"board unreadable: {exc}"}))
            return 5
        # A start is reported but never ends the call: each queued card's own wait row
        # already tracks it, and ending here would spend a caller's round on every start.
        changed = [c for c in current if watch.get(c["id"]) in QUEUED and c["status"] not in QUEUED]
        new = [c for c in fresh if c["creator"] in watch or set(c["parents"]) & set(watch)]
        if new:
            print(json.dumps({"now": int(time.time()), "changed": changed, "new": new}))
            return 0
        if current and all(c["status"] in TERMINAL for c in current):
            print(json.dumps({"now": int(time.time()), "changed": changed, "new": [], "all_finished": True}))
            return 2
        if time.time() >= deadline:
            print(json.dumps({"now": int(time.time()), "changed": changed, "new": [], "timeout": True}))
            return 3
        nap(deadline, args.interval)


def cmd_active(args) -> int:
    live = sorted(QUEUED | {"running"})
    with connect(board_db(args.board)) as conn:
        rows = conn.execute(
            f"select id, title, status, assignee from tasks where status in ({','.join('?' * len(live))})"
            " order by created_at",
            live,
        ).fetchall()
    print(json.dumps({"now": int(time.time()), "cards": [dict(r) for r in rows]}))
    return 0


def cmd_wait_start(args) -> int:
    deadline = time.time() + args.max_seconds
    last = None
    while True:
        card = read_card(args.board, args.task)
        if card is None:
            print(f"{args.task}: NOT_FOUND on board {args.board}")
            return 4
        if card["status"] != last:
            print(card_line(card), flush=True)
            last = card["status"]
        if card["status"] not in QUEUED:
            return 0
        if time.time() >= deadline:
            print(f"still {card['status']} - call again", flush=True)
            return 3
        nap(deadline, args.interval)


def cmd_follow(args) -> int:
    deadline = time.time() + args.max_seconds
    output = FollowOutput(args.max_bytes if args.max_bytes is not None else (1500 if args.compact else None))
    cpath = cursor_path(args.board, args.task, args.cursor_key)
    cpath.parent.mkdir(parents=True, exist_ok=True)
    after = int(cpath.read_text()) if cpath.exists() and not args.from_start else 0
    card = read_card(args.board, args.task)
    if card is None:
        output.finish(f"{args.task}: NOT_FOUND on board {args.board}")
        return 4
    output.emit(card_line(card))
    store = session_store(card.get("assignee"))
    if store is None:
        output.emit(f"no session store for {card.get('assignee') or 'unassigned'} yet;"
                    " following the card's status only")
    session = None
    followed: set[str] = set()  # sessions already moved through, so a bad lineage cannot loop
    quiet_since = time.time()

    def finish(status: str):
        output.finish(status)
        # Compact messages stay buffered until finish. A killed call must leave
        # the saved cursor behind them so the next call can replay the batch.
        if args.compact:
            cpath.write_text(str(after))

    def note(line: str):
        # Compact mode holds a note with the tool calls, so it keeps its place among them.
        if args.compact:
            output.hold(line)
        else:
            output.emit(line)

    while True:
        store = store or session_store(card.get("assignee"))
        if store and session is None:
            run_start = (card.get("run") or {}).get("started_at") or card.get("started_at")
            session = find_session(store, args.task, card.get("title"), run_start)
            if session:
                followed.add(session)
            if session and after == 0:
                output.emit(f"session {session} in {store}")
        if store and session:
            rows = new_messages(store, session, after, output.emit)
            if rows and after == 0 and len(rows) > args.backlog:
                output.emit(f"... {len(rows) - args.backlog} earlier messages skipped")
                rows = rows[-args.backlog:]
            if args.compact:
                output.compact(rows)
            else:
                for row in rows:
                    line = render(row)
                    if line:
                        output.emit(line)
            if rows:
                after = rows[-1]["id"]
                if not args.compact:
                    cpath.write_text(str(after))
                quiet_since = time.time()
            child = compression_child(store, session)
            if child and child not in followed:
                followed.add(child)
                # Message ids grow across the whole store, so the cursor stays valid in the child.
                note(f"[{hhmm(time.time())}] context compressed: session {session} continues in {child}")
                session = child
                continue
        previous_run = (card.get("run") or {}).get("id")
        card = read_card(args.board, args.task) or card
        if card["status"] in TERMINAL:
            finish("FINAL " + card_line(card))
            return 0
        if (card.get("run") or {}).get("id") != previous_run:
            # A crash or reclaim started a new run, which writes to a new session.
            note(f"[{hhmm(time.time())}] new run: " + card_line(card))
            session, after = None, 0
            if not args.compact:
                cpath.write_text("0")
        if time.time() - quiet_since > args.quiet_warn:
            # Compact mode holds it with the tool calls, so the live row sees it too.
            note(f"[{hhmm(time.time())}] no new worker messages for {int(time.time() - quiet_since)}s"
                 f" (heartbeat {hhmm(card.get('last_heartbeat_at'))})")
            quiet_since = time.time()
        if time.time() >= deadline:
            finish(f"[{hhmm(time.time())}] still {card['status']} - call again")
            return 3
        nap(deadline, args.interval)


def cmd_scout(args) -> int:
    known = {t for t in args.known.split(",") if t}
    deadline = time.time() + args.max_seconds
    while True:
        try:
            with connect(board_db(args.board)) as conn:
                rows = conn.execute(
                    "select id, title, status, assignee from tasks where created_at >= ?"
                    " and status != 'archived' order by created_at",
                    (args.since,),
                ).fetchall()
                states = {
                    r["id"]: r["status"]
                    for r in conn.execute(
                        f"select id, status from tasks where id in ({','.join('?' * len(known))})",
                        tuple(known),
                    )
                } if known else {}
        except sqlite3.Error as exc:
            print(f"board unreadable: {exc}")
            return 5
        fresh = [dict(r) for r in rows if r["id"] not in known]
        if fresh:
            print(json.dumps({"new": fresh}))
            return 0
        if known and all(states.get(t) in TERMINAL for t in known):
            print(json.dumps({"new": [], "all_terminal": True}))
            return 2
        if time.time() >= deadline:
            print(json.dumps({"new": [], "timeout": True}))
            return 3
        nap(deadline, args.interval)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p, many=False):
        p.add_argument("--board", default="default")
        if many:
            p.add_argument("--task", action="append", required=True)
        p.add_argument("--max-seconds", type=int, default=540)
        p.add_argument("--interval", type=int, default=15)

    p = sub.add_parser("snapshot")
    common(p, many=True)
    p = sub.add_parser("cards")
    p.add_argument("--board", default="default")
    p.add_argument("--task", action="append")
    p = sub.add_parser("events")
    common(p)
    p.add_argument("--watch", required=True, help="id=status,id=status as last seen")
    p.add_argument("--known", default="", help="every other card id already watched; never reported as new")
    p.add_argument("--since", type=int, required=True)
    p = sub.add_parser("active")
    p.add_argument("--board", default="default")
    p = sub.add_parser("wait-start")
    common(p)
    p.add_argument("--task", required=True)
    p = sub.add_parser("follow")
    common(p)
    p.add_argument("--task", required=True)
    p.add_argument("--backlog", type=int, default=15, help="first call: show at most this many earlier messages")
    p.add_argument("--quiet-warn", type=int, default=600, help="say so after this many seconds with no new message")
    p.add_argument("--from-start", action="store_true", help="ignore the saved cursor")
    p.add_argument("--cursor-key", default="", help="separate cursor per watcher, so two watchers never share one")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--compact", action="store_true",
                      help="tool calls and latest assistant text only; default cap 1500 bytes")
    mode.add_argument("--verbose", action="store_true", help="worker text, tool calls and results (the default)")
    p.add_argument("--max-bytes", type=int, help="cap all follow output, including status and overflow count (minimum 128)")
    p = sub.add_parser("scout")
    common(p)
    p.add_argument("--known", default="")
    p.add_argument("--since", type=int, required=True)

    args = parser.parse_args(argv)
    if args.cmd == "follow" and args.max_bytes is not None and args.max_bytes < 128:
        parser.error("--max-bytes must be at least 128 to fit the status and overflow count")
    if not board_db(args.board).exists():
        print(f"board {args.board} has no kanban.db at {board_db(args.board)}")
        return 5
    return {"snapshot": cmd_snapshot, "cards": cmd_cards, "events": cmd_events, "active": cmd_active, "wait-start": cmd_wait_start,
            "follow": cmd_follow, "scout": cmd_scout}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
