#!/usr/bin/env python3
"""PostToolUse(Bash) hook: after `hermes kanban create` or `hermes kanban swarm`, remind Claude to
launch the kanban-watch workflow, so you can watch the new cards in /workflows like native agents.

Optional. It only adds context: it never blocks, and it prints nothing for any other command.
install.sh prints the settings.json entry to add it; remove that entry to turn it off.

Card ids come only from what hermes printed about the cards it created. A card id is t_ plus 8 or
more hex characters (some Hermes versions print 8, others 12).
  --json output   create: a top-level object with "id", "title" and "status" (the task itself);
                  swarm: a top-level object with root_id, worker_ids, verifier_id, synthesizer_id.
                  A top-level list is skipped (create and swarm never print one; list does), and so
                  is an object that wraps the task (show prints {"task": ...}).
  text output     "Created t_xxxxxxxx ...", "Swarm root: ...", "Workers: ...", "Verifier: ...",
                  "Synthesizer: ..."
Ids inside a --body text, a parent link or a title are never read. When the command has no loop
(for, while, until, xargs,
parallel), the hook reads at most one JSON result per create or swarm call it
counts in the command, so a later --json command that prints a task-shaped object is not reported.
It is a heuristic over printed text: it can still miss a card or, in a loop that also runs other
--json commands, name one that was not new.
"""
import json
import os
import re
import sys

FILED = re.compile(r"\bhermes\b[^|;&]*\bkanban\b[^|;&]*\b(create|swarm)\b")
LOOP = re.compile(r"(?:^|[\s;&|(])(?:for|while|until|xargs|parallel)\b")
BOARD = re.compile(r"--board[ =]([^\s;&|]+)")
CARD_ID = re.compile(r"^t_[0-9a-f]{8,}$")
TEXT_LINE = re.compile(r"^\s*(?:Created|Swarm root:|Workers:|Verifier:|Synthesizer:)\s+(.*)$")
TEXT_ID = re.compile(r"\bt_[0-9a-f]{8,}\b")
SWARM_KEYS = ("root_id", "verifier_id", "synthesizer_id")
TASK_KEYS = ("id", "title", "status")


def workflow_path() -> str:
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join("~", ".claude")
    return os.path.expanduser(os.path.join(config, "workflows", "kanban-watch.js"))


def ids_from_json(obj) -> list:
    """Ids of the cards one create or swarm --json result describes, or [] when obj is not shaped
    like one. Only the known id keys at the top level; never "body", "parents" or nested text."""
    if not isinstance(obj, dict):
        return []
    if "root_id" in obj:
        found = [obj[k] for k in SWARM_KEYS if isinstance(obj.get(k), str) and CARD_ID.match(obj[k])]
        workers = obj.get("worker_ids")
        if isinstance(workers, list):
            found[1:1] = [w for w in workers if isinstance(w, str) and CARD_ID.match(w)]
        return found
    if all(k in obj for k in TASK_KEYS) and "task" not in obj:
        v = obj["id"]
        return [v] if isinstance(v, str) and CARD_ID.match(v) else []
    return []


def card_ids(stdout: str, max_results=None) -> list:
    """Walk stdout: decode each JSON value hermes printed, and read the create/swarm text lines.
    A loop of several create calls prints several values or lines; all are collected. With
    max_results, at most that many JSON values that look like a create or swarm result are read."""
    decoder = json.JSONDecoder()
    found, i, n, results = [], 0, len(stdout), 0
    while i < n:
        if stdout[i].isspace():
            i += 1
            continue
        if stdout[i] in "{[":
            try:
                obj, end = decoder.raw_decode(stdout, i)
            except ValueError:
                obj, end = None, -1
            if end > i:
                ids = ids_from_json(obj)
                if ids and (max_results is None or results < max_results):
                    found.extend(ids)
                    results += 1
                i = end
                continue
        nl = stdout.find("\n", i)
        nl = n if nl < 0 else nl
        m = TEXT_LINE.match(stdout[i:nl])
        if m:
            found.extend(TEXT_ID.findall(m.group(1)))
        i = nl + 1
    return list(dict.fromkeys(found))


def stdout_of(response) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        out = response.get("stdout")
        return out if isinstance(out, str) else ""
    return ""


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(event, dict) or event.get("tool_name") != "Bash":
        return 0
    command = str((event.get("tool_input") or {}).get("command", ""))
    if not FILED.search(command) or "--help" in command:
        return 0
    calls = None if LOOP.search(command) else len(FILED.findall(command))
    cards = card_ids(stdout_of(event.get("tool_response")), calls)
    if not cards:
        return 0
    found = BOARD.search(command)
    board = found.group(1).strip("'\"") if found else "default"
    if board.startswith("$"):
        # A shell variable: the hook cannot see its value, so name it rather than guess.
        board = f"<the board in {board}>"
    note = (
        f"Kanban card(s) were just filed on board {board}: {', '.join(cards)}. "
        f"Once this wave's cards are all filed and dispatched, launch ONE watcher for the whole wave, "
        f"with its stages as phases, so each card shows as a live row in /workflows: "
        f'Workflow({{scriptPath: "{workflow_path()}", args: {{board: "{board}", '
        f'phases: [{{title: "<stage>", tasks: [...]}}]}}}}). '
        f"Skip this if a kanban-watch run already covers these cards, or if a watched card filed them."
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": note}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
