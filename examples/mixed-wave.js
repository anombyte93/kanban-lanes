export const meta = {
  name: 'mixed-wave',
  description: 'A mixed wave: land the interfaces, run native lanes and a sonnet watcher over Kanban cards side by side, then integrate the native lanes',
  phases: [
    { title: 'Contracts', detail: 'wave 0: land the fixed interfaces once, or skip when args.base is given' },
    { title: 'Build', detail: 'wave 1: native lanes, one worktree each' },
    { title: 'Review', detail: 'fresh adversarial reviewer per lane' },
    { title: 'Fix', detail: 'serious findings, then a fresh re-review of the new head (at most 2 rounds)' },
    { title: 'Watch', detail: 'wave 1: the Kanban half, read-only' },
    { title: 'Integrate', detail: 'wave 2: merge landable native lanes in order, then one whole-branch review' },
  ],
}
// Template for a mixed wave (docs/mixed-waves.md). Copy it, fill in the args, lint it with
// skills/dynamic-workflow/scripts/lint-workflow-script.py, then launch it with {scriptPath}.
//
// The parent session files the Kanban cards BEFORE launching and passes their ids in args.cards.
// This script never creates, dispatches, accepts or comments on a card, and never merges a card's
// branch: verifying and merging the Kanban half is the parent's job.
//
// args: {
//   repo:        main checkout path (may be shared with other live sessions; lanes never touch it)
//   scratch:     directory for lane worktrees
//   integration: the branch every lane PR targets and wave 2 merges into (never the deploy branch)
//   plan, spec:  paths, relative to the repo, of the plan (Fixed interfaces, lanes) and the spec
//   base?:       sha of an already-landed wave 0. Omit it and the Contracts stage lands wave 0.
//   contracts?:  wave 0 brief: what to land (types, stubs, migrations, copy) when base is omitted
//   board?:      Kanban board slug (default 'default')
//   cards:       Kanban card ids the parent filed, e.g. ['t_1a2b3c4d']; [] for none
//   lanes:       [{ key, branch, owns, brief, gates, live_config?, skip_ci? }]
//                skip_ci: true when CI on push is paid (macOS runners, release builds); the lane's
//                pushes then carry [skip ci] on the tip commit. args.skip_ci sets the default for
//                every lane and for waves 0 and 2.
//   skip_ci?:    default for lane.skip_ci (false)
//   merge_order?: lane keys in wave 2 merge order (producers before consumers); default lanes order
// }
// repo, scratch, integration, plan and spec are required. lanes and cards may not both be empty.
// Returns { base, native: [{ lane, branch, built, review, fix, rereview, rounds, landable, blocked }],
//           kanban, integration }.
// landable is true only when the LAST review passed, so "reviewed" always means the head that
// would land. A fix_needed verdict with no findings stops the lane: a fixer would get '[]' and a
// re-review of an unchanged head could pass what nobody examined.
const A = args || {}
for (const key of ['repo', 'scratch', 'integration', 'plan', 'spec']) {
  if (!A[key]) throw new Error(`args.${key} is required`)
}
if (!(A.lanes && A.lanes.length) && !(A.cards && A.cards.length)) {
  throw new Error('args.lanes is empty, so args.cards must name at least one card')
}
const repo = A.repo
const scratch = A.scratch
const integration = A.integration
const plan = A.plan
const spec = A.spec
const board = A.board || 'default'
const cards = A.cards || []
const lanes = A.lanes || []
const MAX_FIX_ROUNDS = 2
// Single quotes: the shell expands this, not JavaScript.
const SCRIPTS = '${KANBAN_LANES_HOME:-$HOME/.claude/kanban-lanes}/scripts'

// Dependency reuse (see the dynamic-workflow skill, rule 7). A fresh worktree has no node_modules,
// so every gate would fail, and a lane that installs on its own costs 1 GB or more per copy.
const DEPS = `DEPENDENCIES: never run a fresh package install. If ${repo}/node_modules exists and every lockfile in your worktree is byte-identical to the one in ${repo} (check with cmp), make a hard-link copy: \`cp -al ${repo}/node_modules <your worktree>/node_modules\`. If a lockfile differs, or you must add a dependency, do a real install and say so in your result. On return, remove the copy with \`rm -rf -- <your worktree>/node_modules\` (it drops links only).`

// Optional standing line for repos where CI on push is paid.
const SKIP_CI = '- CI: CI on push is paid here. The tip commit of every push carries [skip ci] in its message. A marker on an earlier commit does not count, because CI reads only the pushed head. After each push, check that `gh run list --commit <tip sha>` lists no runs.'

// Standing lines in every brief that can run commands.
const standing = (lane) => `STANDING RULES:
- PROCESSES: stop processes only by the PID you started; never pkill or killall by pattern. A pattern also matches other sessions' processes and system services.
- LIVE CONFIG: edit only the non-repo config files this brief names, back each one up first, and record the rollback. Files named for this lane: ${lane.live_config || 'none, so edit no file outside your worktree'}.
- TESTS: run tests with HOME and the XDG dirs pointed at a temp dir under your worktree: \`T=<worktree>/.tmp-home; mkdir -p $T; HOME=$T XDG_CACHE_HOME=$T/.cache XDG_CONFIG_HOME=$T/.config XDG_DATA_HOME=$T/.local/share XDG_STATE_HOME=$T/.local/state <test command>\`.
- ${DEPS}${(lane.skip_ci ?? A.skip_ci) ? '\n' + SKIP_CI : ''}`

const setup = (lane, base) => `SETUP (mandatory): the main checkout ${repo} may be shared with other live sessions. Never switch, stash, reset or commit there. Create your worktree: \`git -C ${repo} fetch -q origin && git -C ${repo} worktree add -b ${lane.branch} ${scratch}/${lane.branch} ${base}\`, and work only inside it.
READ FIRST in your worktree: ${plan} (Global Constraints, Fixed interfaces, Words, Review Focus, your lane) and ${spec}. The Fixed interfaces are binding. If one is impossible, stop and report instead of inventing one.
YOU OWN ONLY: ${lane.owns}. Touching another lane's file means stopping and reporting. Never add a migration, merge, deploy or change hosted config.
METHOD: write your detailed test-first task list to the plan's lane folder and commit it. Then implement test-first in small commits, run every gate (${lane.gates}), push the branch and open a DRAFT PR whose base is ${integration}.
${standing(lane)}
RETURN: branch, head sha, draft PR URL, gate commands with pass/fail counts, deviations from the Fixed interfaces, anything you could not do.`

const BASE_SCHEMA = {
  type: 'object',
  properties: {
    base: { type: 'string', description: 'the full sha of the landed wave 0 commit on the integration branch' },
    landed: { type: 'array', items: { type: 'string' } },
    gates: { type: 'string' },
  },
  required: ['base', 'landed', 'gates'],
}

const REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['pass', 'fix_needed'] },
    findings: { type: 'array', items: { type: 'object', properties: {
      severity: { type: 'string', enum: ['blocker', 'major', 'minor'] },
      file: { type: 'string' }, issue: { type: 'string' }, fix: { type: 'string' } }, required: ['severity', 'issue', 'fix'] } },
    gates: { type: 'string' },
  },
  required: ['verdict', 'findings', 'gates'],
}

// A review passes only when its verdict is pass AND it carries no blocker or major.
const serious = (r) => ((r && r.findings) || []).filter((f) => f.severity !== 'minor')
const passed = (r) => !!r && r.verdict === 'pass' && !serious(r).length

// Wave 0: land the interfaces every lane builds against, as one commit on the integration branch.
let base = A.base
if (!base) {
  const landed = await agent(`You land wave 0 of a mixed wave in ${repo}. Work in a new worktree: \`git -C ${repo} fetch -q origin && git -C ${repo} worktree add ${scratch}/wave0 origin/${integration}\`. Never touch the main checkout.
READ FIRST: ${plan} (Fixed interfaces) and ${spec}.
LAND exactly the Fixed interfaces: ${A.contracts || 'every type, stub, migration and placeholder the plan lists under Fixed interfaces'}. Exact names and shapes from the plan; no feature logic.
Run the plan's gates, commit, and push to ${integration}. Never push to the deploy branch.
${standing({ live_config: null, skip_ci: A.skip_ci })}
Return the full sha of the pushed commit as base, the files you landed and the gate counts.`, { label: 'build:contracts', phase: 'Contracts', model: 'opus', effort: 'high', schema: BASE_SCHEMA })
  if (!landed || !landed.base) throw new Error('wave 0 returned no base sha; read its transcript before re-running')
  base = landed.base
}

// One reviewer for the first look and every re-review: fresh agent, same schema, own label.
const review = (lane, reports, round) => agent(`You are a fresh, adversarial reviewer of lane ${lane.key}${round ? ', re-review ' + round + ' of the head after a fix' : ''}. Reports so far:\n\n${reports}\n\nWorktree ${scratch}/${lane.branch} is read-only for you. Review the PUSHED head: \`git -C ${scratch}/${lane.branch} rev-parse HEAD\` must equal the sha that \`git -C ${scratch}/${lane.branch} ls-remote origin refs/heads/${lane.branch}\` prints; an unpushed or diverged head is a major. Diff: \`git -C ${scratch}/${lane.branch} diff ${base}...HEAD\`. Check every Fixed interface the lane produces against ${plan}, re-run its gates yourself (${lane.gates}), check the Review Focus items it owns and the file-ownership boundary. Report a blocker or major only with a file and a concrete failure. You are not re-adjudicating the plan.\n${DEPS}`, { label: round ? `rereview:${lane.key}:${round}` : `verify:${lane.key}`, phase: 'Review', model: 'opus', effort: 'xhigh', schema: REVIEW_SCHEMA })

// Wave 1, native half: build, review, then fix and re-review at most MAX_FIX_ROUNDS times.
const native = pipeline(
  lanes,
  (lane) => agent(`${setup(lane, base)}\n\nLANE ${lane.key}:\n${lane.brief}`, { label: `build:${lane.key}`, phase: 'Build', model: 'opus', effort: 'high' }),
  (built, lane) => review(lane, `Builder's report:\n${built}`, 0).then((first) => ({ built, review: first })),
  async (prev, lane) => {
    const rounds = []
    let last = prev.review
    let blocked = null
    let reports = `Builder's report:\n${prev.built}`
    for (let round = 1; round <= MAX_FIX_ROUNDS && last && !passed(last); round++) {
      const findings = serious(last).length ? serious(last) : last.findings || []
      if (!findings.length) {
        blocked = `review returned ${last.verdict} with no findings; nothing for a fixer, read the reviewer's gates and report by hand`
        break
      }
      const fix = await agent(`You built lane ${lane.key} in ${scratch}/${lane.branch}. A reviewer found:\n\n${JSON.stringify(findings, null, 2)}\n\nFor each: confirm it in the code. If it is real, fix it test-first, commit and push. If it is not, say why with evidence. Re-run the gates (${lane.gates}). Work only in that worktree and only on the files you own (${lane.owns}).\n${standing(lane)}\nReturn per finding fixed or rejected, the new pushed head sha and the gate counts.`, { label: `fix:${lane.key}:${round}`, phase: 'Fix', model: 'opus', effort: 'high' })
      reports += `\n\nFix round ${round}, findings:\n${JSON.stringify(findings, null, 2)}\nFixer's report:\n${fix}`
      last = await review(lane, reports, round)
      rounds.push({ fix, rereview: last })
    }
    const tail = rounds[rounds.length - 1]
    const landable = passed(last) && !blocked
    if (!landable && !blocked) {
      blocked = last ? `last review still ${last.verdict} after ${rounds.length} fix round(s)` : 'no review verdict returned'
    }
    return {
      lane: lane.key,
      branch: lane.branch,
      ...prev,
      fix: tail ? tail.fix : null,
      rereview: tail ? tail.rereview : null,
      rounds,
      landable,
      blocked: landable ? null : blocked,
    }
  },
)

// Wave 1, Kanban half: the kanban-watcher agent type (defined in agents/kanban-watcher.md, pinned
// to sonnet) waits on the exact card ids the parent filed. Its definition holds the read-only
// rules and the report format.
const taskFlags = cards.map((c) => `--task ${c}`).join(' ')
const watch = cards.length
  ? agent(`Standalone mode. Lane: Kanban, wave 1 of a mixed wave. Board: ${board}. Cards: ${cards.join(', ')}.
Wait with \`timeout 290 python3 ${SCRIPTS}/wave_status.py --board ${board} ${taskFlags} --wait 270 --interval 60\` (Bash timeout 600000 ms). On exit 3, run the same command again. When it exits 0, run \`timeout 30 python3 ${SCRIPTS}/kanban_live.py snapshot --board ${board} ${taskFlags}\`.
Return your report for each card, then the last wave_status.py JSON and the snapshot lines verbatim.`, { label: 'watch:kanban', phase: 'Watch', agentType: 'kanban-watcher' })
  : Promise.resolve(null)

const [lanesOut, kanban] = await Promise.all([native, watch])

// Wave 2: merge the landable native lanes in order, then one fresh whole-branch review.
const order = A.merge_order || lanes.map((l) => l.key)
const ready = order.map((k) => lanesOut.find((l) => l && l.lane === k)).filter((l) => l && l.landable)
let integrated = null
if (ready.length) {
  const merged = await agent(`You integrate wave 2 of a mixed wave in ${repo}. Work in a new worktree: \`git -C ${repo} fetch -q origin && git -C ${repo} worktree add ${scratch}/wave2 origin/${integration}\`. Never touch the main checkout.
Merge these lane branches into ${integration}, in this order, one at a time: ${ready.map((l) => l.branch).join(', ')}. For each: rebase it on the last merge, run its gates green, merge, push ${integration}. If a rebase conflicts or a gate fails, stop at that lane and report; do not merge the rest.
Never merge to the deploy branch, deploy, or merge a Kanban card's branch: those belong to the parent.
${standing({ live_config: null, skip_ci: A.skip_ci })}
Return each lane's merge sha and gate counts, the final ${integration} head sha, and where you stopped if you stopped.`, { label: 'build:integrate', phase: 'Integrate', model: 'opus', effort: 'high' })
  const whole = await agent(`You are a fresh, adversarial whole-branch reviewer. The integrator reported:\n\n${merged}\n\nReview \`git -C ${scratch}/wave2 diff ${base}...origin/${integration}\` against ${plan} and ${spec}, the Review Focus items first. Re-run the plan's gates yourself in ${scratch}/wave2. Report a blocker or major only with a file and a concrete failure.\n${DEPS}`, { label: 'verify:whole-branch', phase: 'Integrate', model: 'opus', effort: 'xhigh', schema: REVIEW_SCHEMA })
  integrated = { merged, review: whole }
}

return { base, native: lanesOut, kanban, integration: integrated }
