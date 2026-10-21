# Dogfood: check the skill on your own scripts

Run these from the repo root after any change to the linter, the harvester or the skill.

```bash
bash tests/test_lint.sh      # known-answer fixtures: PASS, FAIL and UNKNOWN in each direction
bash tests/test_status.sh    # the harvester on a fixed run directory
python3 skills/dynamic-workflow/scripts/lint-workflow-script.py examples/mixed-wave.js
```

Then lint one real script you launched recently. If you launched it inline, copy the script text
into a file first; the linter only reads files.

What a first run usually shows: scripts written without this skill FAIL rule 1 on most calls,
because each `agent()` call inherits the session model.

Record what you ran, the exit codes and the date. Do not record session paths or run ids from a
private machine in a public copy of this file.
