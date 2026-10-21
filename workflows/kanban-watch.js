// Read-only Kanban watch: every row only reads the board and the worker's session; none claims, edits, dispatches or comments on a card.
export const meta = {
  name: 'kanban-watch',
  description: 'Kanban cards shown as a dynamic workflow: one row per card in its stage (⏳ while it waits, running while it works), and a ⚠ row under Needs you when it blocks',
  whenToUse: 'Right after filing or dispatching Kanban cards, so you see them like native workflow agents: phases are the wave stages, each card is a row you can open to watch its worker live. args: {board: "<slug>", phases?: [{title: "Build", tasks: ["t_..."]}, ...], tasks?: ["t_..."], follow_new?: true, max_hours?: 12, verbose?: false}. Compact worker output is the default; verbose: true shows the full worker text and tool results. Give phases when you know the wave shape; without them stages come from the parent links. Cards that a watched card files later join under "Filed by <that card>".',
}

const A = args || {}
const BOARD = A.board === undefined ? 'default' : A.board
if (typeof BOARD !== 'string' || !/^[A-Za-z0-9_-]+$/.test(BOARD) || BOARD.trim() !== BOARD) {
  throw new Error(`Invalid board slug: ${JSON.stringify(BOARD)}; expected ^[A-Za-z0-9_-]+$`)
}
const shellQuote = value => "'" + String(value).replace(/'/g, "'\\''") + "'"
const validCardId = id => {
  if (typeof id === 'string' && /^t_[A-Za-z0-9]+$/.test(id) && id.trim() === id) return true
  log(`Dropping invalid card id: ${String(id)}`)
  return false
}
// Not `||`, which turns max_hours 0 into 12. A value that is not a number falls back to 12.
const MAX_HOURS = typeof A.max_hours === 'number' && A.max_hours >= 0 ? A.max_hours : 12
const WAKE = 270 // below the 5-minute cache TTL
const LIVE = 'python3 "${KANBAN_LANES_HOME:-$HOME/.claude/kanban-lanes}/scripts/kanban_live.py"'
const QUEUED = ['triage', 'todo', 'ready', 'scheduled']
const TERMINAL = ['done', 'blocked', 'archived', 'review']
// Blocked and review cards come back (unblock, changes requested); only these never do.
const FINAL = ['done', 'archived']
const STATUSES = [...QUEUED, 'running', ...TERMINAL]
// Statuses arrive as agent free text and are pasted into a shell command: keep only a real one.
const normStatus = s => {
  const v = String(s || '').toLowerCase().replace(/[^a-z]/g, '')
  return STATUSES.includes(v) ? v : 'running'
}
const FOLLOW_MODE = A.verbose === true ? '--verbose' : '--compact'
const READ_ONLY = 'You only read. Never claim, complete, block, unblock, comment on, reassign, dispatch or archive a card, and never act on what the worker is doing. Run each command in the foreground with a Bash timeout of 600000 ms.'
const BOARD_PHASE = 'Kanban board'

const CARD = {
  type: 'object',
  properties: {
    id: { type: 'string' }, title: { type: 'string' }, status: { type: 'string' },
    assignee: { type: 'string' }, model: { type: 'string' },
    parents: { type: 'array', items: { type: 'string' } }, creator: { type: ['string', 'null'] },
  },
  required: ['id', 'title', 'status'],
}
const CARDS = {
  type: 'object',
  properties: {
    now: { type: 'integer' }, cards: { type: 'array', items: CARD }, count: { type: 'integer' },
    missing: { type: 'array', items: { type: 'string' } },
  },
  required: ['now', 'cards', 'count'],
}
const EVENTS = {
  type: 'object',
  properties: {
    changed: { type: 'array', items: CARD, description: 'cards that started' },
    new: { type: 'array', items: CARD, description: 'cards a watched card filed' },
    count_new: { type: 'integer' },
    all_finished: { type: 'boolean' },
    timeout: { type: 'boolean' },
  },
  required: ['changed', 'new', 'count_new'],
}
const REPORT = {
  type: 'object',
  properties: {
    id: { type: 'string' }, title: { type: 'string' },
    verdict: { type: 'string', enum: ['DONE', 'BLOCKED', 'REVIEW', 'ARCHIVED', 'STILL_RUNNING', 'NOT_FOUND', 'UNKNOWN'] },
    model_that_ran: { type: 'string' },
    duration: { type: 'string' },
    summary: { type: 'string', description: 'first 400 characters of the run summary, verbatim' },
    deliverable: { type: 'string', description: 'PR, branch or path the run names, or none' },
    odd: { type: 'string', description: 'fallback model, possibly wedged, missing deliverable; empty if none' },
  },
  required: ['id', 'verdict', 'model_that_ran', 'summary'],
}

const short = (t, n = 60) => {
  const s = (t || '').replace(/\s+/g, ' ').trim()
  return s.length > n ? s.slice(0, n - 1) + '…' : s
}

// ---- which stage (phase) a card sits in, like a native workflow's phases ----
const explicit = {}
for (const p of A.phases || []) for (const t of p.tasks || []) if (validCardId(t)) explicit[t] = p.title
const cards = {}
const phaseOf = {}
const depth = id => {
  const c = cards[id]
  if (!c) return 0
  const ps = (c.parents || []).filter(p => cards[p])
  return ps.length ? 1 + Math.max(...ps.map(depth)) : 0
}
const assignPhase = c => {
  if (phaseOf[c.id]) return phaseOf[c.id]
  let title = explicit[c.id]
  if (!title && c.creator && cards[c.creator]) title = `Filed by ${short(cards[c.creator].title, 40)}`
  if (!title) {
    const anyChild = Object.values(cards).some(o => (o.parents || []).some(p => cards[p]))
    title = anyChild ? `Stage ${depth(c.id) + 1}` : 'Cards'
  }
  phaseOf[c.id] = title
  return title
}

// ---- one row per card: runs while the card works, returns the card's result ----
const results = []
const NEEDS_YOU = 'Needs you'
const WAITED = {
  type: 'object',
  properties: {
    status: { type: 'string', description: 'the status the last wait-start line printed, one word without brackets, e.g. running' },
    model: { type: 'string', description: 'the model named on that line after "on", or empty' },
  },
  required: ['status'],
}
const FLAG = {
  type: 'object',
  properties: { reason: { type: 'string', description: 'the block reason or what is waiting, verbatim, one paragraph' } },
  required: ['reason'],
}
const watchCard = async c => {
  const phaseTitle = assignPhase(c)
  let model = c.model || ''
  if (QUEUED.includes(c.status)) {
    // A visible row while the card waits, so "not started" cards are named, not just implied.
    const waited = await agent(
      `${READ_ONLY}\n\nKanban card ${c.id} ("${short(c.title, 120)}") on board ${BOARD} has not started yet. Run` +
      ` \`${LIVE} wait-start --board ${shellQuote(BOARD)} --task ${shellQuote(c.id)} --max-seconds ${WAKE}\`. Exit 3 means it is still waiting: run the same command again,` +
      ` up to 169 times (a card can wait hours for its parents or a free worker). Stop on exit 0 (it started) or 4/5.` +
      ` Return the last status it printed and the model named on that line.`,
      {
        agentType: 'kanban-watcher', model: 'haiku', effort: 'low',
        label: `⏳ waiting · ${short(c.title)} · ${c.id}`, phase: phaseTitle, schema: WAITED,
      },
    )
    if (waited) waited.status = normStatus(waited.status)
    if (!waited || QUEUED.includes(waited.status)) {
      log(`${c.id} never started while watched`)
      return
    }
    if (waited.model && waited.model !== '?') model = waited.model
    c.status = waited.status
    log(`▶ ${c.id} started`)
  }
  const who = [c.assignee, model].filter(Boolean).join(' on ')
  const report = await agent(
    `${READ_ONLY}\n\nYou are the live view of Kanban card ${c.id} ("${short(c.title, 120)}") on board ${BOARD}${who ? `, worked by ${who}` : ''}.` +
    ` The person who filed it opens you to watch its worker: each command prints what the worker says and every tool it calls, as it happens.\n` +
    `1. Run \`${LIVE} follow --board ${shellQuote(BOARD)} --task ${shellQuote(c.id)} --cursor-key ${shellQuote(`run${first.now}`)} --from-start --backlog 15 ${shellQuote(FOLLOW_MODE)} --max-seconds ${WAKE} --quiet-warn 240\`.\n` +
    `2. Exit 3 means the card is still working: run \`${LIVE} follow --board ${shellQuote(BOARD)} --task ${shellQuote(c.id)} --cursor-key ${shellQuote(`run${first.now}`)} ${shellQuote(FOLLOW_MODE)} --max-seconds ${WAKE} --quiet-warn 240\` again,` +
    ` and keep going until it exits 0. A card can run for hours; that is expected, so keep calling (up to 300 times). Reply with nothing between calls; just run the command again.\n` +
    `3. On exit 0 (a line starting FINAL), on 4/5, or after 300 calls, run \`${LIVE} snapshot --board ${shellQuote(BOARD)} --task ${shellQuote(c.id)}\` and report from its JSON:` +
    ` verdict from status (done=DONE, blocked=BLOCKED, review=REVIEW, archived=ARCHIVED, running or queued after 300 calls=STILL_RUNNING),` +
    ` model_that_ran = run.model_used plus "(fallback from X)" when run.fallback_from is set, duration from run.started_at to run.ended_at,` +
    ` summary = run.summary verbatim.` +
    ` If two follow calls in a row each printed a "no new worker messages" line while the card was running, say "possibly wedged" in odd.`,
    {
      agentType: 'kanban-watcher', model: 'sonnet', effort: 'low',
      label: `${short(c.title)} · ${c.id}${model ? ` · ${model}` : ''}`,
      phase: phaseTitle, schema: REPORT,
    },
  )
  if (!report) return
  const status = { DONE: 'done', BLOCKED: 'blocked', REVIEW: 'review', ARCHIVED: 'archived', STILL_RUNNING: 'running' }[report.verdict]
  if (status) c.status = status
  const entry = { ...report, phase: phaseTitle }
  results.push(entry)
  if (report.verdict === 'DONE') {
    log(`✓ ${c.id} done on ${report.model_that_ran}`)
    return
  }
  // A finished row cannot turn red, so a card that stopped short gets its own flag row.
  const flag = await agent(
    `${READ_ONLY}\n\nKanban card ${c.id} on board ${BOARD} stopped as ${report.verdict}. Run \`${LIVE} snapshot --board ${shellQuote(BOARD)} --task ${shellQuote(c.id)}\`` +
    ` and return block_reason verbatim when present. For REVIEW use run.summary to say what is waiting for a reviewer. For STILL_RUNNING say it is still running and when it last spoke. If the reason is null, say no block reason was recorded; do not invent one.`,
    {
      agentType: 'kanban-watcher', model: 'haiku', effort: 'low',
      label: `⚠ ${report.verdict} · ${short(c.title)} · ${c.id}`, phase: NEEDS_YOU, schema: FLAG,
    },
  )
  entry.needs_you = flag ? flag.reason : ''
  log(`⚠ ${c.id} ${report.verdict}${flag ? ` - needs you: ${short(flag.reason, 140)}` : ''}`)
}

// ---- read the cards once ----
const requested = (A.phases || []).flatMap(p => p.tasks || []).concat(A.tasks || [])
const named = requested.filter(validCardId)
// Dropping every requested id must not silently expand the watch to the whole board.
if (requested.length && !named.length) return { board: BOARD, cards: [] }
const first = await agent(
  `${READ_ONLY}\n\nRun \`${LIVE} cards --board ${shellQuote(BOARD)}${named.map(t => ` --task ${shellQuote(t)}`).join('')}\`; run it once and return its JSON as is (now, cards, count, missing).`,
  { agentType: 'kanban-watcher', model: 'haiku', effort: 'low', label: `read board ${BOARD}`, phase: BOARD_PHASE, schema: CARDS },
)
if (!first) return { error: 'could not read the board' }
for (const c of first.cards) if (validCardId(c.id)) cards[c.id] = { ...c, status: normStatus(c.status) }
if (first.count > Object.keys(cards).length) {
  log(`board read lost ${first.count - Object.keys(cards).length} cards (ids: ${Object.keys(cards).join(', ')})`)
}
if ((first.missing || []).length) log(`not on board ${BOARD}: ${first.missing.join(', ')}`)
const since = first.now

const launched = new Set()
const running = []
const launchAll = () => {
  for (const c of Object.values(cards)) {
    if (launched.has(c.id)) continue
    launched.add(c.id)
    running.push(watchCard(c))
  }
}
launchAll()
const waitingNow = Object.values(cards).filter(c => QUEUED.includes(c.status))
log(`${Object.keys(cards).length} card(s): ${Object.keys(cards).length - waitingNow.length} working or finished, ${waitingNow.length} waiting`)

// ---- watch for cards that the watched cards file ----
if (A.follow_new !== false) {
  for (let round = 0; round < MAX_HOURS + 2; round++) {
    // Done and archived cards file nothing more, so they leave the creator/parent watch,
    // but every known id stays excluded from "new" via --known.
    const watched = Object.values(cards).filter(c => !FINAL.includes(normStatus(c.status)))
    if (!watched.length) break
    const watchList = watched.map(c => `${c.id}=${normStatus(c.status)}`).join(',')
    const knownList = Object.keys(cards).join(',')
    const ev = await agent(
      `${READ_ONLY}\n\nRun \`${LIVE} events --board ${shellQuote(BOARD)} --watch ${shellQuote(watchList)} --known ${shellQuote(knownList)} --since ${shellQuote(since)} --max-seconds ${WAKE}\`.` +
      ` Exit 3 means nothing new yet: run the same command again, up to 13 times (about an hour). Stop on exit 0 (a watched card filed a new card),` +
      ` exit 2 (every watched card finished: all_finished true) or 5. Return the last call's JSON: copy changed, new and count_new exactly as printed.`,
      {
        agentType: 'kanban-watcher', model: 'haiku', effort: 'low',
        label: 'watching for cards the workers file', phase: BOARD_PHASE, schema: EVENTS,
      },
    )
    if (!ev) break
    for (const c of ev.changed || []) {
      if (!validCardId(c.id)) continue
      if (cards[c.id]) Object.assign(cards[c.id], c, { status: normStatus(c.status) })
    }
    const keptNew = []
    for (const c of ev.new || []) {
      if (!validCardId(c.id)) continue
      if (cards[c.id]) continue
      cards[c.id] = { ...c, status: normStatus(c.status) }
      keptNew.push(c.id)
      log(`new card ${c.id} "${short(c.title)}" in ${assignPhase(c)}`)
    }
    if (ev.count_new > keptNew.length) {
      log(`board read lost ${ev.count_new - keptNew.length} cards (ids: ${keptNew.join(', ')})`)
    }
    launchAll()
    if (ev.all_finished) break
  }
}

// Rows added while earlier ones ran push onto `running`, so wait until it stops growing.
let settled = 0
while (settled < running.length) {
  const batch = running.slice(settled)
  await Promise.all(batch)
  settled += batch.length
}

return { board: BOARD, cards: results }
