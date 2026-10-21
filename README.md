<h1 align="center">kanban-lanes</h1>

<p align="center">
  <strong>Run Hermes Agent Kanban cards as lanes in Claude Code workflows, and watch each one live in /workflows.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License: MIT"></a>
  <a href="https://github.com/anombyte93/kanban-lanes/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/anombyte93/kanban-lanes/ci.yml?branch=main&style=for-the-badge" alt="CI status"></a>
  <a href="https://docs.claude.com/en/docs/claude-code"><img src="https://img.shields.io/badge/Claude%20Code-workflows-D97757?style=for-the-badge" alt="Claude Code"></a>
  <a href="https://github.com/NousResearch/hermes-agent"><img src="https://img.shields.io/badge/Hermes%20Agent-kanban-blueviolet?style=for-the-badge" alt="Hermes Agent"></a>
</p>

<p align="center">
  <a href="#install">Install</a> · <a href="#quick-start">Quick start</a> · <a href="#how-it-works">How it works</a> · <a href="#what-it-costs">Costs</a> · <a href="#faq">FAQ</a>
</p>

You file a card with `hermes kanban create`. A Hermes worker picks it up and runs it on whatever model that profile uses. kanban-lanes turns that card into a row in a Claude Code dynamic workflow, next to your native subagents. You click the row and see the worker's tool calls and latest message as they happen. When the card finishes, the row reports the verdict, the model that ran, how long it took and what it delivered.

It is for you if you already hand long jobs to Hermes workers from Claude Code, and you are tired of tailing a worker in a second terminal to find out whether it is stuck.

It also ships a routing table that names a model for every workflow stage, and a linter that fails any `agent()` call with neither `model` nor `agentType`.

---

## Why

<table>
  <tr>
    <td><b>Kanban work shows up next to your subagents</b></td>
    <td>Each card is a row in <code>/workflows</code>, in its stage, so you stop switching to another terminal to tail a worker.</td>
  </tr>
  <tr>
    <td><b>Long jobs run off your Claude usage</b></td>
    <td>The worker runs on the model your Hermes profile names. Claude only watches. This holds unless your Hermes profile uses the same Claude account, which shares its limits.</td>
  </tr>
  <tr>
    <td><b>Waiting costs about one model turn every 9 minutes</b></td>
    <td><code>scripts/kanban_live.py</code> blocks in Python. A waiting or events call lasts up to 570 seconds. A live call lasts up to 540. The row's model runs the same command again when a call times out.</td>
  </tr>
  <tr>
    <td><b>Scripts open your board read-only</b></td>
    <td>Every script the watcher runs opens the board with <code>mode=ro</code>. The watcher has Bash, so only its prompt keeps it off card actions; see the FAQ.</td>
  </tr>
  <tr>
    <td><b>Watch rows name their model</b></td>
    <td>In the <code>kanban-watch</code> workflow, every row runs as the <code>kanban-watcher</code> agent with its model set, so none inherits your session's model. Rows that wait or read run on Haiku. Only the live row runs on Sonnet.</td>
  </tr>
  <tr>
    <td><b>The linter catches stages that would inherit your session's model</b></td>
    <td>It exits 1 when any <code>agent()</code> call names neither a <code>model</code> nor an <code>agentType</code>. It accepts any <code>agentType</code> by name without opening the agent file, so check that the definition pins a <code>model</code>.</td>
  </tr>
  <tr>
    <td><b>Recover a stopped run instead of rerunning it</b></td>
    <td>The harvester reads a stopped run's journal and returns every finished agent's output, so you do not rerun them.</td>
  </tr>
</table>

---

## Install

```bash
git clone https://github.com/anombyte93/kanban-lanes && cd kanban-lanes && ./install.sh
```

**Requirements:**

- Claude Code with the Workflow tool and custom `agentType` support. No minimum version is known.
- Hermes Agent with `hermes kanban` and a running dispatcher or gateway.
- Python 3.10+.
- node, only for the linter's parse check (`node --check`) and the workflow tests. Tested on node 22.23. Older versions are not tested.

`install.sh` copies the agent, workflow, scripts, skill and example into `~/.claude`. It never edits your `settings.json`. It prints the optional hook snippet for you to paste yourself.

```bash
./install.sh --dry-run     # show every file it would copy, change nothing
./install.sh --uninstall   # remove the files it installed
```

> **The model fields need a Hermes fork.** "Model that actually ran" and fallback come from `task_runs.model_used`, `provider_used` and `fallback_from`, which upstream Hermes does not have. On upstream they are `null` in JSON and `?` in `kanban_live.py` text lines; everything else works.

---

## Quick start

**1. File a card.**

```bash
hermes kanban create "Add retry to the upload client" --assignee <profile>
```

Note the card id it prints. Card ids are `t_` plus 8 hex characters, for example `t_1a2b3c4d`.

**2. Start the watch.** Start a new Claude Code session after `./install.sh`, so it loads the `kanban-watcher` agent type. Then ask it to "run the kanban-watch workflow on board default with task t_1a2b3c4d", or call it by name:

```js
Workflow({
  name: "kanban-watch",
  args: { board: "default", tasks: ["t_1a2b3c4d"] }
})
```

If you pass `scriptPath` instead, give the absolute path that `echo ~/.claude/workflows/kanban-watch.js` prints. Do not pass a path that starts with `~`.

To group cards into stages, pass phases instead:

```js
args: {
  board: "default",
  phases: [
    { title: "Build",  tasks: ["t_1a2b3c4d", "t_5e6f7a8b"] },
    { title: "Review", tasks: ["t_9c0d1e2f"] }
  ]
}
```

`examples/sample-args.json` has a full args file with `follow_new`, `max_hours` and `verbose` set.

**3. Open `/workflows` and click the row.**

The live row is compact by default: the worker's newest tool calls and its latest message, at most 1.5 KB a call. Pass `verbose: true` in args to see every message and tool result.

---

## How it works

```
hermes kanban create ──> Hermes dispatcher ──> worker (your profile's model)
                                                   │
                                  ┌────────────────┴────────────────┐
                                  │                                 │
                      writes card state to the          writes its messages and tool
                      board DB (kanban.db)              calls to the profile session
                                  │                     store (profiles/<assignee>/state.db)
                                  │                                 │
                                  └────────────────┬────────────────┘
                                                   │  both opened read-only
kanban-watch.js ──> kanban-watcher rows ──> kanban_live.py (blocks in Python)
```

The scripts read two SQLite databases under `$HERMES_HOME` (default `~/.hermes`), both read-only. The board DB holds each card's status and runs. The assignee profile's session store, `$HERMES_HOME/profiles/<assignee>/state.db`, holds the worker's messages and tool calls, which is where the live stream comes from.

Each card gets up to three rows:

| Row | When | What you see |
|---|---|---|
| `⏳ waiting` | The card is queued | Nothing until a worker claims it. The row blocks in Python for up to 570 seconds per call. |
| Live row | The worker is running | The worker's newest tool calls and its latest message, at most 1.5 KB per call. A last line counts what it left out. With `verbose: true` you get every message and tool result. When the card stops, the row ends with the report: verdict, the model that ran plus any fallback, duration and deliverable. |
| `⚠` row | The card stops as anything but `DONE` | Shown under **Needs you**, with the reason read from `kanban_live.py snapshot`. This covers `BLOCKED`, `REVIEW`, `ARCHIVED` and `STILL_RUNNING`. If no block reason was recorded, the row says so. |

Cards a worker files while it runs join the run under **Filed by &lt;card&gt;**, so a run that grows is still one view.

`scripts/wave_status.py` prints one JSON snapshot of the cards you name (members, counts, `all_done`). With `--wait` it blocks until they are quiescent. Both scripts use only the Python standard library.

### What is in the repo

| Path | What it does |
|---|---|
| `install.sh` | Copies everything below into `~/.claude`. Supports `--dry-run` and `--uninstall`. |
| `agents/kanban-watcher.md` | The watcher subagent every row runs as. Its prompt forbids every card action. |
| `workflows/kanban-watch.js` | The workflow: board plus card ids or phases in, one row per card out. |
| `scripts/kanban_live.py` | Waits for a card to start, follows it, and snapshots it at the end. |
| `scripts/wave_status.py` | A one-shot or blocking status view of a set of cards. |
| `skills/dynamic-workflow/` | The routing table, the linter and the journal harvester. |
| `examples/mixed-wave.js` | Native subagents and Kanban cards in one workflow run. |
| `examples/sample-args.json` | A sample args file for `kanban-watch.js`, using the phases form. |
| `hooks/kanban-watch-nudge.py` | Optional: reminds Claude to start a watch after `hermes kanban create`. |
| `tests/` | Known-answer tests for the linter, the harvester, `kanban_live.py`, the workflow and the hook. |

### Routing every stage

The `dynamic-workflow` skill holds a routing table: which model and effort each kind of stage gets. A short version:

| Stage kind | `model` | `effort` |
|---|---|---|
| Evidence: file mining, log reads, code survey | `sonnet` | `medium` |
| Mechanical: transforms, JSON reshaping | `haiku` (`sonnet` if it needs judgement) | `low` |
| Harvest of a stopped run | `sonnet` | `low` |
| Design, judge panel, synthesis | `opus` | `high` |
| Implement | `opus` | `high` |
| Adversarial review | `opus` | `xhigh` |
| Report for a human | `opus` | `medium` |
| Long job, over 20 minutes | a Kanban card. In your own script, one `agentType: 'kanban-watcher'` stage (`sonnet`). The `kanban-watch` workflow runs `haiku` rows while it waits and `sonnet` on the live row | `low` |

The full table, with notes, is in `skills/dynamic-workflow/SKILL.md`.

Check a script before you run it:

```bash
python3 ~/.claude/skills/dynamic-workflow/scripts/lint-workflow-script.py my-workflow.js
```

It fails a script when an `agent()` call names neither `model` nor `agentType`. It also checks your top model: the alias in `TOP_TIER` (default `fable`) may appear on at most one `agent()` call, and only one labelled judge, verify or synth.

It prints `PASS`, `FAIL` or `UNKNOWN` and exits 0, 1 or 2. `UNKNOWN` (exit 2) means the linter could not decide: node is missing, the file is unreadable or empty, or it found no `agent()` calls. A usage error also exits 2. If node is missing and another rule fails, the result is `FAIL` with exit 1.

If a run stops part-way, recover what already finished. The Workflow tool prints the transcript dir when it launches a run, as `Transcript dir: .../subagents/workflows/<run>`.

```bash
python3 ~/.claude/skills/dynamic-workflow/scripts/harvest-journal.py --status <transcript dir>
python3 ~/.claude/skills/dynamic-workflow/scripts/harvest-journal.py <transcript dir>
```

The first prints one line per agent. The second prints each agent's result as JSON.

> **Read a live run with `--status` only.** The full harvest labels any agent without a result "stopped", even a live one. Check the transcript age with `--status` before you rerun anything.

---

## What it costs

Measured once, on one board and one machine. Before is the earlier watcher, which printed the full stream; after is this one with its default compact live row.

| | Before | After |
|---|---|---|
| Live row output per call, in a 10-minute watch | about 11 KB of worker text | 1.3 KB |
| Live row context at the end of that watch | 23.5k tokens | 14k tokens |
| Live row first call, three busy finished workers (502-639 messages each) | 34-48 KB | 1.5 KB (the cap) |
| Flag row read | 16.9 KB (`hermes kanban show`) | about 1 KB (`kanban_live.py snapshot`), up to about 2.5 KB for a blocked card |
| Model on idle rows | Sonnet | Haiku |
| Follow calls per hour, per live row | about 12 (300 s each) | about 7 (540 s each) |

Three rows follow from the code: the cap is 1500 bytes, the idle rows pin Haiku, and the call counts are 3600 / 300 and 3600 / 540. The other figures come from that one run. No command log or raw output was kept, so treat them as one observation. A blocked card's snapshot is larger because it carries up to 2000 characters of `block_reason`.

The worker's own model use is billed wherever your Hermes profile points.

> **A watch still spends Claude turns.** A `⏳ waiting` row runs again every 570 seconds, about 6 Haiku turns an hour per queued card. Each live row wakes about every 540 seconds to print what changed, about 7 Sonnet turns an hour. The watch also looks for cards your workers file, every 570 seconds on Haiku. It looks in rounds of up to about an hour, for at most `max_hours` + 2 rounds (default 12, so 14 rounds and about 13 hours). A round ends early when a worker files a card. Pass `follow_new: false` to turn that off.

---

## FAQ

**Does the watcher ever change a card?**
It is not meant to. Its prompt forbids every card action. Every `kanban_live.py` and `wave_status.py` command it runs opens the board with `mode=ro`. It has Bash, so only its prompt keeps it off card actions. The only file it writes is a follow cursor under `${XDG_CACHE_HOME:-~/.cache}/kanban-lanes/live/`. Its prompt never tells it to run `hermes kanban`, because those commands open the board read-write. Upstream Hermes runs its schema setup before every action, so even `hermes kanban log` can add columns to an older board. To unblock or reassign a card, use `hermes kanban` yourself.

**What happens if I close Claude Code mid-run?**
The cards keep running on Hermes. Only the watch stops. Start `kanban-watch` again with the same card ids. On a running card, the live row's first call replays at most the last 15 worker messages (`kanban_live.py follow --from-start --backlog 15`), then follows live. On a finished card, the first call prints the `FINAL` line at once and the row reports. A compact call saves its cursor only after it prints, so a call cut off part-way shows its batch again on the next call. The tests cover these cases on a temp board.

**Can I watch cards I filed earlier, or another board?**
Yes. Pass any card ids and the `board` slug. A card that has already finished gets its report.

**Can one workflow mix native subagents and Kanban cards?**
Yes. In `examples/mixed-wave.js` the Kanban cards run as one `watch:kanban` row in their own Watch stage, next to the native Build and Review stages. That row waits on all the cards with `wave_status.py --wait`. It does not give each card its own row.

**Do I need the hook?**
No. It only reminds Claude to start a watch after you file a card. `install.sh` prints the snippet. You decide whether to add it.

**Why does a stage need a model name at all?**
A stage with no `model` or `agentType` runs on your session's model. If that is your most expensive model, every watcher in the run uses it, even the ones that only wait. The linter catches this before you run the script.

---

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md). Before a PR, run what CI runs: `python3 -m pytest -q tests/`, `bash tests/test_lint.sh`, `bash tests/test_status.sh` and `shellcheck install.sh`.

---

## License

MIT. See [LICENSE](LICENSE).
