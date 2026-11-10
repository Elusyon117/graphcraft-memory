import type { EngineInterface as Engine, Register } from 'claude-code'

// Push recall: before each prompt reaches Claude, graph/recall.py finds the facts linked to what the prompt names
// and they ride along as context the user never sees. A failure here never blocks a prompt: it just adds nothing.

const CANDIDATES: readonly (readonly string[])[] = [['py', '-3'], ['python3'], ['python']]
const RECALL_TIMEOUT_MS = 5000

// Found once per load; undefined = not looked yet, null = no Python 3 on this machine.
let python: readonly string[] | null | undefined

const slash = (p: string) => p.replace(/\\/g, '/').replace(/\/+$/, '')

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
  $.ui.log('graphcraft-memory: no Python 3 found (tried py -3, python3, python); recall is off.', { to: 'debug' })
  return (python = null)
}

export const register: Register = on => {
  on('prompt.submit', async ($, e, next) => {
    if (e.command || e.isMeta || !e.text.trim()) return next(e)
    const py = await findPython($)
    if (!py) return next(e)

    const script = `${slash($.plugin.root)}/graph/recall.py`
    const cwd = await $.session.cwd()
    const r = await $.process.run([...py, '-I', script, '--stdin', '--json'], {
      stdin: JSON.stringify({ prompt: e.text, cwd }),
      timeoutMs: RECALL_TIMEOUT_MS,
    })
    if (r.exitCode !== 0) {
      $.ui.log(`graphcraft-memory: recall failed: ${r.stderr.slice(0, 300)}`, { to: 'debug' })
      return next(e)
    }
    const out = JSON.parse(r.stdout) as { facts: number; summary: string; context: string }
    $.ui.status(out.facts > 0 ? out.summary : undefined)
    if (out.facts === 0) return next(e)
    return next({ ...e, context: [...(e.context ?? []), out.context] })
  })
  // No .catch: the engine lets a prompt through when a prompt.submit hook throws, which is what memory should do.
}
