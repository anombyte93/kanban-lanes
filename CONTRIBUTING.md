# Contributing

Keep changes small and tested.

## Before you open a PR

Run the same checks CI runs:

```bash
python3 -m pytest -q tests/
bash tests/test_lint.sh
bash tests/test_status.sh
shellcheck install.sh
```

You need Python 3.10 or newer and Node 22 (the linter's parse check calls `node --check`).

## Rules for changes

- **A new linter rule comes with a fixture and a test.** Add the fixture under `tests/fixtures/`
  and assert its exact output line in `tests/test_lint.sh`. Write the test first and watch it fail.
- **Keep the exit codes.** The linter returns 0 PASS, 1 FAIL, 2 UNKNOWN. A WARN never changes the
  exit code.
- **Read-only stays read-only.** The watcher agent, the watcher workflow and the scripts it runs
  never claim, complete, block, comment on, dispatch or archive a card. A change that writes to a
  board will not be merged.
- **install.sh never edits settings.json.** It prints what to add.
- **Tests run under a temp HOME.** Nothing in `tests/` may write to your real `~/.claude`,
  `~/.hermes` or `~/.cache`.
- **No private data.** No names, hostnames, absolute home paths, tokens or run ids in code, docs
  or fixtures. Use `~` or `$HOME`.

## Writing style

Short plain sentences, second person, concrete numbers and commands.
