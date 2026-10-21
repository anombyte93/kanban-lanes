#!/usr/bin/env bash
# Known-answer tests for skills/dynamic-workflow/scripts/lint-workflow-script.py.
#   bad.js          FAIL: rule1 (no model) and rule2 (top tier twice); no rule3 by default
#   bad.js          with --require-header also FAILs rule3
#   unparseable.js  FAIL: rule0 at line 3
#   good.js         PASS, no WARN; FAILs rule3 only when --require-header asks for a marker line
#   header.js       PASS with --require-header (bare, TEXT without //, and =TEXT); FAIL on another TEXT
#   empty TEXT      '--require-header ""' and '--require-header=' are both UNKNOWN (usage error)
#   clock.js        FAIL: rule4 Date.now()
#   installs.js     PASS with exactly three rule7 WARNs (line 4 npm ci, line 8 yarn install,
#                   line 9 vitest with no reuse line); the reuse recipe via ${RULES}, the negated
#                   mention and the pnpm --store-dir install stay quiet
#   examples/mixed-wave.js  PASS, no WARN
#   missing file and bad usage  UNKNOWN (exit 2)
set -u
S="$(cd "$(dirname "$0")/.." && pwd)"
L="$S/skills/dynamic-workflow/scripts/lint-workflow-script.py"
F="$S/tests/fixtures"
fail=0; cases=0
check() { cases=$((cases + 1)); }

check; out=$(python3 "$L" "$F/bad.js"); rc=$?
[ $rc -eq 1 ] || { echo "FAIL: bad.js rc=$rc (want 1)"; fail=1; }
grep -q "rule1: agent() at line 2" <<<"$out" || { echo "FAIL: bad.js missing rule1 hit: $out"; fail=1; }
grep -q "rule2: fable on more than one" <<<"$out" || { echo "FAIL: bad.js missing rule2 hit: $out"; fail=1; }
grep -q "rule3" <<<"$out" && { echo "FAIL: bad.js has a rule3 hit without --require-header: $out"; fail=1; }

check; out=$(python3 "$L" --require-header "$F/bad.js"); rc=$?
[ $rc -eq 1 ] || { echo "FAIL: bad.js --require-header rc=$rc (want 1)"; fail=1; }
grep -q "rule3: line 1 must start with '// WORKFLOW-EXEMPT:'" <<<"$out" || { echo "FAIL: bad.js --require-header missing rule3 hit: $out"; fail=1; }

check; out=$(python3 "$L" "$F/unparseable.js"); rc=$?
[ $rc -eq 1 ] || { echo "FAIL: unparseable.js rc=$rc (want 1)"; fail=1; }
grep -q "rule0: script does not parse at line 3" <<<"$out" || { echo "FAIL: unparseable.js missing rule0 hit: $out"; fail=1; }

check; out=$(python3 "$L" "$F/good.js"); rc=$?
[ $rc -eq 0 ] || { echo "FAIL: good.js rc=$rc (want 0): $out"; fail=1; }
grep -q "WARN" <<<"$out" && { echo "FAIL: good.js has a WARN: $out"; fail=1; }

check; out=$(python3 "$L" --require-header "$F/good.js"); rc=$?
[ $rc -eq 1 ] || { echo "FAIL: good.js --require-header rc=$rc (want 1): $out"; fail=1; }
grep -q "rule3" <<<"$out" || { echo "FAIL: good.js --require-header missing rule3 hit: $out"; fail=1; }

check; out=$(python3 "$L" --require-header "$F/header.js"); rc=$?
[ $rc -eq 0 ] || { echo "FAIL: header.js --require-header rc=$rc (want 0): $out"; fail=1; }

check; out=$(python3 "$L" --require-header "WORKFLOW-EXEMPT:" "$F/header.js"); rc=$?
[ $rc -eq 0 ] || { echo "FAIL: header.js --require-header TEXT rc=$rc (want 0): $out"; fail=1; }

check; out=$(python3 "$L" "--require-header=// REVIEWED:" "$F/header.js"); rc=$?
[ $rc -eq 1 ] || { echo "FAIL: header.js --require-header=other rc=$rc (want 1): $out"; fail=1; }
grep -q "rule3: line 1 must start with '// REVIEWED:'" <<<"$out" || { echo "FAIL: header.js missing custom rule3 hit: $out"; fail=1; }

check; out=$(python3 "$L" --require-header "" "$F/good.js"); rc=$?
[ $rc -eq 2 ] || { echo "FAIL: --require-header '' rc=$rc (want 2, empty TEXT is a usage error): $out"; fail=1; }
grep -q "TEXT is empty" <<<"$out" || { echo "FAIL: --require-header '' missing usage message: $out"; fail=1; }

check; out=$(python3 "$L" "--require-header=" "$F/good.js"); rc=$?
[ $rc -eq 2 ] || { echo "FAIL: --require-header= rc=$rc (want 2, empty TEXT is a usage error): $out"; fail=1; }
grep -q "TEXT is empty" <<<"$out" || { echo "FAIL: --require-header= missing usage message: $out"; fail=1; }

check; out=$(python3 "$L" "$F/clock.js"); rc=$?
[ $rc -eq 1 ] || { echo "FAIL: clock.js rc=$rc (want 1): $out"; fail=1; }
grep -q "rule4: Date.now()" <<<"$out" || { echo "FAIL: clock.js missing rule4 hit: $out"; fail=1; }

check; out=$(python3 "$L" "$F/installs.js"); rc=$?
[ $rc -eq 0 ] || { echo "FAIL: installs.js rc=$rc (want 0, WARN never fails): $out"; fail=1; }
grep -q "WARN rule7: prompt text at line 4 runs 'npm ci'" <<<"$out" || { echo "FAIL: installs.js missing line-4 npm ci WARN: $out"; fail=1; }
grep -q "WARN rule7: prompt text at line 8 runs 'yarn install'" <<<"$out" || { echo "FAIL: installs.js missing line-8 yarn WARN: $out"; fail=1; }
grep -q "WARN rule7: prompt text at line 9 names 'vitest'" <<<"$out" || { echo "FAIL: installs.js missing line-9 vitest WARN: $out"; fail=1; }
[ "$(grep -c "WARN rule7" <<<"$out")" -eq 3 ] || { echo "FAIL: installs.js want exactly 3 rule7 WARNs: $out"; fail=1; }

if [ -f "$S/examples/mixed-wave.js" ]; then
  check; out=$(python3 "$L" "$S/examples/mixed-wave.js"); rc=$?
  [ $rc -eq 0 ] || { echo "FAIL: examples/mixed-wave.js rc=$rc (want 0): $out"; fail=1; }
  grep -q "WARN" <<<"$out" && { echo "FAIL: examples/mixed-wave.js has a WARN: $out"; fail=1; }
fi

check; python3 "$L" /nonexistent.js >/dev/null; rc=$?
[ $rc -eq 2 ] || { echo "FAIL: missing file rc=$rc (want 2)"; fail=1; }

check; python3 "$L" >/dev/null; rc=$?
[ $rc -eq 2 ] || { echo "FAIL: no arguments rc=$rc (want 2)"; fail=1; }

check; python3 "$L" --bogus "$F/good.js" >/dev/null; rc=$?
[ $rc -eq 2 ] || { echo "FAIL: unknown flag rc=$rc (want 2)"; fail=1; }

[ $fail -eq 0 ] && echo "PASS: lint known-answer ($cases cases)"
exit $fail
