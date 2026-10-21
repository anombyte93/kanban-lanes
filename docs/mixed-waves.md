# Mixed waves: native lanes and Kanban cards in one build

Run some lanes of a build as native Workflow agents and the rest as Hermes Kanban cards, watch both
in `/workflows`, and merge them natively at the end.

The script for it is `examples/mixed-wave.js` (installed at
`~/.claude/kanban-lanes/examples/mixed-wave.js`). Lint it before every launch:

```bash
python3 ~/.claude/skills/dynamic-workflow/scripts/lint-workflow-script.py my-wave.js
```

## Which lanes go where

| Native lane (Workflow agent) | Kanban lane (card) |
|---|---|
| shared code other lanes consume | one page, screen or doc |
| many files, or a state machine | runs over 20 minutes, or needs its own checkout |
| privacy or security rules that need close review | a different model family is cheaper or better at it |
| needs the parent's context | unrelated to the other lanes |

Wave 0 (contracts) and wave 2 (integration) always stay native.

## The waves

1. **Plan.** Write a spec, then a plan whose header carries: Global Constraints, a Fixed
   interfaces table (exact names and shapes), a Words section (every new user-facing line,
   verbatim), a Review Focus (numbered edge cases, each with an owning lane) and the lane table
   (lane, native or Kanban, model, owned files, interfaces consumed and produced, tests). A lane
   that needs a different interface stops and reports. It never invents one.

2. **Wave 0: contracts, native, opus.** Land every migration, the data layer on each platform,
   feature switches, copy and placeholder components with fixed props as one commit. Every lane
   branches from that commit and builds against a frozen shape. No lane adds a migration or
   touches another lane's files. Only the parent ships. `examples/mixed-wave.js` runs this stage
   when you omit `args.base`; pass `base` when you landed it yourself.

3. **Wave 1: lanes in parallel, both halves launched in the same turn.**
   - **Native lanes** run inside the Workflow script. Each implement stage uses `model: 'opus'`.
     When the main checkout is shared with other live sessions, each lane's brief creates its own
     `git worktree add -b <lane> <scratch>/<lane> <wave 0 commit>` and never touches the main
     checkout. Each lane runs build, then a fresh adversarial review, then a fix, and after every
     fix a fresh re-review of the new pushed head with the same schema. Review and fix loop at
     most twice, then the lane reports. A lane is landable only when its **last** review passed,
     so "reviewed" always means the head that would land. A `fix_needed` verdict with no findings
     never reaches a fixer: the lane stops with `landable: false` and `blocked` says why.
   - **Kanban lanes** are filed by the parent session before launch, never by the script. Pin
     each card's model before its first dispatch. The verifier for a card comes from a different
     model family than the worker that actually ran (read it from the run's metadata, not the
     pin), and it must FAIL a known-bad control before its PASS counts.
   - **Every lane, native or Kanban,** first writes its detailed test-first task list after reading
     the code it changes. Then it implements test-first, runs the plan's gates, pushes, opens a
     draft PR and reports its branch, PR, gate counts and anything it could not do. A lane never
     merges, deploys, applies hosted migrations or changes hosted config.
   - **Standing lines in every brief that can run commands:**
     - stop processes only by the PID you started; never `pkill` or `killall` by pattern (a lane's
       `pkill -f 'http.server'` once killed a system service's web server);
     - edit only the non-repo config files the brief names, back each one up first, record the
       rollback (the template takes them as `lane.live_config`; none named means none edited);
     - if CI on push is paid (macOS runners, release builds), the tip commit of every push carries
       `[skip ci]`. A marker on an earlier commit does not count, because Actions reads only the
       pushed head. Check `gh run list --commit <tip>` is empty after each push (the template
       adds this line when `lane.skip_ci` or `args.skip_ci` is true).

4. **Wave 2: integration, native.**
   - Merge in the plan's order: producers before their consumers, one platform at a time. Rebase
     each lane on the last merge and get its gates green before merging.
   - One fresh whole-branch reviewer (opus) checks the combined diff against the plan and spec,
     Review Focus first. Fix what it finds.
   - Then ship: deploy, hosted migrations, switches on, a smoke run.
   - Then write the record: what landed, what is pending, what needs a human.

   `examples/mixed-wave.js` merges only the landable native lanes. Verifying and merging the
   Kanban half stays with the parent session.

## Watching the Kanban half

Launch the `kanban-watch` workflow right after you dispatch the cards:

```text
Workflow({name: "kanban-watch",
          args: {board: "default", phases: [{title: "Build", tasks: ["t_1a2b3c4d"]},
                                            {title: "Verify", tasks: ["t_9c0d1e2f"]}]}})
```

Start a new session after `./install.sh` so the `kanban-watcher` agent type is loaded. If you pass
`scriptPath` instead of `name`, give the absolute path to `kanban-watch.js`, not a `~` path.

`examples/sample-args.json` is a fuller example. Give `phases` in the order the wave runs them:
workers, then verifier, then lander. Each card you name runs as one `kanban-watcher` row in
`/workflows`. Open a row to see the card's worker as it works: the watcher follows its tool calls
and latest message, at most 1.5 KB a call (see `agents/kanban-watcher.md`). Pass `verbose: true`
for every message and tool result. The `whenToUse` line in the `meta` block of
`workflows/kanban-watch.js` lists every arg it takes. Waiting, board-read, events and flag rows run
on `haiku`; the live row runs on `sonnet`.

The optional hook `hooks/kanban-watch-nudge.py` reminds Claude to launch the watcher after
`hermes kanban create` or `swarm`. It adds context only and never blocks.

Inside your own script, use one `agentType: 'kanban-watcher'` stage (pinned to `sonnet`) that
waits on the exact card ids, as `examples/mixed-wave.js` does:

```bash
timeout 290 python3 "${KANBAN_LANES_HOME:-$HOME/.claude/kanban-lanes}/scripts/wave_status.py" \
  --board default --task <id> --task <id> --wait 270 --interval 60
```

Keep an agent's `--wait` at 270 seconds so the next turn can read its prompt cache. Run the
command again on exit 3. From the parent session with Bash `run_in_background`, a longer
`--wait` is fine: a background shell process holds no model prompt cache.

- It reads only the cards you name, in one read transaction, and never dispatches, archives,
  resets or comments.
- Exit 0 proves a successful read only. Check `all_done` separately: "quiescent" includes review,
  blocked and archived.
- `--wait` re-reads the same cards until quiescent (exit 0) or the time runs out (exit 3, last
  snapshot printed). Run it as a background command you get notified on; do not write your own
  polling loop.
- A locked DB is UNKNOWN. Retry later; never clear its WAL.
- A fresh heartbeat is not liveness: a wedged worker keeps heartbeating. Before you call a running
  card healthy, check that it still writes messages:
  `kanban_live.py follow --board <b> --task <id> --cursor-key check --compact --max-seconds 0`
  prints what the worker wrote since your last check. Do not use `hermes kanban log` or `show` for
  this: `hermes kanban` commands open the board read-write and can change its schema.
- A runtime-cap kill can land seconds after a commit. Check the card's worktree
  (`git rev-list --count HEAD ^<base>`) before you treat the kill as a failure.

## Plan header template

Put this at the top of the plan, after the goal, architecture and spec link. Lanes read it before
they write their own task list.

```markdown
**Execution.**
- Wave 0 and the native lanes run as a native Workflow on opus, each lane in its own git worktree.
- The Kanban lanes run in parallel as Hermes Kanban cards. Each card's verifier comes from a
  different model family than the model that actually ran.
- Every lane's first deliverable is its detailed task list, written to
  `docs/plans/<plan>/<lane>.md` (files, interfaces, failing test, run, implement, pass, commit,
  with real code), after reading the code it changes.
- The interfaces below are fixed. A lane that needs a different one stops and reports.

## Global Constraints
- repo rules that apply in full
- data and privacy rules (what is never stored, logged or sent)
- words rules (where copy lives, house style)
- migrations reserved for Wave 0: "No lane adds a migration."
- switches and their defaults (including when the settings store is unreachable)
- CI and build rules (for example `[skip ci]` on app paths)
- gates before a lane is ready, as exact commands
- gates name test FILES, never name patterns: jest treats each word as a path pattern, so a
  stray space that leaves a one-letter word like `s` matches almost every test file and starts
  the full suite
- "Lanes don't deploy, merge, apply hosted migrations or change hosted config."

## Fixed interfaces
Every field a lane CONSUMES names its source. Grep that source in the real response or query
shape before you file the lane: a card once needed a field the API never selected, and the round
trip cost a re-dispatch.
### SQL (Wave 0)   | Object | Signature |   tables, functions, triggers, storage policies
### Web (Wave 0)   | File | Interface |     types, loaders, placeholder props, route stubs, API shapes
### App (Wave 0)   | File | Interface |     models, protocol methods, placeholder views

## Words
- `key`: "the exact line"

## Review Focus
1. **<edge case in one line>.** Expected behaviour. Owner: <lane ids> (<which test>).
```

## Lane table template

One block per lane in the plan's Wave 1 section:

```markdown
**N1: <name> (<platform>).**                     native lanes N*, Kanban lanes K*
- Owns <explicit file list or globs>.
- Interface it produces: <signatures other lanes consume>.
- Consumes <Wave 0 interfaces>.
- Tests: <named cases, including its Review Focus items>.
```

State the Wave 2 merge order in the plan too.

## Estimate worksheet

Give a range per option, not one number, and name what dominates it.

| Term | Where it comes from |
|-|-|
| W0 | wave 0 contract lanes in parallel, plus review and fix, plus landing |
| N | the longest native lane: its task count times the per-task pace on opus |
| K | the longest Kanban lane: dispatch wait + worker run + verifier (with its known-bad control) + one retry if that kind of lane has a history of retries |
| W2 | merges in order × (rebase + gates) + whole-branch review + fixes + ship + smoke |

- **Mixed:** `W0 + max(N, K) + W2`.
- **All native:** `W0 + max(all lanes on opus) + W2`. Your harvest and review load grows with
  every native lane, and Workflow concurrency caps how many run at once.
- **All Kanban:** contracts and integration stay native anyway, so `W0 + max(all lanes as cards)
  + W2`. Cross-cutting lanes on cards cost retries and verifier rounds; count them.

**Cap lanes per provider.** Some providers have short usage windows. In one run, on one machine,
seven lanes on one provider used up a 5-hour window in about half an hour. Cap each provider at
about 3 code lanes per wave, and route the rest to another model from the start, not as a mid-run
fallback. Check the provider's remaining usage before you size the wave.

Say which lane is the critical path, which option you recommend, and why.

## Worked example

This example is invented to show the shape. A feature across a Postgres backend, a web app and an
Android app: team reading lists. You save articles, tag them and share a list with your team.

- **Wave 0** was one native Workflow with three contract lanes (SQL, web, Android), each in its own
  worktree created by its brief, because the main checkout was shared with live sessions. It used
  `pipeline(LANES, build, review, fix)`. The review schema was `{verdict: pass | fix_needed,
  findings[{severity, file, issue, fix}], gates}`, and the fix stage ran only when a blocker or
  major finding existed.
- **Native lanes** and why each went native:
  - N1, list sync engine: shared code that three Kanban lanes consume
  - N2, sharing and permissions: access rules that need close review
  - N3, article import: many files and a retry state machine
  - N4, Android offline queue: a state machine that the Android screens consume
- **Kanban lanes**, one page, screen or doc each: K1 the web list page, K2 the web tag editor, K3
  the public share page, K4 the Android list screen, K5 the Android share sheet, K6 the help page
  and release notes.
- **Wave 2 order:** N1, N2, N3, then the web pages K1, K2, K3, then K6, then the Android lanes N4,
  K4, K5. N1 goes first because K1 and K4 read lists through it. N2 lands before K3, which needs its
  share rules. N4 lands before the Android screens that queue writes through it. Then one opus
  whole-branch review, the deploy with switches on and a smoke run, then the build log.
