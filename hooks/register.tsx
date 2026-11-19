import { atom, read, update } from 'claude-code'
import type { EngineInterface as Engine, Register } from 'claude-code'

import type { Clawd, Fixing, PaneFact, PaneView, Searched, Used } from '../types'

// graphcraft-memory, the mod side. Python (graph/) owns the graph; this module only decides when things run:
// - prompt.submit: recall the facts linked to the prompt and attach them as context (never blocks a prompt); recall
//   runs in one Python process kept warm for the session (graph/serve.py), else in a fresh recall.py
// - turn.complete: keep a draft of the conversation; every `captureEvery` messages of yours, read the new part now,
//   so what is settled mid-session is recalled in this session too; session.end marks the draft finished;
//   session.compact queues it
// - session.start: read other mods' feeder files (graph/feeders.py), then extract queued conversations in the
//   background with a small model, and tell the user
// - /graph-memory, /graph-memory-undo, /graph-view: see what was learned, take a session back out, open the map
// - /graph-rename (and Rename in the pane): give a session your own name; a later capture never replaces it
// - /graph-export, /graph-import: carry memory to another PC as one file; import merges (graph/sync.py)
// - the Memory pane (/graph-pane): search memory, facts used for the prompt, facts learned last session, open
//   problems; Forget or Fix any fact (Fix: pick the right link, or tell Claude what is true);
//   and a brief band above the prompt until the first message

const CANDIDATES: readonly (readonly string[])[] = [['py', '-3'], ['python3'], ['python']]
const RECALL_TIMEOUT_MS = 5000
const CAPTURE_TIMEOUT_MS = 30000
const MAX_PROMPT_CHARS = 8000
const EXTRACT_MAX_TOKENS = 4096

type Json = Record<string, unknown>
type Turn = { role: string; text: string }

const PANE = 'memory'
// The terminal seats the pane inline in about a third of the screen: keep it short enough that the buttons show.
const PANE_ROWS = 22
const SHOW_USED = 6
const SHOW_LEARNED = 5
// Change a tag when that value's fields change: it declines what an earlier version stored, which outlives a reload.
const used = atom({ plugin: 'graphcraft-memory', key: 'used' } as const, null, { shape: 'used-1' })
const view = atom({ plugin: 'graphcraft-memory', key: 'view' } as const, null, { shape: 'view-1' })
const clawd = atom({ plugin: 'graphcraft-memory', key: 'clawd' } as const, null, { shape: 'clawd-2' })
const briefShown = atom({ plugin: 'graphcraft-memory', key: 'briefShown' } as const, true, { shape: 'brief-1' })
// True while the pane shows the field that renames the last session.
const renaming = atom({ plugin: 'graphcraft-memory', key: 'renaming' } as const, false, { shape: 'renaming-1' })
// The pane's search results, and the fact being corrected.
const searched = atom({ plugin: 'graphcraft-memory', key: 'searched' } as const, null as Searched | null, { shape: 'searched-1' })
const fixing = atom({ plugin: 'graphcraft-memory', key: 'fixing' } as const, null as Fixing | null, { shape: 'fixing-1' })
const SHOW_SEARCH = 6

// The warm recall process (graph/serve.py): its port and this session's token once it has said its port.
let warm: { port: number; token: string } | undefined
let warmStarting = false
const WARM_TIMEOUT_MS = 2000

type Recalled = {
  facts: number
  summary: string
  context: string
  db: string
  prompt_chars: number
  triples?: PaneFact[]
  matched?: string[]
  more?: number
}

/** Starts graph/serve.py for the session's life (the spawn loop ends with the module); never throws. */
function startWarm($: Engine, py: readonly string[]): void {
  if (warm || warmStarting) return
  warmStarting = true
  const token = crypto.randomUUID()
  void (async () => {
    try {
      const child = $.process.spawn({ argv: [...py, '-I', `${slash($.plugin.root)}/graph/serve.py`], env: { GRAPHCRAFT_TOKEN: token } })
      let said = ''
      for await (const { stream, text } of child) {
        if (stream === 'stderr') $.ui.log(`graphcraft-memory: serve.py: ${text.slice(0, 300)}`, { to: 'debug' })
        else if (!warm) {
          said += text
          const port = /"port":\s*(\d+)/.exec(said)
          if (port) {
            warm = { port: Number(port[1]), token }
            warmStarting = false
          }
        }
      }
    } catch (err) {
      $.ui.log(`graphcraft-memory: warm recall did not start: ${String(err).slice(0, 200)}`, { to: 'debug' })
    } finally {
      warm = undefined
      warmStarting = false
    }
  })()
}

/** Recall through the warm process; undefined when it is not there or does not answer in time. */
async function recallWarm($: Engine, prompt: string, cwd: string): Promise<Recalled | undefined> {
  const w = warm
  if (!w) return undefined
  try {
    const late = new Promise<undefined>(done => setTimeout(() => done(undefined), WARM_TIMEOUT_MS))
    const r = await Promise.race([
      $.http.fetch(`http://127.0.0.1:${w.port}/recall`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Token': w.token },
        body: JSON.stringify({ prompt, cwd }),
      }),
      late,
    ])
    if (r?.ok) return JSON.parse(r.text) as Recalled
  } catch {
    // not answering: below
  }
  if (warm === w) warm = undefined // a fresh one starts with this prompt's fallback
  return undefined
}

// Found once per load; undefined = not looked yet, null = no Python 3 on this machine.
let python: readonly string[] | null | undefined
// One extraction pass at a time in this process (claims in Python keep two sessions apart).
let draining = false

const slash = (p: string) => p.replace(/\\/g, '/').replace(/\/+$/, '')
const parent = (p: string) => slash(p).replace(/\/[^/]*$/, '')
const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`

async function findPython($: Engine): Promise<readonly string[] | null> {
  if (python !== undefined) return python
  for (const argv of CANDIDATES) {
    try {
      const r = await $.process.run([...argv, '--version'], { timeoutMs: 5000 })
      if (r.exitCode === 0 && /Python 3\./.test(r.stdout + r.stderr)) return (python = argv)
    } catch {
      // not installed under this name
    }
  }
  $.ui.log('graphcraft-memory: no Python 3 found (tried py -3, python3, python); memory is off.', { to: 'debug' })
  return (python = null)
}

/** Runs graph/<script> and parses its one JSON line; undefined (and a debug line) on any failure. */
async function runPy($: Engine, script: string, args: readonly string[], timeoutMs: number): Promise<Json | undefined> {
  const py = await findPython($)
  if (!py) return undefined
  const r = await $.process.run([...py, '-I', `${slash($.plugin.root)}/graph/${script}`, ...args], { timeoutMs })
  if (r.exitCode !== 0) {
    $.ui.log(`graphcraft-memory: ${script} ${args[0] ?? ''} failed: ${r.stderr.slice(0, 300)}`, { to: 'debug' })
    return undefined
  }
  try {
    const out = JSON.parse(r.stdout) as Json
    if (typeof out.error === 'string') $.ui.log(`graphcraft-memory: ${script} ${args[0] ?? ''}: ${out.error}`, { to: 'debug' })
    return out
  } catch {
    $.ui.log(`graphcraft-memory: ${script} ${args[0] ?? ''} printed no JSON`, { to: 'debug' })
    return undefined
  }
}

/** Where the graph and its queue live; mirrors store.db_path() so both sides agree. */
async function dataDir($: Engine): Promise<string> {
  const db = await $.env.get('GRAPHCRAFT_DB')
  if (db) return parent(db)
  const config = await $.env.get('CLAUDE_CONFIG_DIR')
  if (config) return `${slash(config)}/graphcraft-memory`
  const home = (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME')) ?? '~'
  return `${slash(home)}/.claude/graphcraft-memory`
}

const safeId = (sessionId: string) => sessionId.replace(/[^\w-]/g, '_')

/**
 * Writes the conversation as a capture job. `pending/` queues it now (before a compaction); `drafts/` keeps the
 * session's running copy, rewritten after every reply, which capture.py queues once the session has ended.
 * Only role and text: tool calls and results are left out here.
 */
async function saveJob($: Engine, where: 'pending' | 'drafts', trigger: string, messages: readonly Turn[]): Promise<void> {
  const turns = messages
    .filter(m => (m.role === 'user' || m.role === 'assistant') && m.text.trim() !== '')
    .map(m => ({ role: m.role, text: m.text }))
  if (!turns.some(m => m.role === 'user')) return
  const sessionId = await $.session.id()
  const name = where === 'pending' ? `${safeId(sessionId)}-${Date.now()}.json` : `${safeId(sessionId)}.json`
  const file = `${await dataDir($)}/${where}/${name}`
  const job = { version: 1, sessionId, project: await $.session.cwd(), trigger, at: Date.now(), messages: turns }
  await $.fs.write(file, JSON.stringify(job))
  $.ui.log(`graphcraft-memory: saved ${plural(turns.length, 'message')} (${trigger}) to ${file}`, { to: 'debug' })
}

/** Extracts every queued conversation: claim, prepare, one model call, apply. Never throws. */
async function drain($: Engine, model: string, from = 'earlier conversation'): Promise<void> {
  if (draining) return
  draining = true
  let facts = 0
  let sessions = 0
  try {
    const queued = await runPy($, 'capture.py', ['pending'], CAPTURE_TIMEOUT_MS)
    const files = Array.isArray(queued?.files) ? (queued.files as string[]) : []
    for (const queuedFile of files) {
      const claimed = await runPy($, 'capture.py', ['claim', queuedFile], CAPTURE_TIMEOUT_MS)
      const file = typeof claimed?.claimed === 'string' ? claimed.claimed : undefined
      if (!file) continue

      const prep = await runPy($, 'capture.py', ['prepare', file], CAPTURE_TIMEOUT_MS)
      if (!prep || typeof prep.error === 'string') {
        await runPy($, 'capture.py', ['fail', file, String(prep?.error ?? 'prepare failed')], CAPTURE_TIMEOUT_MS)
        continue
      }
      if (typeof prep.skip === 'string') {
        await runPy($, 'capture.py', ['discard', file, prep.skip], CAPTURE_TIMEOUT_MS)
        continue
      }

      const reply = await $.model.complete({
        model,
        system: String(prep.system),
        prompt: String(prep.prompt),
        maxTokens: EXTRACT_MAX_TOKENS,
      })
      if (!reply.isAnswered) {
        await runPy($, 'capture.py', ['fail', file, `model: ${reply.reason}`], CAPTURE_TIMEOUT_MS)
        continue
      }
      const replyFile = file.replace(/\.json$/, '.reply.txt')
      await $.fs.write(replyFile, reply.text)
      const applied = await runPy($, 'capture.py', ['apply', file, replyFile], CAPTURE_TIMEOUT_MS)
      if (!applied || typeof applied.error === 'string') {
        await runPy($, 'capture.py', ['fail', file, String(applied?.error ?? 'apply failed')], CAPTURE_TIMEOUT_MS)
        continue
      }
      facts += Number(applied.facts ?? 0)
      sessions += 1
    }
  } catch (err) {
    $.ui.log(`graphcraft-memory: capture stopped: ${String(err).slice(0, 300)}`, { to: 'debug' })
  } finally {
    draining = false
  }
  if (facts > 0) $.ui.toast(`memory: +${plural(facts, 'fact')} from ${from === 'this conversation' ? from : plural(sessions, from)}`)
}

async function memoryReport($: Engine): Promise<string> {
  const stats = await runPy($, 'store.py', ['stats'], CAPTURE_TIMEOUT_MS)
  const recent = await runPy($, 'capture.py', ['recent', '5'], CAPTURE_TIMEOUT_MS)
  const queued = await runPy($, 'capture.py', ['pending'], CAPTURE_TIMEOUT_MS)
  if (!stats) return 'Could not read the graph (is Python 3 installed?).'
  const lines = [
    `${plural(Number(stats.entities ?? 0), 'node')}, ${plural(Number(stats.relations ?? 0), 'fact')}` +
      ` from ${plural(Number(stats.origins ?? 0), 'source')}`,
    `graph: ${slash(String(stats.path ?? ''))}`, // forward slashes: a terminal's markdown eats "\."
  ]
  if (draining) lines.push('Reading earlier conversations right now: run /graph-memory again in a few seconds.')
  const waiting = Array.isArray(queued?.files) ? queued.files.length : 0
  if (waiting > 0) lines.push(`${plural(waiting, 'conversation')} waiting to be read`)
  const captures = Array.isArray(recent?.captures) ? (recent.captures as Json[]) : []
  if (captures.length > 0) lines.push('', 'Recent:')
  for (const c of captures) {
    const when = new Date(Number(c.at ?? 0) * 1000).toLocaleString()
    const sid = String(c.sessionId ?? '').slice(0, 8)
    const name = typeof c.title === 'string' && c.title ? `  "${c.title}"` : ''
    if (c.event === 'applied' || c.event === 'remembered')
      lines.push(
        `  ${when}  +${plural(Number(c.facts ?? 0), 'fact')}${c.event === 'remembered' ? ' remembered' : ''}` +
          `  session ${sid}${name}  ${String(c.project ?? '')}`,
      )
    else lines.push(`  ${when}  ${String(c.event)}  session ${sid}${name}  ${String(c.reason ?? '')}`)
  }
  lines.push('', 'Take a session back out with /graph-memory-undo (the last one) or /graph-memory-undo <session id>.')
  lines.push('Name a session with /graph-rename <session id or last> <name>.')
  return lines.join('\n')
}

const SEARCH_TOOL = 'mcp__graphcraft-memory__memory_search'
const REMEMBER_TOOL = 'mcp__graphcraft-memory__memory_remember'
type Vocab = { types: Record<string, string>; predicates: Record<string, string> }

async function registerRemember($: Engine, vocab: Vocab): Promise<void> {
  const types = Object.entries(vocab.types).map(([k, v]) => `${k}: ${v}`).join('\n')
  const predicates = Object.values(vocab.predicates).join('\n')
  const node = {
    type: 'object',
    properties: {
      name: { type: 'string', description: 'Full name, without the type word ("Linked layer", not "Linked layer decision")' },
      type: { type: 'string', enum: Object.keys(vocab.types) },
      description: { type: 'string', description: 'One short sentence: conditions, numbers, dates, the reason' },
      scope: { type: 'string', enum: ['global'], description: 'Only when it holds in every project (most preferences)' },
    },
    required: ['name', 'type'],
    additionalProperties: false,
  }
  const edge = {
    type: 'object',
    properties: {
      source: { type: 'string' },
      predicate: { type: 'string', enum: Object.keys(vocab.predicates) },
      target: { type: 'string' },
    },
    required: ['source', 'predicate', 'target'],
    additionalProperties: false,
  }
  await $.tool.register({
    name: 'memory_remember',
    description:
      "Save facts to the user's knowledge-graph memory the moment they are settled, so later sessions know them: " +
      'a decision and its reason, a problem and what fixed it (or that it is still open), a preference about how ' +
      'to work, what a project uses or is made of. Not for chores (commits, releases, test runs), plans that will ' +
      'change, or what memory already holds. Link the current project by its folder name as a PROJECT node; call ' +
      'the person "User" (PERSON). Every edge must read as a true sentence "source predicate target" with the types ' +
      'the predicate allows; anything else is dropped and reported back. Use retire for facts that are no longer ' +
      `true. At most 15 edges.\n\nTypes:\n${types}\n\nPredicates (source predicate target):\n${predicates}`,
    inputSchema: {
      type: 'object',
      properties: {
        nodes: { type: 'array', items: node, description: 'Every node an edge names, unless memory already has it' },
        edges: { type: 'array', items: edge, maxItems: 15 },
        retire: { type: 'array', items: edge, description: 'Facts that are no longer true' },
      },
      required: ['nodes', 'edges'],
      additionalProperties: false,
    },
    isDeferred: false,
  })
}

const factKey = (f: PaneFact) => `${f.source_id}|${f.predicate}|${f.target_id}`

/** Facts the person pressed Keep on, for the session they came from: they leave the Learned list. */
async function keptKeys($: Engine, sessionId: string): Promise<string[]> {
  const kept = (await $.store.get('kept')) as { sessionId?: string; keys?: string[] } | undefined
  return kept?.sessionId === sessionId && Array.isArray(kept.keys) ? kept.keys : []
}

async function keep($: Engine, sessionId: string, keys: readonly string[]): Promise<void> {
  const all = [...new Set([...(await keptKeys($, sessionId)), ...keys])]
  await $.store.set('kept', { sessionId, keys: all })
  await update($, view, v =>
    v?.learned ? { ...v, learned: { ...v.learned, facts: v.learned.facts.filter(f => !all.includes(factKey(f))) } } : v,
  )
}

/** Reads graph/pane.py state into the pane's value; leaves the old value on a failure. */
async function refreshView($: Engine): Promise<void> {
  const out = await runPy($, 'pane.py', ['state', '--cwd', await $.session.cwd()], CAPTURE_TIMEOUT_MS)
  if (!out || typeof out.error === 'string') return
  const next = out as unknown as PaneView
  if (next.learned) {
    const kept = await keptKeys($, next.learned.sessionId)
    next.learned = { ...next.learned, facts: next.learned.facts.filter(f => !kept.includes(factKey(f))) }
  }
  await update($, view, () => next)
}

/** Names a session ("last", an id or its first characters) through store.py; the reply line for the person. */
async function renameSession($: Engine, which: string, title: string): Promise<string> {
  const out = await runPy($, 'store.py', ['rename', which, '--', title], CAPTURE_TIMEOUT_MS)
  if (!out || typeof out.renamed !== 'string')
    return `Not renamed (${String(out?.reason ?? out?.error ?? 'memory could not be reached')}).`
  await refreshView($)
  return `Session ${out.renamed.slice(0, 8)} is now "${String(out.title)}".`
}

/** Swaps one fact in the pane's lists (`to` undefined: drops it). */
async function replaceFact($: Engine, from: PaneFact, to?: PaneFact): Promise<void> {
  const k = factKey(from)
  const swap = (facts: PaneFact[]) => facts.flatMap(x => (factKey(x) === k ? (to ? [to] : []) : [x]))
  await update($, used, u => (u ? { ...u, facts: swap(u.facts) } : u))
  await update($, searched, s => (s ? { ...s, facts: swap(s.facts) } : s))
}

async function forgetFact($: Engine, f: PaneFact): Promise<void> {
  await runPy($, 'pane.py', ['forget', f.source_id, f.predicate, f.target_id], CAPTURE_TIMEOUT_MS)
  await replaceFact($, f)
  await refreshView($)
}

async function searchMemory($: Engine, query: string): Promise<void> {
  await update($, fixing, () => null)
  if (!query.trim()) return void (await update($, searched, () => null))
  const out = await runPy($, 'tools.py', ['search', '--cwd', await $.session.cwd(), '--', query.trim().slice(0, 500)], CAPTURE_TIMEOUT_MS)
  const facts = Array.isArray(out?.triples) ? (out.triples as PaneFact[]) : []
  await update($, searched, () => ({ query: query.trim(), facts: facts.slice(0, SHOW_SEARCH), more: Math.max(0, facts.length - SHOW_SEARCH) }))
}

/** Opens the Fix box under one row: `where` is the list ("u" used, "s" search), since one fact can be in both and
 * two boxes would draw two elements with the same key, which the pane refuses to draw at all. */
async function openFix($: Engine, f: PaneFact, where: string): Promise<void> {
  const out = await runPy($, 'pane.py', ['links', f.source_id, f.target_id], CAPTURE_TIMEOUT_MS)
  const links = Array.isArray(out?.links) ? (out.links as Fixing['links']) : []
  await update($, fixing, () => ({ key: `${where}:${factKey(f)}`, links }))
}

async function relinkFact($: Engine, f: PaneFact, link: string): Promise<void> {
  if (link === f.predicate) return
  const args = ['relink', f.source_id, f.predicate, f.target_id, link, '--session', await $.session.id(), '--project', await $.session.cwd()]
  const out = await runPy($, 'pane.py', args, CAPTURE_TIMEOUT_MS)
  const fixed = out?.fact as PaneFact | undefined
  if (!fixed) return void $.ui.toast(`memory: not changed (${String(out?.error ?? 'no answer')})`)
  await replaceFact($, f, fixed)
  await update($, fixing, () => null)
  $.ui.toast(`memory: now "${fixed.source} ${fixed.predicate.replace(/_/g, ' ')} ${fixed.target}"`)
  await refreshView($)
}

/** A correction only Claude can make (a wrong name, a fact that should be two): a turn, as if the person typed it. */
async function tellClaude($: Engine, f: PaneFact, truth: string): Promise<void> {
  await update($, fixing, () => null)
  await $.prompt.submit({
    text: `Correct my memory: "${f.source} ${f.predicate} ${f.target}" is wrong. ${truth.trim()} ` +
      'Retire the wrong fact and save what is true with memory_remember.',
    asUser: true,
  })
}

export const register: Register = (on, options) => {
  const model = typeof options.extractModel === 'string' && options.extractModel ? options.extractModel : 'haiku'
  const isCapturing = options.capture !== false
  const every = typeof options.captureEvery === 'number' ? Math.max(0, Math.round(options.captureEvery)) : 8
  // Per session: how many of the person's messages had been queued for reading when it last happened.
  const queuedAt = new Map<string, number>()

  on('prompt.submit', async ($, e, next) => {
    if (e.command || e.isMeta || !e.text.trim()) return next(e)
    const py = await findPython($)
    if (!py) return next(e)

    // The prompt goes as an argument, not stdin: in interactive sessions stdin arrived empty. Seeding only needs the
    // words, so a long paste is cut to stay under Windows' command-line limit.
    const prompt = e.text.slice(0, MAX_PROMPT_CHARS)
    const cwd = await $.session.cwd()
    let out = await recallWarm($, prompt, cwd)
    const how = out ? 'warm' : 'cold'
    if (!out) {
      startWarm($, py) // for the next prompt
      const r = await $.process.run(
        [...py, '-I', `${slash($.plugin.root)}/graph/recall.py`, '--json', '--cwd', cwd, '--', prompt],
        { timeoutMs: RECALL_TIMEOUT_MS },
      )
      if (r.exitCode !== 0) {
        $.ui.log(`graphcraft-memory: recall failed: ${r.stderr.slice(0, 300)}`, { to: 'debug' })
        return next(e)
      }
      out = JSON.parse(r.stdout) as Recalled
    }
    $.ui.log(`graphcraft-memory: ${out.facts} facts from ${out.db} (prompt ${out.prompt_chars} chars, ${how})`, { to: 'debug' })
    $.ui.status(out.facts > 0 ? out.summary : undefined)
    await update($, used, () => ({ matched: out.matched ?? [], more: out.more ?? 0, facts: out.triples ?? [] }))
    await update($, briefShown, () => false)
    if (out.facts === 0) return next(e)
    return next({ ...e, context: [...(e.context ?? []), out.context] })
  })
  // No .catch: the engine lets a prompt through when a prompt.submit hook throws, which is what memory should do.

  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'graph-memory', description: 'graphcraft-memory: what is remembered and recent captures' })
    await $.command.register({
      name: 'graph-memory-undo',
      description: 'graphcraft-memory: remove the facts one session added (default: the last one)',
    })
    await $.command.register({
      name: 'graph-view',
      description: 'graphcraft-memory: open the memory map in your browser (optionally: a project name)',
    })
    await $.command.register({ name: 'graph-pane', description: 'graphcraft-memory: open the Memory pane' })
    await $.command.register({
      name: 'graph-export',
      description: 'graphcraft-memory: save all memory to one file to take to another PC (default: Downloads)',
    })
    await $.command.register({
      name: 'graph-import',
      description: 'graphcraft-memory: merge a /graph-export file from another PC into this one',
    })
    await $.command.register({
      name: 'graph-rename',
      description: 'graphcraft-memory: name a session: /graph-rename <session id or last> <name>',
    })
    await $.tool.register({
      name: 'memory_search',
      description:
        "Search the user's knowledge-graph memory (facts from earlier sessions and installed mods: projects, " +
        'decisions and their reasons, problems and their fixes, preferences, tools). Facts linked to each prompt ' +
        'already arrive as context; call this to dig further: something the user refers to from before, a name ' +
        'that context did not cover, or more links around one thing. Returns "source --[predicate]--> target" ' +
        'lines with where each came from, and short descriptions.',
      inputSchema: {
        type: 'object',
        properties: {
          query: { type: 'string', description: 'Names or words to look for, e.g. "post-process swap crash"' },
          hops: { type: 'integer', minimum: 1, maximum: 3, description: 'How many links out to walk (default 2)' },
        },
        required: ['query'],
        additionalProperties: false,
      },
      isDeferred: false,
    })
    const started = await next(e)
    const py = await findPython($)
    if (py) startWarm($, py)
    void (async () => {
      await refreshView($)
      // memory_remember names the types and predicates in force (core plus feeders'), so it waits for Python.
      const vocab = await runPy($, 'tools.py', ['vocab'], CAPTURE_TIMEOUT_MS)
      if (vocab?.types && vocab.predicates) await registerRemember($, vocab as unknown as Vocab)
      // Clawd and his memory bubble (graph/clawd.py): the clawd-pet mod's frames when installed, else a stand-in.
      const art = await runPy($, 'clawd.py', ['svg'], CAPTURE_TIMEOUT_MS)
      if (art?.lit && art.idle) await update($, clawd, () => art as unknown as Clawd)
      // The pane opens by itself until the person closes it; /graph-pane brings it back.
      if ((await $.store.get('paneClosed')) !== true) await $.ui.open({ id: PANE, title: 'Memory', rows: PANE_ROWS })
      // Other mods' facts (graph/feeders.py): only files that changed since the last session start are read.
      const fed = await runPy($, 'feeders.py', ['run'], CAPTURE_TIMEOUT_MS)
      for (const p of Array.isArray(fed?.problems) ? (fed.problems as string[]) : [])
        $.ui.log(`graphcraft-memory: feeder: ${p}`, { to: 'debug' })
      if (Number(fed?.facts ?? 0) > 0) {
        const names = (fed?.loaded as { source: string }[]).map(l => l.source.split(':')[0])
        $.ui.toast(`memory: ${plural(Number(fed?.facts), 'fact')} from ${[...new Set(names)].join(', ')}`)
        await refreshView($)
      }
      if (isCapturing) {
        await drain($, model)
        await refreshView($)
      }
      // One thing saved twice under different types splits its facts: join them (graph/store.py merge_duplicates).
      const merged = await runPy($, 'store.py', ['merge'], CAPTURE_TIMEOUT_MS)
      if (Array.isArray(merged?.merged) && merged.merged.length > 0)
        $.ui.log(`graphcraft-memory: joined duplicate nodes: ${(merged.merged as string[]).join(', ')}`, { to: 'debug' })
    })().catch(err => $.ui.log(`graphcraft-memory: session start: ${String(err).slice(0, 200)}`, { to: 'debug' }))
    return started
  })

  on('tool.call', { tool: SEARCH_TOOL }, async ($, e) => {
    const input = e as unknown as { query?: unknown; hops?: unknown } // an MCP tool's arguments sit on e itself
    const query = typeof input.query === 'string' ? input.query.trim().slice(0, MAX_PROMPT_CHARS) : ''
    if (!query) return { result: 'Give a query: the names or words to look for.' }
    const hops = typeof input.hops === 'number' ? String(Math.round(input.hops)) : '2'
    const out = await runPy($, 'tools.py', ['search', '--cwd', await $.session.cwd(), '--hops', hops, '--', query], CAPTURE_TIMEOUT_MS)
    if (!out) return { result: 'Memory could not be searched (is Python 3 installed?).' }
    return { result: String(out.text ?? out.error ?? 'No answer from memory.') }
  }).catch(() => ({ result: 'Memory could not be searched just now.' }))

  on('tool.call', { tool: REMEMBER_TOOL }, async ($, e) => {
    const input = e as unknown as { nodes?: unknown; edges?: unknown; retire?: unknown }
    const batch = { nodes: input.nodes ?? [], edges: input.edges ?? [], retire: input.retire ?? [] }
    const file = `${await dataDir($)}/tmp/remember-${Date.now()}.json`
    await $.fs.write(file, JSON.stringify(batch))
    const args = ['remember', file, '--session', await $.session.id(), '--project', await $.session.cwd()]
    const out = await runPy($, 'tools.py', args, CAPTURE_TIMEOUT_MS)
    if (!out || typeof out.error === 'string') return { result: `Nothing saved: ${String(out?.error ?? 'memory could not be reached')}.` }
    const facts = Number(out.facts ?? 0)
    const dropped = Array.isArray(out.dropped) ? (out.dropped as string[]) : []
    const retired = Number(out.retired ?? 0)
    if (facts > 0) $.ui.toast(`memory: +${plural(facts, 'fact')} remembered`)
    void refreshView($).catch(() => undefined)
    const lines = [`Saved ${plural(facts, 'fact')} (${plural(Number(out.nodes ?? 0), 'node')} added or updated).`]
    if (retired > 0) lines.push(`Retired ${plural(retired, 'old fact')}.`)
    if (dropped.length > 0) lines.push('Not saved:', ...dropped.map(d => `- ${d}`))
    lines.push('The user can take them back out with /graph-memory-undo or the Memory pane.')
    return { result: lines.join('\n') }
  }).catch(() => ({ result: 'Nothing saved: memory could not be reached just now.' }))

  on('command.run', { command: 'graph-pane' }, async $ => {
    await $.store.delete('paneClosed')
    await refreshView($)
    const opened = await $.ui.open({ id: PANE, title: 'Memory', rows: PANE_ROWS })
    return {
      text: opened.isPlaced
        ? 'Memory pane opened.'
        : `The Memory pane is waiting for room (${opened.reason}). /graph-memory shows the same in text.`,
    }
  })

  on('ui.close', async ($, e, next) => {
    if (e.id === PANE && e.origin === 'person') await $.store.set('paneClosed', true).catch(() => undefined)
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const v = await read($, view)
    if (!(await read($, briefShown)) || !v?.brief) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    return (
      <Box flexDirection="row" gap={1}>
        <Text dimColor>memory</Text>
        <Text>{v.brief}</Text>
        <Button role="dismiss" label="Hide" dimColor onPress={() => void update($, briefShown, () => false)} />
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const el = $.ui.resolve(e)
    const { Box, Text, Button } = el
    try {
      const u = await read($, used)
      const v = await read($, view)
      const pet = await read($, clawd)
      const learned = v?.learned
      const isRenaming = await read($, renaming)
      const found = await read($, searched)
      const fix = await read($, fixing)
      // Desktop draws each section as a card; the terminal has few rows, so there sections are plain.
      const isDesktop = 'Svg' in el
      const card = isDesktop ? ({ borderStyle: 'round', borderColor: 'subtle', paddingX: 'md', paddingY: 'sm' } as const) : {}

      const note = (text: string) => (
        <Text dimColor size="footnote">
          {text}
        </Text>
      )
      const section = (title: string, count: string, body: unknown) => (
        <Box flexDirection="column" rowGap="sm" {...card}>
          <Box flexDirection="row" justifyContent="space-between" alignItems="center" columnGap="md">
            <Text weight="semibold">{title}</Text>
            {note(count)}
          </Box>
          {body}
        </Box>
      )
      const row = (key: string, f: PaneFact, actions: unknown) => (
        <Box key={key} flexDirection="row" justifyContent="space-between" alignItems="center" columnGap="md">
          <Box flexDirection="column" flexShrink={1}>
            <Text wrap="wrap">
              <Text weight="medium">{f.source}</Text>
              <Text dimColor size="footnote">{`  ${f.predicate.replace(/_/g, ' ')}  `}</Text>
              <Text weight="medium">{f.target}</Text>
            </Text>
          </Box>
          <Box flexDirection="row" columnGap="xs" flexShrink={0}>
            {actions}
          </Box>
        </Box>
      )

      // Fix and Forget on a fact; under it, while open, the Fix box: the right link, or words for Claude.
      const fixable = (prefix: string, f: PaneFact) => {
        const k = factKey(f)
        const isOpen = fix?.key === `${prefix}:${k}`
        return (
          <Box key={`${prefix}:${k}`} flexDirection="column" rowGap="xs">
            {row(`${prefix}r:${k}`, f,
              <Box flexDirection="row" columnGap="xs">
                <Button key={`fix:${prefix}:${k}`} label={isOpen ? 'Close' : 'Fix'} variant="secondary"
                  onPress={() => void (isOpen ? update($, fixing, () => null) : openFix($, f, prefix))} />
                <Button key={`forget:${prefix === 'u' ? '' : `${prefix}:`}${k}`} label="Forget" variant="secondary" onPress={() => void forgetFact($, f)} />
              </Box>)}
            {isOpen && (
              <Box flexDirection="column" rowGap="xs" paddingLeft="md">
                {fix.links.length > 0 && (
                  <el.Select
                    key={`fix-link:${prefix}`}
                    label="Link"
                    value={fix.links.some(l => l.value === f.predicate) ? f.predicate : fix.links[0]!.value}
                    options={fix.links}
                    onSelect={value => void relinkFact($, f, value)}
                  />
                )}
                <el.Input
                  key={`fix-tell:${prefix}`}
                  placeholder="Or tell Claude what is true"
                  submitLabel="tell Claude"
                  onSubmit={value => void (value.trim() ? tellClaude($, f, value) : undefined)}
                />
              </Box>
            )}
          </Box>
        )
      }

      const usedFacts = u?.facts ?? []
      const hidden = u ? usedFacts.length - SHOW_USED + u.more : 0
      const open = v?.problems.filter(p => !p.fixed).length ?? 0
      const subtitle = [
        u ? `${plural(usedFacts.length, 'fact')} recalled` : 'waiting for a message',
        learned && learned.facts.length > 0 ? `${learned.facts.length} to review` : '',
        open > 0 ? plural(open, 'open problem') : '',
      ]
        .filter(Boolean)
        .join(' · ')

      return (
        <Box flexDirection="column" rowGap="md">
          <Box flexDirection="row" justifyContent="space-between" alignItems="center" columnGap="md">
            <Box flexDirection="column" flexShrink={1}>
              <Text weight="semibold" size="heading">
                {v?.project || 'Memory'}
              </Text>
              {note(subtitle)}
            </Box>
            <Box flexDirection="row" alignItems="center" columnGap="sm" flexShrink={0}>
              {'Svg' in el && pet && (
                <el.Svg
                  source={pet[usedFacts.length > 0 ? 'lit' : 'idle'].svg}
                  alt={usedFacts.length > 0 ? 'Clawd thinking: a memory graph builds in his bubble' : 'Clawd looking around, memories forming'}
                  width={pet.lit.width}
                  height={pet.lit.height}
                />
              )}
              <Button
                label="Open map"
                variant="secondary"
                onPress={() => void runPy($, 'view.py', ['build', '--open'], CAPTURE_TIMEOUT_MS)}
              />
            </Box>
          </Box>

          <Box flexDirection="column" rowGap="sm">
            <el.Input
              key="search"
              placeholder="Search memory: names or words"
              value={found?.query ?? ''}
              submitLabel="search"
              onSubmit={value => void searchMemory($, value)}
            />
            {found &&
              section(
                `Search: ${found.query}`,
                found.facts.length === 0 ? 'nothing found' : plural(found.facts.length + found.more, 'fact'),
                <Box flexDirection="column" rowGap="sm">
                  {found.facts.length === 0 && note('Nothing in memory matches. Try other words, or a name.')}
                  {found.facts.map(f => fixable('s', f))}
                  {found.more > 0 && note(`+${found.more} more · ask Claude to search memory for the rest`)}
                  <Box flexDirection="row" marginTop="xs">
                    <Button key="search-clear" label="Clear" variant="secondary" onPress={() => void searchMemory($, '')} />
                  </Box>
                </Box>,
              )}
          </Box>

          {section(
            'Used for this prompt',
            u && u.matched.length > 0 ? `matched ${u.matched.map(m => `"${m}"`).join(', ')}` : '',
            <Box flexDirection="column" rowGap="sm">
              {!u && note('Send a message: the facts recalled for it show here.')}
              {u && usedFacts.length === 0 && note('Nothing in memory is linked to the last message.')}
              {usedFacts.slice(0, SHOW_USED).map(f => fixable('u', f))}
              {hidden > 0 && note(`+${hidden} more · all in the map`)}
            </Box>,
          )}

          {learned &&
            section(
              'Learned recently',
              plural(learned.total, 'fact'),
              <Box flexDirection="column" rowGap="sm">
                {isRenaming ? (
                  <Box flexDirection="row" columnGap="sm" alignItems="center">
                    <el.Input
                      key="rename"
                      label="Name"
                      value={learned.title}
                      placeholder="What this session was about"
                      submitLabel="rename"
                      autoFocus
                      onSubmit={value =>
                        void (async () => {
                          if (value.trim()) $.ui.toast(`memory: ${await renameSession($, learned.sessionId, value.trim())}`)
                          await update($, renaming, () => false)
                        })()
                      }
                    />
                    <Button key="rename-cancel" label="Cancel" variant="secondary" onPress={() => void update($, renaming, () => false)} />
                  </Box>
                ) : (
                  <Box flexDirection="row" justifyContent="space-between" alignItems="center" columnGap="md">
                    <Text wrap="wrap" weight="medium" dimColor={learned.titleIsAuto || !learned.title}>
                      {learned.title || `Session ${learned.sessionId.slice(0, 8)}`}
                    </Text>
                    <Button key="rename-start" label="Rename" variant="secondary" onPress={() => void update($, renaming, () => true)} />
                  </Box>
                )}
                {learned.facts.length === 0 && note('All reviewed.')}
                {learned.facts.slice(0, SHOW_LEARNED).map(f =>
                  row(
                    `l:${factKey(f)}`,
                    f,
                    <Box flexDirection="row" columnGap="xs">
                      <Button key={`keep:${factKey(f)}`} label="Keep" variant="secondary" onPress={() => void keep($, learned.sessionId, [factKey(f)])} />
                      <Button key={`drop:${factKey(f)}`} label="Drop" variant="secondary" onPress={() => void forgetFact($, f)} />
                    </Box>,
                  ),
                )}
                {learned.facts.length > SHOW_LEARNED && note(`+${learned.facts.length - SHOW_LEARNED} more to review`)}
                {learned.facts.length > 0 && (
                  <Box flexDirection="row" columnGap="sm" marginTop="xs">
                    <Button label="Keep all" variant="primary" onPress={() => void keep($, learned.sessionId, learned.facts.map(factKey))} />
                    <Button
                      label="Undo session"
                      variant="secondary"
                      onPress={() =>
                        void (async () => {
                          const out = await runPy($, 'capture.py', ['undo', learned.sessionId], CAPTURE_TIMEOUT_MS)
                          $.ui.toast(`memory: removed ${plural(Number(out?.relations ?? 0), 'fact')} from that session`)
                          await refreshView($)
                        })()
                      }
                    />
                  </Box>
                )}
              </Box>,
            )}

          {v &&
            v.problems.length > 0 &&
            section(
              'Problems',
              open > 0 ? `${open} open` : 'all fixed',
              <Box flexDirection="column" rowGap="sm">
                {v.problems.map(p => (
                  <Box key={`p:${p.name}`} flexDirection="row" columnGap="sm" alignItems="flex-start">
                    <Text color={p.fixed ? 'success' : 'warning'} size="footnote" weight="semibold">
                      {p.fixed ? '✓ fixed' : '● open'}
                    </Text>
                    <Box flexDirection="column" flexShrink={1}>
                      <Text wrap="wrap">{p.name}</Text>
                      {p.fixed && p.fixedBy ? note(`by ${p.fixedBy}`) : null}
                    </Box>
                  </Box>
                ))}
              </Box>,
            )}
        </Box>
      )
    } catch (err) {
      // Logged and shown, so a pane that draws nothing can be told from one with nothing to show.
      $.ui.log(`graphcraft-memory: pane failed to draw: ${String(err).slice(0, 300)}`, { to: 'debug' })
      return <Text color="error">Memory pane could not draw: {String(err).slice(0, 200)}</Text>
    }
  })

  on('command.run', { command: 'graph-memory' }, async $ => ({ text: await memoryReport($) }))

  const pathArg = (args: string) => args.trim().replace(/^(["'])(.*)\1$/, '$2')

  on('command.run', { command: 'graph-export' }, async ($, e) => {
    const out = await runPy($, 'sync.py', ['export', ...(pathArg(e.args) ? [pathArg(e.args)] : [])], CAPTURE_TIMEOUT_MS)
    if (!out || typeof out.exported !== 'string') return { text: `Nothing exported (${String(out?.error ?? 'is Python 3 installed?')}).` }
    return {
      text: `Saved ${plural(Number(out.facts), 'fact')} and ${plural(Number(out.nodes), 'node')} from ` +
        `${plural(Number(out.sources), 'source')} to:\n${slash(out.exported)}\n` +
        'Copy it to the other PC (USB stick, cloud folder, email to yourself) and run /graph-import <that file> there.',
    }
  })

  on('command.run', { command: 'graph-import' }, async ($, e) => {
    const file = pathArg(e.args)
    if (!file) return { text: 'Usage: /graph-import <file from /graph-export>' }
    const out = await runPy($, 'sync.py', ['import', file], CAPTURE_TIMEOUT_MS)
    if (!out || typeof out.imported !== 'string') return { text: `Nothing imported (${String(out?.error ?? 'is Python 3 installed?')}).` }
    await refreshView($)
    const lines = [`Merged: +${plural(Number(out.facts), 'fact')}, +${plural(Number(out.nodes), 'node')}, ` +
      `+${plural(Number(out.sources), 'source')}` + (Number(out.retired) ? `, ${plural(Number(out.retired), 'fact')} retired as on the other PC` : '') + '.']
    if (out.backup) lines.push(`This PC's memory before the import is kept at ${slash(String(out.backup))}`)
    return { text: lines.join('\n') }
  })

  on('command.run', { command: 'graph-rename' }, async ($, e) => {
    const m = /^(\S+)\s+([\s\S]+)$/.exec(e.args.trim())
    if (!m) return { text: 'Usage: /graph-rename <session id, its first characters, or last> <name>' }
    return { text: await renameSession($, m[1], m[2].trim()) }
  })

  on('command.run', { command: 'graph-view' }, async ($, e) => {
    const args = ['build', '--open']
    if (e.args.trim()) args.push('--project', e.args.trim())
    const out = await runPy($, 'view.py', args, CAPTURE_TIMEOUT_MS)
    if (!out || typeof out.path !== 'string')
      return { text: `Could not build the map (${String(out?.error ?? 'is Python 3 installed?')}).` }
    const where = slash(out.path)
    const counts = `${plural(Number(out.nodes ?? 0), 'node')}, ${plural(Number(out.edges ?? 0), 'fact')}`
    return {
      text: out.opened
        ? `Map opened in your browser (${counts}).\nFile: ${where}`
        : `Map written (${counts}). Open it in your browser: ${String(out.url)}`,
    }
  })

  on('command.run', { command: 'graph-memory-undo' }, async ($, e) => {
    const out = await runPy($, 'capture.py', ['undo', e.args.trim() || 'last'], CAPTURE_TIMEOUT_MS)
    if (!out || typeof out.undone !== 'string')
      return { text: `Nothing undone (${String(out?.reason ?? out?.error ?? 'no answer')}).` }
    return {
      text: `Removed ${plural(Number(out.relations ?? 0), 'fact')} and ${plural(Number(out.entities ?? 0), 'node')} from session ${out.undone}.`,
    }
  })

  // After each reply of the main conversation: rewrite this session's draft. The session is fully alive here, which
  // it no longer is at session.end (reading messages there fails: the REPL has already unmounted).
  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    if (isCapturing && e.agentId === undefined) {
      void (async () => {
        try {
          const messages = (await $.session.messages()) as readonly Turn[]
          await saveJob($, 'drafts', 'turn', messages)
          // Every `every` messages of the person's: read the new part now (capture.py skips what was read before).
          const sid = await $.session.id()
          const users = messages.filter(m => m.role === 'user' && m.text.trim() !== '').length
          if (every > 0 && users - (queuedAt.get(sid) ?? 0) >= every) {
            queuedAt.set(sid, users)
            await saveJob($, 'pending', 'turns', messages)
            await drain($, model, 'this conversation')
            await refreshView($)
          }
        } catch (err) {
          $.ui.log(`graphcraft-memory: could not save the draft: ${String(err).slice(0, 200)}`, { to: 'debug' })
        }
      })()
    }
    return done
  })

  on('session.compact', async ($, e, next) => {
    if (isCapturing && e.agentId === undefined && e.trigger !== 'precompute') {
      try {
        await saveJob($, 'pending', 'compact', e.messages)
        void drain($, model)
      } catch (err) {
        $.ui.log(`graphcraft-memory: could not queue before compaction: ${String(err).slice(0, 200)}`, { to: 'debug' })
      }
    }
    return next(e)
  })

  // Only a marker: the draft is already on disk. capture.py queues a draft that has one at once, and any other draft
  // after it has sat unchanged for a while (a crash or a closed window raises no session.end).
  on('session.end', async ($, e, next) => {
    if (isCapturing) {
      try {
        await $.fs.write(`${await dataDir($)}/drafts/${safeId(e.sessionId)}.ended`, String(Date.now()))
      } catch (err) {
        $.ui.log(`graphcraft-memory: could not mark the session ended: ${String(err).slice(0, 200)}`, { to: 'debug' })
      }
    }
    return next(e)
  })
}
