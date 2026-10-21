---
name: dynamic-workflow
description: Use before you author, launch or re-run a Claude Code Workflow (dynamic workflow) script. Routes every agent() stage to the cheapest model that can do it (haiku or sonnet for evidence and mechanical stages, opus for design, implementation and review, the top tier on at most one judge stage, a Kanban card with a `sonnet` watcher for jobs over 20 minutes), keeps the script inside the Workflow sandbox, lints it before launch, and harvests a stopped run instead of re-running it.
---

# Dynamic workflow: route every stage to the model that fits

An `agent()` call with no `model` inherits the session's model. If your session runs on the most
expensive tier, every stage without a `model` runs there too, scouts and log reads included.
In one run, on one machine, two workflows with about ten evidence and design agents ran for 20
minutes on the top tier before they were stopped. The evidence stages were file mining that sonnet
does.

Use the routing table below to name a model and effort for every stage. Lint the script before
you launch it. If a run stops, harvest its transcripts instead of re-running it. The scripts live
next to this file:

| Script | In this repo | Installed |
|---|---|---|
| Linter | `skills/dynamic-workflow/scripts/lint-workflow-script.py` | `~/.claude/skills/dynamic-workflow/scripts/lint-workflow-script.py` |
| Harvester | `skills/dynamic-workflow/scripts/harvest-journal.py` | `~/.claude/skills/dynamic-workflow/scripts/harvest-journal.py` |

A build that splits into parallel lanes, some native and some on Kanban cards, is a mixed wave.
Start it from the template and read the guide. This skill still routes its stages and lints it.

| File | In this repo | Installed | Online |
|---|---|---|---|
| Template | `examples/mixed-wave.js` | `~/.claude/kanban-lanes/examples/mixed-wave.js` | <https://github.com/anombyte93/kanban-lanes/blob/main/examples/mixed-wave.js> |
| Guide | `docs/mixed-waves.md` | `~/.claude/kanban-lanes/docs/mixed-waves.md` | <https://github.com/anombyte93/kanban-lanes/blob/main/docs/mixed-waves.md> |

The installed paths use the default `KANBAN_LANES_HOME`. If you set it, look there instead.

## Routing table: pick the row, then set `model` and `effort` explicitly

| Stage kind | `model` | `effort` | Notes |
|---|---|---|---|
| Evidence: file mining, log reads, code survey, OCR | `sonnet` | `medium` | schema output; a read-only RULES block in the prompt |
| Mechanical: transforms, JSON reshaping, fixture generation | `haiku` (`sonnet` if it needs judgement) | `low` | |
| Harvest of a stopped run's transcripts | `sonnet` | `low` | one agent per stopped transcript, or one for the journal |
| Design under a lens (cost-first, ops-first, risk-first) | `opus` | `high` | independent designers in `parallel()` |
| Judge panel, synthesis of designs | `opus` | `high` | the top tier (`fable`) only when the decision is safety-critical and you asked for depth; at most once per workflow, on a judge stage |
| Implement (code and tests, own files only) | `opus` | `high` | `isolation: 'worktree'` only when parallel writers would collide |
| Adversarial review, refute | `opus` | `xhigh` | a different agent from the implementer; `pipeline()` it after each implement |
| Report, synthesis for a human | `opus` | `medium` | plain structured text |
| Isolated long job (over 20 minutes, needs its own checkout, or unrelated to the lane) | a **Kanban card**, not an agent | | the parent session files the card; a `sonnet` watcher (`agentType: 'kanban-watcher'`) waits on the exact card ids and reports. The script never files, accepts or merges a card |

An `agentType` passes the linter by name. Check that the agent definition pins `model` in its
frontmatter, or it inherits the session model too. `agents/kanban-watcher.md` pins `sonnet`.

## Lessons that bind every script

**Only the adversarial stage may overturn a finding.** A stage returns its own schema's fields and
nothing else. A scout handed a finding so it can search well has what it needs to re-judge that
finding, and because it runs after the review stage, its answer is the last one you read. A
scout that re-judges can be wrong with confidence: for example, it reports that a bug does not
reproduce because it tested the working tree instead of the commit under review, while the
finding was right all along. Put this line in every post-review stage's prompt: *"You are not re-adjudicating the
finding. If you believe it is wrong, say so in `out_of_scope_note`; do not restate the verdict."*
Give those stages an `out_of_scope_note` field, and treat what lands there as unverified until you
check it yourself.

**Workflow agents see your recent instructions, so scope each brief.** In one run, a brief said
"only read the report in the inbox" about where a source report came from. A research lane read
that as a ban on all desk research and returned nothing, and the next stage had no data. A 40-minute re-run
followed. When a constraint is in the conversation, every brief says what it covers and what it
does not ("the constraint governs X; desk research on Y is allowed").

**Put another session's binding facts in the brief as facts.** If another session or document
already holds facts about the same subject, read them before you write the brief. A brief that
leaves them out produces a plan that contradicts them, and someone gets asked a question they
already answered.

**Run tests under a temp HOME, with the XDG dirs too.** `HOME` alone is not enough: if your shell
exports absolute `XDG_CACHE_HOME`, `XDG_CONFIG_HOME` or `XDG_DATA_HOME`, anything that follows XDG
still writes to your live dirs. Put the temp HOME under the stage's own work dir, not a bare
`mktemp -d`. The run line:

```bash
T=$DIR/home; mkdir -p "$T"
HOME=$T XDG_CACHE_HOME=$T/.cache XDG_CONFIG_HOME=$T/.config \
  XDG_DATA_HOME=$T/.local/share XDG_STATE_HOME=$T/.local/state <test command>
```

Name your live `~/.cache`, `~/.local` and `~/.config` as off-limits in the RULES block. In one run a
reviewer's mutation test dropped a destination argument, and the suite's defaults (bound to
`Path.home()` at import) wrote test data into a live cache for about 25 seconds. If a suite
defaults to live paths, have the implementer pin those defaults to a temp dir in setUp.

**Hard-link `node_modules` with `cp -al`; never reinstall it.** See § Dependencies below.

**Plan mode in the parent session stalls every running writer.** A fixer returned "Nothing fixed:
plan mode switched on mid-task and blocks every edit except the plan file" and left its edits
uncommitted. Before you enter plan mode, name the running write lanes. After it ends, re-run those
fix rounds from a new script: `resumeFromRunId` replays the cached "nothing fixed" result for an
unchanged prompt.

**A usage-limit kill returns null, and the work is still on disk.** A weekly limit killed an
integrator agent. The script's guard threw "the integrator returned nothing", but the agent had
already made every merge and all eight integration commits, and its test logs ended `EXIT=0`.
Before you re-run, read the worktree (`git log`, `git status`) and the lane's log dir, and finish
the last step yourself. Until the limit resets, no new `agent()` call runs, so say which stages ran
and which did not.

**Resume needs the original args.** `resumeFromRunId` with the same `scriptPath` but no `args`
changes every prompt that reads `args`, so nothing comes from cache and the whole run starts
again. Copy the `args` from the launch call.

**Write the script to a file, lint it, launch it with `scriptPath`.** The Workflow tool's own
reference may tell you to pass the script inline. The linter only reads a file, and an unrouted
script has already spent top-tier tokens by the time you could lint it afterwards. So: write it to
your scratch dir, lint it, then call `Workflow({scriptPath: "<that file>", args: {...}})`.

## The Workflow sandbox

A Workflow script runs in a locked-down sandbox, and it only orchestrates. It has no clock, no
randomness, no filesystem and no network. Anything real (read or write a file, get the date, call
a tool) is a job for an `agent()` stage.

| Rule | Why |
|---|---|
| `export const meta = {...}` is the first statement and a **pure literal** | it is evaluated in an empty sandbox: no variables, calls, spreads or template interpolation |
| Plain JavaScript only, no TypeScript | the body is syntax-checked; `: type` annotations, interfaces and generics fail to parse |
| No `Date.now()`, `new Date()`, `Date()`, `Math.random()` | they throw, because resume must be deterministic. Pass a timestamp in `args`, or have an agent run `date` |
| No `fs`, `require`, `import`, `process`, `eval`, `new Function` | there is no Node or filesystem access; an agent writes the file and returns its path |
| `isolation: 'worktree'` only for parallel writers | it is the built-in fix when parallel stages edit the same repo. Do not hand-roll a file-conflict heuristic; do not pay for a worktree on read-only stages |
| Pass `args` as JSON values | `args` arrives as data, not code. Read it with a default: `const X = (args && args.x) ?? DEFAULT` |
| Escape backticks inside prompts (`` \` ``) | an unescaped backtick ends the template literal and the script fails to parse |
| Branch on an `enum`, not a regex | give the schema `enum: ['real', 'not_real']` and compare with `===`; `/real/` also matches "not real" |

Common asks, mapped:

| You want | Do this |
|---|---|
| "let me choose X when I run it" | read `X` from `args` with a default; never hardcode it |
| "save it to a file" | an agent writes the file and replies with the path |
| "note when it ran" | pass a timestamp in `args`, or an agent runs `date` |
| "do these N at once" | `parallel()` over the list; `pipeline()` when each item has dependent stages |
| "they overwrite each other" | `isolation: 'worktree'` on the writing stage |
| "add a step" | a new `phase('Name')` and an `agent()` call; add the phase to `meta.phases` |
| "only return the real ones" | an `enum` verdict and an exact comparison |

## Rules the linter checks

```bash
python3 ~/.claude/skills/dynamic-workflow/scripts/lint-workflow-script.py <script.js>
# exit 0 PASS, 1 FAIL (each broken rule named with its line), 2 UNKNOWN
```

0. **The script parses.** `node --check` on the body, wrapped the way the runtime runs it (an async
   function body, `export const meta` as a plain const). UNKNOWN when `node` is missing.
1. **Every `agent()` call carries `model`** (or an `agentType` whose definition pins one).
2. **The top tier (`fable`) appears on at most one call**, and that call's label contains `judge`,
   `verify` or `synth`. Change `TOP_TIER` in the script if your model names differ.
3. **Opt-in: a marker on line 1.** Off by default. If a PreToolUse hook of yours refuses Workflow
   scripts without a marker comment, add `--require-header` (wants `// WORKFLOW-EXEMPT:`) or
   `--require-header=TEXT` (wants `// TEXT`). An empty TEXT is a usage error (exit 2).
4. **`export const meta` comes first; no `Date.now()`, `Math.random()`, `new Date()`.**
7. **WARN only: verify and review lanes reuse dependencies.** It warns on a prompt that runs
   `npm install`, `npm ci`, `pnpm install`, `yarn install` or `bun install` with no reuse recipe,
   and on a prompt that names a JS test or build runner (`vitest`, `jest`, `tsc`, `npm test`,
   `npm run ...`, `next build`, `playwright`) and says nothing about dependencies. A reuse line in
   a shared `${RULES}` constant counts for every prompt that interpolates it. WARN lines do not
   change the exit code.

Two rules are yours to keep, because no linter can read them:

5. Every prompt that touches a live system carries a RULES block: read-only boundaries, never send
   input to a live app, evidence first.
6. Every stage that runs tests or mutation experiments uses the temp HOME line above.

`PASS: <path> (N agent() calls)` counts `agent(` call sites in the source, not the runtime
fan-out. Three call sites inside `pipeline()` over a 6-item list spawn far more than 3 agents. The
number is syntax coverage, never a cost estimate. Call sites inside `//` comments count too, so
write "agent call" in comments.

## Dependencies: reuse, never reinstall (rule 7)

In one run, on one machine, verify lanes that copied a tree and then installed into the copy cost
over 1 GB each, and a day of those rounds filled more than 10 GB. A `cp -al` copy of a node_modules
that size cost tens of MB of directory entries and well under a minute. Measure your own tree
before you plan around these numbers.

`SRC` is the checkout whose `node_modules` is installed. `DST` is the lane's copy under its own
work dir. Put these lines in the lane's RULES block:

```bash
# 1. Lockfile equality: reuse only when every lockfile present is byte-identical.
same=yes; for f in package-lock.json npm-shrinkwrap.json pnpm-lock.yaml yarn.lock bun.lock bun.lockb; do
  if [ -e "$SRC/$f" ] || [ -e "$DST/$f" ]; then cmp -s "$SRC/$f" "$DST/$f" || same=no; fi
done; echo "lockfile-equal=$same"
# 2. Default: hard-link copy. Near-zero bytes; files the lane creates stay in its copy.
[ "$same" = yes ] && cp -al "$SRC/node_modules" "$DST/node_modules"
# 3. Prove the tree resolves before you trust a test result (under the temp HOME).
(cd "$DST" && npm ls --depth=0 >/dev/null) && echo deps-ok
```

- **Git worktree of the same repo.** `git -C "$SRC" worktree add "$DIR/wt" <ref>`, run the
  lockfile check with `DST=$DIR/wt`, then `cp -al`. A symlink (`ln -sfn "$SRC/node_modules"
  "$DIR/wt/node_modules"`) works only when the lane never writes into `node_modules`: Vite and
  Vitest write `node_modules/.vite`, and through a symlink that lands in the shared tree.
- **Same filesystem.** `cp -al` fails across filesystems with "Invalid cross-device link". Keep
  scratch dirs and worktrees on the same filesystem as the checkout.
- **pnpm.** Resolve the store with your real HOME before you swap HOME:
  `STORE=$(pnpm store path)`, then `pnpm install --frozen-lockfile --offline --store-dir "$STORE"`.
  Under a temp HOME, `pnpm store path` moves into the temp dir and every package downloads again.
- **Hard links share inodes.** A tool that rewrites a file in place (patch-package, a postinstall
  that edits) changes `SRC` too. If the lockfile differs, or the lane must add a dependency, it
  does a real install and says so.
- **Clean up on return.** `rm -rf -- "$DST/node_modules"` for a `cp -al` copy (drops links only).
  `unlink "$DST/node_modules"` for a symlink; never `rm -rf "$DST/node_modules/"` with a trailing
  slash, which follows the link into the shared tree. For a worktree, `unlink` first, then
  `git -C "$SRC" worktree remove --force "$DIR/wt"`.

## Harvest a stopped run; do not re-run it

Before you re-run a stopped, killed or mis-routed workflow:

```bash
python3 ~/.claude/skills/dynamic-workflow/scripts/harvest-journal.py <transcriptDir> > harvest.json
```

It reads `journal.jsonl` and every `agent-*.jsonl` transcript and prints, per agent: id, status
(`result` or `stopped`), the structured result when one landed, the last assistant text, the tool
call count and the files it touched. Pass that JSON (or its path) to the new run in `args`, and
give a `sonnet` harvest stage the transcript paths. An evidence stage whose transcript already
holds the answer is not re-run. The Workflow tool prints the transcript dir at launch.

To check a live or finished run:

```bash
python3 ~/.claude/skills/dynamic-workflow/scripts/harvest-journal.py --status <transcriptDir>
```

One line per agent in start order: its label, PASS or FAIL with high and medium defect counts for
a verdict, the commits for a build or fix, or "no result yet" with the transcript's age. The full
harvest calls a running agent "stopped", so read live runs with `--status`. A task's `.output` file
can truncate a long result to invalid JSON; `--status` reads the journal instead.

## Procedure

1. Write the script with the routing table. Every `agent()` gets `model` and `effort`, or an
   `agentType` that pins a model.
2. Write it to a file and lint it. Do not launch on FAIL. Fix every `WARN rule7`, or say in your
   launch note why the lane needs a real install.
3. On a redo, harvest first and pass `args: {harvest: <json>}`.
4. Launch with `scriptPath`. Watch `/workflows`. When the run returns, read `journal.jsonl` before
   you believe an empty result.
5. In your report, say which stages ran on which model. One run proves one runtime and one host.

## Blind spots

- The linter reads text, not the runtime. An `agentType` that inherits the session model passes
  rule 1 by name only.
- The Kanban row assumes the board's dispatcher is running and the card is assigned. An unassigned
  card is skipped silently: `hermes kanban assign <id> <profile>` before you dispatch.
- Rule 7 reads only template-literal prompts. A quoted-string prompt (`'...'`) is not scanned, and a
  lane that installs on its own with no runner named gets past it. Put the reuse line in the RULES
  block anyway.
- A workflow stopped before its first tool call leaves nothing to harvest; the harvester says so
  per agent.
