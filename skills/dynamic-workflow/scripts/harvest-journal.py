#!/usr/bin/env python3
"""harvest-journal.py TRANSCRIPT_DIR: mine a stopped or finished workflow run instead of re-running it.

Reads journal.jsonl (started/result records) and every agent-*.jsonl transcript, and prints JSON:
{agents: [{id, status, result, last_text, tool_calls, files_read, transcript}], transcript_dir,
journal_records}. Stdlib only.

harvest-journal.py --status TRANSCRIPT_DIR prints one line per agent in start order: its label,
whether it has returned, and for a returned verdict PASS or FAIL with high and medium defect
counts, or for a returned build or fix its commits. An agent with no result is "no result yet"
with the age of its transcript, never "stopped": a live agent and a dead one look the same in the
journal, and the full harvest labels a running agent "stopped". Read live runs with --status.
"""
import glob, json, os, sys, time


def load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def blocks(msg):
    content = (msg.get("message") or {}).get("content")
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def status(d):
    journal = load_jsonl(os.path.join(d, "journal.jsonl")) if os.path.exists(os.path.join(d, "journal.jsonl")) else []
    labels, order, results = {}, [], {}
    for r in journal:
        aid = r.get("agentId")
        if r.get("type") == "started":
            if aid not in labels:
                order.append(aid)
            labels[aid] = r.get("label") or aid
        elif r.get("type") == "result":
            results[aid] = r.get("result")
    lines = []
    for aid in order:
        lab = labels.get(aid) or aid
        if aid not in results:
            t = os.path.join(d, "agent-%s.jsonl" % aid)
            if os.path.exists(t):
                lines.append("%-34s no result yet (transcript updated %ds ago)" % (lab, time.time() - os.path.getmtime(t)))
            else:
                lines.append("%-34s no result yet (no transcript)" % lab)
            continue
        res = results[aid]
        if isinstance(res, dict) and "pass" in res:
            defs = res.get("defects") or []
            hm = sum(1 for x in defs if str(x.get("severity", "")).lower() in ("high", "medium", "critical"))
            lines.append("%-34s %s  defects high/medium %d, total %d" % (lab, "PASS" if res["pass"] else "FAIL", hm, len(defs)))
        elif isinstance(res, dict) and res.get("verdict") in ("PASS", "FAIL") and isinstance(res.get("findings"), list):
            # The verify schema: {verdict: PASS | FAIL, head_sha, findings: [{severity, ...}]}.
            fs = res["findings"]
            hm = sum(1 for x in fs if str(x.get("severity", "")).lower() in ("high", "medium", "critical"))
            lines.append("%-34s %s  head %s  findings high/medium %d, total %d" % (
                lab, res["verdict"], str(res.get("head_sha") or "?")[:8], hm, len(fs)))
        elif isinstance(res, dict) and "commits" in res:
            lines.append("%-34s returned  commits: %s" % (lab, ", ".join(str(c).split()[0] for c in res.get("commits") or []) or "none"))
        elif res is None:
            lines.append("%-34s returned nothing (null)" % lab)
        else:
            lines.append("%-34s returned %s" % (lab, str(res)[:80].replace("\n", " ")))
    return lines


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--status" and os.path.isdir(sys.argv[2]):
        print("\n".join(status(sys.argv[2])) or "no agents started")
        return 0
    if len(sys.argv) != 2 or not os.path.isdir(sys.argv[1]):
        print(json.dumps({"error": "usage: harvest-journal.py [--status] TRANSCRIPT_DIR"})); return 2
    d = sys.argv[1]
    journal = load_jsonl(os.path.join(d, "journal.jsonl")) if os.path.exists(os.path.join(d, "journal.jsonl")) else []
    results = {r.get("agentId"): r for r in journal if r.get("type") == "result"}
    started = [r.get("agentId") for r in journal if r.get("type") == "started"]
    agents = []
    for path in sorted(glob.glob(os.path.join(d, "agent-*.jsonl"))):
        aid = os.path.basename(path)[6:-6]
        rows = load_jsonl(path)
        last_text, tool_calls, files = "", 0, []
        for r in rows:
            if r.get("type") != "assistant":
                continue
            for b in blocks(r):
                if b.get("type") == "text" and b.get("text", "").strip():
                    last_text = b["text"].strip()
                elif b.get("type") == "tool_use":
                    tool_calls += 1
                    inp = b.get("input") or {}
                    for k in ("file_path", "path", "command"):
                        v = inp.get(k)
                        if isinstance(v, str) and k != "command":
                            files.append(v)
                        elif isinstance(v, str) and k == "command":
                            files.extend(t for t in v.split() if t.startswith("/") and len(t) > 8)
        res = results.get(aid)
        agents.append({
            "id": aid,
            "status": "result" if res else ("stopped" if aid in started else "unknown"),
            "result": (res or {}).get("result") if res else None,
            "last_text": last_text[-4000:],
            "tool_calls": tool_calls,
            "files_read": sorted(set(files))[:60],
            "transcript": path,
        })
    print(json.dumps({"transcript_dir": d, "agents": agents, "journal_records": len(journal)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
