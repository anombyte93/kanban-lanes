#!/usr/bin/env python3
"""Read-only watcher token accounting from Claude workflow transcripts (stdlib only)."""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

INPUT_WEIGHT = 1.0
CACHE_WRITE_5M_WEIGHT = 1.25
CACHE_WRITE_1H_WEIGHT = 2.0
CACHE_READ_WEIGHT = 0.1
# Explicit normalisation assumptions, not a price list. Match versioned model IDs
# by family so the report retains the exact model ID without guessing a price.
OUTPUT_RATIOS = {"claude-haiku": 5.0, "claude-sonnet": 5.0, "claude-opus": 5.0}
COLD_WAKE_SECONDS = 300
TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "cache_read_input_tokens",
    "cache_write_5m_tokens", "cache_write_1h_tokens",
)
METRIC_FIELDS = (
    "turns", *TOKEN_FIELDS, "cold_wakes", "cold_cache_write_5m_tokens",
    "cold_cache_write_1h_tokens",
)
WATCHER_TYPE = "kanban-watcher"
UNKNOWN_ROLE = "(unknown)"
RELAY_PREFIX = "[Workflow harness \u2014 user request]"
# Command form only: `python3 [path/]kanban_live.py <subcommand>` or a backtick-quoted
# `[path/]kanban_live.py <subcommand>`. A bare mention (a card title, a reviewer's
# diff) is not a command. The role is the token right after the script, so no
# fixed subcommand list can drift from kanban_live.py's subparsers.
COMMAND_RE = re.compile(
    r"(?:\bpython3?\s+|`)['\"]?(?:[^\s'\"`]*/)?(kanban_live|wave_status)\.py\b['\"]?"
    r"(?:\s+['\"]?([A-Za-z][\w-]*))?")
# A shell-quoted path may hold spaces: "a path/kanban_live.py". Fold it to the bare name first.
QUOTED_PATH_RE = re.compile(r"(['\"])(?:(?!\1)[^\n`])*/((?:kanban_live|wave_status)\.py)\1")


def warn(message):
    print(f"watch_cost: warning: {message}", file=sys.stderr)


def records(path, journal=False):
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                if journal:
                    warn(f"{path}:{number}: invalid JSON; "
                         "journal metadata skipped, transcripts still scanned")
                    continue
                raise ValueError(f"line {number}: invalid JSON") from exc
            if not isinstance(row, dict):
                raise ValueError(f"line {number}: expected an object")
            yield row


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result


def prompt_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block.get("text", "") for block in content
                         if isinstance(block, dict) and block.get("type") == "text")
    return ""


def agent_type(path):
    """agentType from the agent-<id>.meta.json sidecar, or None when absent/unreadable."""
    meta = path.with_name(path.name[:-len(".jsonl")] + ".meta.json")
    if not meta.is_file():
        return None
    try:
        with meta.open(encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        warn(f"{meta}: unreadable meta; classifying from the task prompt")
        return None
    value = data.get("agentType") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def watcher_role(path, prompt, known_watcher):
    """Role from the first command-form script reference in the computed task."""
    commanded = False
    prompt = QUOTED_PATH_RE.sub(r"\2", prompt)
    for match in COMMAND_RE.finditer(prompt):
        commanded = True
        if match.group(1) == "wave_status":
            return "wave_status"
        if match.group(2):
            return match.group(2)
    if known_watcher:
        warn(f"{path}: agentType {WATCHER_TYPE} but no recognised role (no kanban_live.py "
             f"<subcommand> or wave_status.py command); counted under {UNKNOWN_ROLE}")
        return UNKNOWN_ROLE
    if commanded:
        warn(f"{path}: script command has no recognised role; excluded")
    return None


def token_count(usage, key):
    value = usage.get(key, 0) or 0
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"invalid token count for {key}")
    return value


def turn_tokens(usage):
    creation = usage.get("cache_creation") or {}
    total = token_count(usage, "cache_creation_input_tokens")
    five = token_count(creation, "ephemeral_5m_input_tokens")
    hour = token_count(creation, "ephemeral_1h_input_tokens")
    if not creation:
        five = total  # Legacy aggregate-only writes: explicitly assumed 5m.
    elif "cache_creation_input_tokens" in usage and five + hour != total:
        raise ValueError("cache_creation breakdown disagrees with aggregate")
    return {
        "input_tokens": token_count(usage, "input_tokens"),
        "output_tokens": token_count(usage, "output_tokens"),
        "cache_read_input_tokens": token_count(usage, "cache_read_input_tokens"),
        "cache_write_5m_tokens": five,
        "cache_write_1h_tokens": hour,
    }


def weighted(tokens, model):
    ratio = next((ratio for family, ratio in OUTPUT_RATIOS.items()
                  if model == family or model.startswith(family + "-")), None)
    if ratio is None:
        raise ValueError(f"no output ratio configured for model {model!r}")
    return (tokens["input_tokens"] * INPUT_WEIGHT
            + tokens["output_tokens"] * ratio
            + tokens["cache_read_input_tokens"] * CACHE_READ_WEIGHT
            + tokens["cache_write_5m_tokens"] * CACHE_WRITE_5M_WEIGHT
            + tokens["cache_write_1h_tokens"] * CACHE_WRITE_1H_WEIGHT)


def read_agent(path, since):
    kind = agent_type(path)
    if kind is not None and kind != WATCHER_TYPE:
        return None, []  # Meta says it is not a watcher: never parse it.
    known_watcher = kind == WATCHER_TYPE
    role = None
    classified = False
    prompts = []
    first_time = None
    previous_call = None
    turns = {}
    modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    rows = records(path)
    index = -1
    while True:
        try:
            row = next(rows)
        except StopIteration:
            break
        except ValueError as exc:
            if classified or known_watcher:
                raise
            warn(f"{path}: {exc} before the agent was classified; skipped as unclassified")
            return None, []
        index += 1
        time = timestamp(row.get("timestamp"))
        if first_time is None and time is not None:
            first_time = time
            if since is not None and modified < since and first_time < since:
                return None, []  # Do not parse stale tails outside the selection.
        message = row.get("message") or {}
        if not classified:
            # The computed task is every user row before the first reply, minus the
            # harness relay of the triggering user request.
            if row.get("type") == "user":
                text = prompt_text(message.get("content"))
                if not text.lstrip().startswith(RELAY_PREFIX):
                    prompts.append(text)
                continue
            if row.get("type") != "assistant":
                continue
            classified = True
            role = watcher_role(path, "\n".join(prompts), known_watcher)
            if role is None:
                return None, []
        if row.get("type") == "assistant" and message.get("usage"):
            tokens = turn_tokens(message["usage"])
            if message.get("model") == "<synthetic>" and not any(tokens.values()):
                continue  # Harness error notices are not provider completions.
            key = message.get("id") or ("line", index)
            # Claude writes one row per content block. Usage is cumulative for a
            # message ID, not an additional charge per block. Keep the last usage
            # but the first row's cold-wake decision. The cache clock runs from the
            # previous API call, so user rows (tool results arrive seconds before
            # the next call) never reset it.
            if key in turns:
                cold = turns[key][2]
            else:
                cold = (time is not None and previous_call is not None
                        and (time - previous_call).total_seconds() > COLD_WAKE_SECONDS)
                if time is not None:
                    previous_call = time
            turns[key] = (message.get("model") or "unknown", tokens, cold)
    if not classified:
        role = watcher_role(path, "\n".join(prompts), known_watcher)
        if role is None:
            return None, []
    if since is not None:
        if modified < since and (first_time is None or first_time < since):
            return None, []
    return role, list(turns.values())


def empty_metrics():
    return {"agents": 0, **dict.fromkeys(METRIC_FIELDS, 0), "weighted_total": 0.0}


def report(root, runs=(), since=None):
    result = {}
    identities = {}
    found = set()
    for run in sorted(root.glob("*/*/subagents/workflows/*")):
        if not run.is_dir() or (runs and run.name not in runs):
            continue
        found.add(run.name)
        if runs:
            result.setdefault(run.name, {})
            identities.setdefault(run.name, {})
        # Consult the journal as well as the files: a transcript may be present
        # before a started/result event is flushed. Labels are never classifiers.
        agent_ids = set()
        journal = run / "journal.jsonl"
        if journal.exists():
            agent_ids = {row["agentId"] for row in records(journal, journal=True)
                         if row.get("type") in ("started", "result") and row.get("agentId")}
        paths = {path for path in run.glob("agent-*.jsonl") if path.is_file()}
        paths.update(run / f"agent-{agent}.jsonl" for agent in agent_ids
                     if isinstance(agent, str) and re.fullmatch(r"[\w-]+", agent)
                     and (run / f"agent-{agent}.jsonl").is_file())
        for path in sorted(paths):
            try:
                role, turns = read_agent(path, since)
                if role is None:
                    continue
                models = {model for model, _, _ in turns} or {"(no usage)"}
                roles = result.setdefault(run.name, {}).setdefault(role, {})
                for model in models:
                    identities.setdefault(run.name, {}).setdefault(model, set()).add(path)
                    roles.setdefault(model, empty_metrics())["agents"] += 1
                for model, tokens, cold in turns:
                    stats = roles[model]
                    stats["turns"] += 1
                    for field in TOKEN_FIELDS:
                        stats[field] += tokens[field]
                    stats["cold_wakes"] += int(cold)
                    if cold:
                        stats["cold_cache_write_5m_tokens"] += tokens["cache_write_5m_tokens"]
                        stats["cold_cache_write_1h_tokens"] += tokens["cache_write_1h_tokens"]
                    stats["weighted_total"] += weighted(tokens, model)
            except ValueError as exc:
                raise ValueError(f"{path}: {exc}") from exc
        if runs and paths and not identities.get(run.name):
            warn(f"run {run.name}: 0 watcher agents among {len(paths)} transcripts")
    if set(runs) - found:
        raise ValueError("requested run not found: " + ", ".join(sorted(set(runs) - found)))
    return result, identities


def print_text(data, identities):
    # WEIGHTED carries no cross-model price factor, so rows are per run AND model
    # and are never summed across models.
    fields = ("agents", "turns", *TOKEN_FIELDS, "cold_wakes",
              "cold_cache_write_5m_tokens", "cold_cache_write_1h_tokens")
    print("RUN MODEL AGENTS TURNS INPUT OUTPUT CACHE_READ WRITE_5M WRITE_1H COLD COLD_5M COLD_1H WEIGHTED")

    def row(label, model, stats):
        print(label, model, *(str(stats[key]) for key in fields), f'{stats["weighted_total"]:.2f}')

    grand = {}
    grand_agents = {}
    for run, roles in sorted(data.items()):
        per_model = {}
        for models in roles.values():
            for model, stats in models.items():
                total = per_model.setdefault(model, empty_metrics())
                for key in total:
                    total[key] += stats[key]
        if not per_model:
            row(run, "-", empty_metrics())
        for model, total in sorted(per_model.items()):
            agents = identities.get(run, {}).get(model, set())
            total["agents"] = len(agents)
            grand_agents.setdefault(model, set()).update(agents)
            accumulated = grand.setdefault(model, empty_metrics())
            for key in accumulated:
                accumulated[key] += total[key]
            row(run, model, total)
    if not grand:
        row("TOTAL", "-", empty_metrics())
    for model, total in sorted(grand.items()):
        total["agents"] = len(grand_agents[model])
        row("TOTAL", model, total)


def main():
    ratios = ", ".join(f"{model}={ratio}" for model, ratio in OUTPUT_RATIOS.items())
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""Weighted input-equivalent tokens, not dollars:
  input={INPUT_WEIGHT}, cache_write_5m={CACHE_WRITE_5M_WEIGHT},
  cache_write_1h={CACHE_WRITE_1H_WEIGHT}, cache_read={CACHE_READ_WEIGHT}.
  Output/input normalisation ratios by model family: {ratios}.
  These are explicit weighting assumptions, not current vendor prices. Exact
  versioned model IDs match their family; unknown models fail rather than guess.
  WEIGHTED is in each model's own input-token units (no cross-model price
  factor), so text rows are per run and model and never summed across models.

Selection and role:
  agent-<id>.meta.json agentType {WATCHER_TYPE} selects a watcher; any other
  agentType is skipped unread. Without a meta file the computed task (user rows
  before the first reply, minus the harness user-request relay row) must run the
  script in command form: python3 [path/]kanban_live.py <subcommand>, or a
  backtick-quoted [path/]kanban_live.py <subcommand>. A bare mention is not one.
  The role is the token right after the first command-form script reference
  (wave_status.py is role wave_status). A meta-selected watcher with no such
  command warns and is counted under role {UNKNOWN_ROLE}; a command with no
  subcommand and no meta warns and is excluded (e.g. --help).
  An explicit --run that yields 0 watcher agents warns with the transcript count.

Accounting:
  Distinct assistant message IDs are turns; the last cumulative usage per
  message ID wins (rows without an ID count individually). No nested iteration
  usage is added; zero-usage <synthetic> error notices are not API turns.
  JSON per-model agent counts overlap if an agent changes models; text rows
  count each transcript once per model it used.
  Cache write breakdowns are not added again to the aggregate; aggregate-only
  legacy cache writes are assumed 5m. Inconsistent breakdowns fail visibly.
  Cold wakes: the first row of a new message ID minus the first row of the
  previous distinct assistant message ID (the API call that issued the tool) is
  strictly over {COLD_WAKE_SECONDS} seconds. User and tool-result rows never reset it.
  This is a transcript timing proxy, not proof of provider cache expiry.
  Cold write columns are subsets of all writes, not additional charges.
  --since keeps whole agents by transcript file mtime OR first valid timestamp
  at or after midnight UTC. It is not a per-turn date filter.
  Only workflow journal/transcript JSONL is read; kanban.db is never opened.
  Corrupt journal metadata warns on stderr; transcript files are still scanned.
  A JSON error before an agent is classified warns with its path and skips it.
  Corrupt watcher transcripts fail rather than return partial token totals.
""")
    parser.add_argument("--root", type=Path, default=Path.home() / ".claude/projects")
    parser.add_argument("--run", action="append", default=[], help="workflow run ID; repeatable")
    parser.add_argument("--since", help="include agents by file mtime OR first timestamp, from YYYY-MM-DD (UTC)")
    parser.add_argument("--json", action="store_true", help="JSON keyed by run, then role, then exact model ID")
    args = parser.parse_args()
    try:
        since = (datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                 if args.since else None)
        root = args.root.expanduser()
        if not root.is_dir():
            raise ValueError(f"root directory not found: {root}")
        data, identities = report(root, set(args.run), since)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"watch_cost: {exc}\n")
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print_text(data, identities)
    return 0


if __name__ == "__main__":
    sys.exit(main())
