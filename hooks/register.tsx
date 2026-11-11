import type { EngineInterface as Engine, Register } from 'claude-code'

// graphcraft-memory, the mod side. Python (graph/) owns the graph; this module only decides when things run:
// - prompt.submit: recall the facts linked to the prompt and attach them as context (never blocks a prompt)
// - turn.complete: keep a draft of the conversation; session.end marks it finished; session.compact queues it
// - session.start: extract queued conversations in the background with a small model, then tell the user
// - /graph-memory, /graph-memory-undo, /graph-view: see what was learned, take a session back out, open the map

const CANDIDATES: readonly (readonly string[])[] = [['py', '-3'], ['python3'], ['python']]
const RECALL_TIMEOUT_MS = 5000
const CAPTURE_TIMEOUT_MS = 30000
const MAX_PROMPT_CHARS = 8000
const EXTRACT_MAX_TOKENS = 4096

type Json = Record<string, unknown>
type Turn = { role: string; text: string }

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
async function drain($: Engine, model: string): Promise<void> {
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
  if (facts > 0) $.ui.toast(`memory: +${plural(facts, 'fact')} from ${plural(sessions, 'earlier conversation')}`)
}

async function memoryReport($: Engine): Promise<string> {
  const stats = await runPy($, 'store.py', ['stats'], CAPTURE_TIMEOUT_MS)
  const recent = await runPy($, 'capture.py', ['recent', '5'], CAPTURE_TIMEOUT_MS)
  const queued = await runPy($, 'capture.py', ['pending'], CAPTURE_TIMEOUT_MS)
  if (!stats) return 'graphcraft-memory: could not read the graph (is Python 3 installed?).'
  const lines = [
    `graphcraft-memory: ${plural(Number(stats.entities ?? 0), 'node')}, ${plural(Number(stats.relations ?? 0), 'fact')}` +
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
    if (c.event === 'applied')
      lines.push(`  ${when}  +${plural(Number(c.facts ?? 0), 'fact')}  session ${sid}  ${String(c.project ?? '')}`)
    else lines.push(`  ${when}  ${String(c.event)}  session ${sid}  ${String(c.reason ?? '')}`)
  }
  lines.push('', 'Take a session back out with /graph-memory-undo (the last one) or /graph-memory-undo <session id>.')
  return lines.join('\n')
}

export const register: Register = (on, options) => {
  const model = typeof options.extractModel === 'string' && options.extractModel ? options.extractModel : 'haiku'
  const isCapturing = options.capture !== false

  on('prompt.submit', async ($, e, next) => {
    if (e.command || e.isMeta || !e.text.trim()) return next(e)
    const py = await findPython($)
    if (!py) return next(e)

    // The prompt goes as an argument, not stdin: in interactive sessions stdin arrived empty. Seeding only needs the
    // words, so a long paste is cut to stay under Windows' command-line limit.
    const prompt = e.text.slice(0, MAX_PROMPT_CHARS)
    const cwd = await $.session.cwd()
    const r = await $.process.run(
      [...py, '-I', `${slash($.plugin.root)}/graph/recall.py`, '--json', '--cwd', cwd, '--', prompt],
      { timeoutMs: RECALL_TIMEOUT_MS },
    )
    if (r.exitCode !== 0) {
      $.ui.log(`graphcraft-memory: recall failed: ${r.stderr.slice(0, 300)}`, { to: 'debug' })
      return next(e)
    }
    const out = JSON.parse(r.stdout) as { facts: number; summary: string; context: string; db: string; prompt_chars: number }
    $.ui.log(`graphcraft-memory: ${out.facts} facts from ${out.db} (prompt ${out.prompt_chars} chars)`, { to: 'debug' })
    $.ui.status(out.facts > 0 ? out.summary : undefined)
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
    const started = await next(e)
    if (isCapturing) void drain($, model)
    return started
  })

  on('command.run', { command: 'graph-memory' }, async $ => ({ text: await memoryReport($) }))

  on('command.run', { command: 'graph-view' }, async ($, e) => {
    const args = ['build', '--open']
    if (e.args.trim()) args.push('--project', e.args.trim())
    const out = await runPy($, 'view.py', args, CAPTURE_TIMEOUT_MS)
    if (!out || typeof out.path !== 'string')
      return { text: `graphcraft-memory: could not build the map (${String(out?.error ?? 'is Python 3 installed?')}).` }
    const where = slash(out.path)
    const counts = `${plural(Number(out.nodes ?? 0), 'node')}, ${plural(Number(out.edges ?? 0), 'fact')}`
    return {
      text: out.opened
        ? `graphcraft-memory: map opened in your browser (${counts}).\nFile: ${where}`
        : `graphcraft-memory: map written (${counts}). Open it in your browser: ${String(out.url)}`,
    }
  })

  on('command.run', { command: 'graph-memory-undo' }, async ($, e) => {
    const out = await runPy($, 'capture.py', ['undo', e.args.trim() || 'last'], CAPTURE_TIMEOUT_MS)
    if (!out || typeof out.undone !== 'string')
      return { text: `graphcraft-memory: nothing undone (${String(out?.reason ?? out?.error ?? 'no answer')}).` }
    return {
      text: `graphcraft-memory: removed ${plural(Number(out.relations ?? 0), 'fact')} and ${plural(Number(out.entities ?? 0), 'node')} from session ${out.undone}.`,
    }
  })

  // After each reply of the main conversation: rewrite this session's draft. The session is fully alive here, which
  // it no longer is at session.end (reading messages there fails: the REPL has already unmounted).
  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    if (isCapturing && e.agentId === undefined) {
      void (async () => {
        try {
          await saveJob($, 'drafts', 'turn', (await $.session.messages()) as readonly Turn[])
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
