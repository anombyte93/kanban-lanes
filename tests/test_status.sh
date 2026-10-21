#!/usr/bin/env bash
# harvest-journal.py on a fixed run directory.
#   --status: build commits, a FAIL verdict with one medium, an agent with no result, and a
#   verify-schema FAIL with one high.
#   full harvest: the one agent with a transcript returns its result, tool count and files read.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
H="$here/../skills/dynamic-workflow/scripts/harvest-journal.py"
D="$here/fixtures/status-run"

out=$(python3 "$H" --status "$D")
echo "$out"
grep -q "build:x .*commits: abc1234" <<<"$out"
grep -q "review:x .*FAIL  defects high/medium 1, total 2" <<<"$out"
grep -q "fix:x .*no result yet (no transcript)" <<<"$out"
grep -q "verify:x .*FAIL  head deadbeef  findings high/medium 1, total 2" <<<"$out"

full=$(python3 "$H" "$D")
python3 - "$full" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
assert d["journal_records"] == 8, d["journal_records"]
[a] = d["agents"]
assert a["id"] == "a1" and a["status"] == "result", a
assert a["result"]["commits"] == ["abc1234 feat: x"], a["result"]
assert a["tool_calls"] == 2, a["tool_calls"]
assert a["last_text"] == "Committed abc1234.", a["last_text"]
assert a["files_read"] == ["/repo/src/app.py", "/repo/tests/test_app.py"], a["files_read"]
PY

set +e
python3 "$H" /nonexistent-dir >/dev/null; rc=$?
set -e
[ "$rc" -eq 2 ] || { echo "FAIL: missing dir rc=$rc (want 2)"; exit 1; }
echo PASS
