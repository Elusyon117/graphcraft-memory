import { expect, test } from 'claude-code/testing'

// The Memory pane and the brief band, drawn from what Python answers. Python is not run: `process.run` is answered
// here by the script name, the way graph/*.py would print it.

const FACT = { source: 'Campfires', predicate: 'decided', target: 'Linked anim layer', source_id: 'a', target_id: 'b' }
const LEARNED = { source: 'User', predicate: 'prefers', target: 'Short replies', source_id: 'u', target_id: 'p' }
const STATE = {
  project: 'Campfires',
  brief: 'Last time in Campfires: latest decision: Linked anim layer · 1 open problem',
  learned: { sessionId: 's-1', at: 1, total: 1, facts: [LEARNED], title: 'Fix the sit-animation crash', titleIsAuto: false },
  problems: [{ name: 'Bedroll overrides AnimBP_Player', fixed: false, fixedBy: '' }],
}
const RECALL = {
  facts: 1, summary: 'memory: 1 facts, 2 ms', context: 'ctx', db: 'graph.db', prompt_chars: 20,
  triples: [FACT], matched: ['Campfires'], more: 0,
}

const ART = (name: string) => ({ svg: `<svg xmlns="http://www.w3.org/2000/svg"><title>${name}</title></svg>`, width: 77, height: 64 })
const CLAWD = { lit: ART('lit'), idle: ART('idle'), source: 'own' }

const VOCAB = { types: { PROJECT: 'A folder', DECISION: 'A choice' }, predicates: { decided: 'PROJECT decided DECISION' } }

const PANE = {
  plugin: 'graphcraft-memory',
  component: 'Pane',
  requestId: 'memory',
  props: {
    title: 'Memory',
    isFocused: false,
    bodyColumns: 60,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 40 },
    view: {},
  },
} as const

function engine(on: Parameters<Parameters<typeof test>[1]>[1], calls: string[][]) {
  on('command.register', () => ({ value: undefined }))
  on('tool.register', (_, e) => ({ value: { tool: `mcp__graphcraft-memory__${e.name}` } }))
  on('session.id', () => ({ value: 'sess-1' }))
  on('env.get', () => ({ value: undefined }))
  on('fs.write', (_, e) => {
    calls.push(['write', e.path])
    return { value: undefined }
  })
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('prompt.submit', (_, e) => {
    calls.push(['prompt', e.text])
    return e
  })
  on('store.get', () => ({ value: undefined }))
  on('store.set', () => ({ value: undefined }))
  on('store.delete', () => ({ value: undefined }))
  on('ui.log', () => ({ value: undefined }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('session.cwd', () => ({ value: 'C:/mods/Campfires' }))
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })
  on('process.run', (_, e) => {
    calls.push([...e.argv])
    const argv = e.argv.join(' ')
    const out = argv.includes('--version') ? '' : argv.includes('recall.py') ? RECALL
      : argv.includes('clawd.py') ? CLAWD
      : argv.includes('tools.py vocab') ? VOCAB
      : argv.includes('tools.py search') ? { facts: 1, text: 'Memory facts linked to it: Campfires --[decided]--> Linked anim layer', triples: [FACT] }
      : argv.includes('pane.py links') ? { links: [{ value: 'decided', label: 'Campfires decided Linked anim layer' }, { value: 'avoids', label: 'Campfires avoids Linked anim layer' }] }
      : argv.includes('pane.py relink') ? { retired: 1, fact: { ...FACT, predicate: 'avoids' } }
      : argv.includes('tools.py remember') ? { facts: 1, nodes: 2, retired: 0, dropped: ["edge 'X' (TOOL) 'about' 'Y'"] }
      : argv.includes('sync.py export') ? { exported: 'C:\\Users\\me\\Downloads\\graphcraft-memory-2026-11-19.json', nodes: 74, facts: 82, sources: 6 }
      : argv.includes('sync.py import') ? { imported: e.argv[e.argv.length - 1], backup: 'C:/x/graph.before-import.db', nodes: 3, facts: 5, sources: 1, retired: 1 }
      : argv.includes('store.py rename') ? { renamed: 's-1-full-id', title: e.argv[e.argv.length - 1] }
      : argv.includes('pane.py state') ? STATE : argv.includes('pane.py forget') ? { retired: 1 } : { files: [] }
    return {
      value: {
        exitCode: 0, stdout: argv.includes('--version') ? '' : JSON.stringify(out),
        stderr: argv.includes('--version') ? 'Python 3.12.0' : '', isStdoutTruncated: false, isStderrTruncated: false,
      },
    }
  })
}

test('the pane shows used facts, learned facts and problems, on terminal and desktop', { options: { capture: false } }, async ($, on) => {
  const calls: string[][] = []
  engine(on, calls)
  await $.session.start({ cwd: 'C:/mods/Campfires', surface: 'desktop', isInteractive: true })
  await $.prompt.submit({ text: 'why did Campfires crash?' })

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Text', text: /matched "Campfires"/ }))?.text).toBeTruthy()
    expect((await ui.find({ type: 'Text', text: /Linked anim layer/ }))?.text).toContain('decided')
    expect((await ui.find({ type: 'Text', text: /Short replies/ }))?.text).toContain('prefers')
    expect((await ui.find({ type: 'Text', text: /Bedroll overrides/ }))?.text).toBeTruthy()
    expect((await ui.find({ type: 'Text', text: /● open/ }))?.text).toBeTruthy()
    const clawd = await ui.find({ type: 'Svg' } as never)
    if (surface === 'desktop') expect(clawd).toBeTruthy()
    else expect(clawd).toBeFalsy()
    await ui.unmount()
  }
})

test('Forget retires the fact by ids and drops it from the list', { options: { capture: false } }, async ($, on) => {
  const calls: string[][] = []
  engine(on, calls)
  await $.session.start({ cwd: 'C:/mods/Campfires', surface: 'desktop', isInteractive: true })
  await $.prompt.submit({ text: 'why did Campfires crash?' })

  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'forget:a|decided|b' })
  expect(calls.some(c => c.join(' ').includes('pane.py forget a decided b'))).toBe(true)
  expect(await ui.find({ type: 'Text', text: /Linked anim layer/ })).toBeFalsy()
  await ui.unmount()
})

test('Keep hides a learned fact without touching the graph', { options: { capture: false } }, async ($, on) => {
  const calls: string[][] = []
  engine(on, calls)
  await $.session.start({ cwd: 'C:/mods/Campfires', surface: 'desktop', isInteractive: true })
  await $.prompt.submit({ text: 'hello Campfires' })

  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'keep:u|prefers|p' })
  expect(await ui.find({ type: 'Text', text: /Short replies/ })).toBeFalsy()
  expect((await ui.find({ type: 'Text', text: /All reviewed/ }))?.text).toBeTruthy()
  expect(calls.some(c => c.join(' ').includes('pane.py forget'))).toBe(false)
  await ui.unmount()
})

test('the brief shows above the prompt until the first message', { options: { capture: false } }, async ($, on) => {
  engine(on, [])
  await $.session.start({ cwd: 'C:/mods/Campfires', surface: 'terminal', isInteractive: true })
  const band = { plugin: 'graphcraft-memory', component: 'AbovePrompt', surface: 'terminal' } as const

  const before = await $.ui.mount({ ...band, props: { hasSurvey: false, isCollapsed: false, isWorking: false, maxRows: 10 } } as never)
  expect((await before.find({ type: 'Text', text: /Last time in Campfires/ }))?.text).toBeTruthy()
  await before.unmount()

  await $.prompt.submit({ text: 'hello' })
  const after = await $.ui.mount({ ...band, props: { hasSurvey: false, isCollapsed: false, isWorking: false, maxRows: 10 } } as never)
  expect(await after.find({ type: 'Text', text: /Last time in Campfires/ })).toBeFalsy()
  await after.unmount()
})

test('memory_search answers with what memory found', { options: { capture: false } }, async ($, on) => {
  const calls: string[][] = []
  engine(on, calls)
  await $.session.start({ cwd: 'C:/mods/Campfires', surface: 'desktop', isInteractive: true })
  const out = await $.tool.call({ tool: 'mcp__graphcraft-memory__memory_search', query: 'swap crash' } as never)
  expect(JSON.stringify(out)).toContain('Linked anim layer')
  const ran = calls.find(c => c.join(' ').includes('tools.py search'))
  expect(ran?.slice(-2)).toEqual(['--', 'swap crash']) // the query goes after --, never read as an option
})

test('memory_remember saves under this session and reports what was dropped', { options: { capture: false } }, async ($, on) => {
  const calls: string[][] = []
  engine(on, calls)
  await $.session.start({ cwd: 'C:/mods/Campfires', surface: 'desktop', isInteractive: true })
  const out = await $.tool.call({
    tool: 'mcp__graphcraft-memory__memory_remember',
    nodes: [{ name: 'Campfires', type: 'PROJECT' }, { name: 'Linked layer', type: 'DECISION' }],
    edges: [{ source: 'Campfires', predicate: 'decided', target: 'Linked layer' }],
  } as never)
  const text = JSON.stringify(out)
  expect(text).toContain('Saved 1 fact')
  expect(text).toContain('Not saved')
  expect(calls.find(c => c.join(' ').includes('tools.py remember'))?.join(' ')).toContain('--session sess-1')
})

test('the pane names the last session, and Rename saves a new name', { options: { capture: false } }, async ($, on) => {
  const calls: string[][] = []
  engine(on, calls)
  await $.session.start({ cwd: 'C:/mods/Campfires', surface: 'desktop', isInteractive: true })

  for (const surface of ['terminal', 'desktop'] as const) {
    calls.length = 0
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Text', text: /Fix the sit-animation crash/ }))?.text).toBeTruthy()
    await ui.press({ key: 'rename-start' })
    await ui.input({ key: 'rename', text: 'Campfire sit fix' })
    const ran = calls.find(c => c.join(' ').includes('store.py rename'))
    expect(ran?.slice(-4)).toEqual(['rename', 's-1', '--', 'Campfire sit fix'])
    expect(await ui.find({ key: 'rename' })).toBeFalsy() // the field closes after Enter
    await ui.unmount()
  }
})

test('/graph-rename passes the session and the whole name; usage without a name', { options: { capture: false } }, async ($, on) => {
  const calls: string[][] = []
  engine(on, calls)
  await $.session.start({ cwd: 'C:/mods/Campfires', surface: 'terminal', isInteractive: true })
  const out = await $.command.run({ command: 'graph-rename', args: 'last  Pixel memory bubble' } as never)
  expect(out.text).toContain('"Pixel memory bubble"')
  expect(calls.find(c => c.join(' ').includes('store.py rename'))?.slice(-3)).toEqual(['last', '--', 'Pixel memory bubble'])
  const usage = await $.command.run({ command: 'graph-rename', args: 'last' } as never)
  expect(usage.text).toContain('Usage')
})

test('search lists matching facts, and Clear puts the pane back', { options: { capture: false } }, async ($, on) => {
  const calls: string[][] = []
  engine(on, calls)
  await $.session.start({ cwd: 'C:/mods/Campfires', surface: 'desktop', isInteractive: true })
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    await ui.input({ key: 'search', text: 'anim layer' })
    expect(calls.find(c => c.join(' ').includes('tools.py search'))?.slice(-2)).toEqual(['--', 'anim layer'])
    expect((await ui.find({ type: 'Text', text: /Search: anim layer/ }))?.text).toBeTruthy()
    expect((await ui.find({ type: 'Text', text: /Linked anim layer/ }))?.text).toContain('decided')
    await ui.press({ key: 'search-clear' })
    expect(await ui.find({ type: 'Text', text: /Search: anim layer/ })).toBeFalsy()
    await ui.unmount()
  }
})

test('Fix offers the allowed links and saves the one picked', { options: { capture: false } }, async ($, on) => {
  const calls: string[][] = []
  engine(on, calls)
  await $.session.start({ cwd: 'C:/mods/Campfires', surface: 'desktop', isInteractive: true })
  await $.prompt.submit({ text: 'why did Campfires crash?' })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'fix:u:a|decided|b' })
  expect(calls.some(c => c.join(' ').includes('pane.py links a b'))).toBe(true)
  await ui.select({ key: 'fix-link:u', value: 'avoids' } as never)
  const ran = calls.find(c => c.join(' ').includes('pane.py relink'))?.join(' ')
  expect(ran).toContain('relink a decided b avoids --session sess-1')
  expect((await ui.find({ type: 'Text', text: /Linked anim layer/ }))?.text).toContain('avoids')
  expect(await ui.find({ key: 'fix-link:u' })).toBeFalsy()
  await ui.unmount()
})

test('Fix can hand a correction to Claude as a message', { options: { capture: false } }, async ($, on) => {
  const calls: string[][] = []
  engine(on, calls)
  await $.session.start({ cwd: 'C:/mods/Campfires', surface: 'desktop', isInteractive: true })
  await $.prompt.submit({ text: 'why did Campfires crash?' })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'fix:u:a|decided|b' })
  await ui.input({ key: 'fix-tell:u', text: 'We dropped the linked layer in build 5.' })
  const sent = calls.filter(c => c[0] === 'prompt').map(c => c[1]).pop() ?? ''
  expect(sent).toContain('"Campfires decided Linked anim layer" is wrong. We dropped the linked layer in build 5.')
  expect(sent).toContain('memory_remember')
  await ui.unmount()
})

test('Fix on a fact that is both recalled and searched draws one box, and the pane still draws', { options: { capture: false } }, async ($, on) => {
  const calls: string[][] = []
  engine(on, calls)
  await $.session.start({ cwd: 'C:/mods/Campfires', surface: 'desktop', isInteractive: true })
  await $.prompt.submit({ text: 'why did Campfires crash?' })
  for (const where of ['s', 'u'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
    await ui.input({ key: 'search', text: 'anim layer' }) // FACT is now in both lists
    await ui.press({ key: `fix:${where}:a|decided|b` })
    expect(await ui.find({ key: `fix-link:${where}` })).toBeTruthy()
    expect(await ui.find({ key: `fix-link:${where === 's' ? 'u' : 's'}` })).toBeFalsy()
    expect((await ui.find({ type: 'Text', text: /Used for this prompt/ }))?.text).toBeTruthy()
    await ui.unmount()
  }
})

test('/graph-export and /graph-import carry memory between PCs', { options: { capture: false } }, async ($, on) => {
  const calls: string[][] = []
  engine(on, calls)
  await $.session.start({ cwd: 'C:/mods/Campfires', surface: 'terminal', isInteractive: true })
  const saved = await $.command.run({ command: 'graph-export', args: '' } as never)
  expect(saved.text).toContain('82 facts')
  expect(saved.text).toContain('C:/Users/me/Downloads/graphcraft-memory-2026-11-19.json')
  expect(calls.find(c => c.join(' ').includes('sync.py export'))?.slice(-1)).toEqual(['export'])
  const merged = await $.command.run({ command: 'graph-import', args: '"D:/USB/my memory.json"' } as never)
  expect(calls.find(c => c.join(' ').includes('sync.py import'))?.slice(-1)).toEqual(['D:/USB/my memory.json'])
  expect(merged.text).toContain('+5 facts')
  expect(merged.text).toContain('1 fact retired as on the other PC')
  expect((await $.command.run({ command: 'graph-import', args: '' } as never)).text).toContain('Usage')
})

test('recall goes through the warm process once it is up, and falls back to recall.py when it is not', { options: { capture: false } }, async ($, on) => {
  const calls: string[][] = []
  engine(on, calls)
  const asked: { url: string; token?: string }[] = []
  let answering = true
  on('process.spawn', async function* (_, e) {
    calls.push(['spawn', ...e.argv])
    yield { stream: 'stdout' as const, text: '{"port": 45678}\n' }
    await new Promise(() => undefined) // runs for the session
    return { code: 0, signal: null }
  })
  on('http.fetch', (_, e) => {
    asked.push({ url: e.url, token: e.init?.headers?.['X-Token'] })
    if (!answering) throw new Error('connection refused')
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...RECALL, summary: 'memory: 1 facts, 3 ms' }) } }
  })
  await $.session.start({ cwd: 'C:/mods/Campfires', surface: 'terminal', isInteractive: true })
  await new Promise(done => setTimeout(done, 20)) // the port line arrives
  expect(calls.some(c => c[0] === 'spawn' && c.join(' ').includes('serve.py'))).toBe(true)

  const before = calls.filter(c => c.join(' ').includes('recall.py')).length
  await $.prompt.submit({ text: 'why did Campfires crash?' })
  expect(asked[0]?.url).toBe('http://127.0.0.1:45678/recall')
  expect(asked[0]?.token?.length).toBeGreaterThan(20)
  expect(calls.filter(c => c.join(' ').includes('recall.py')).length).toBe(before) // no cold run

  answering = false
  await $.prompt.submit({ text: 'why did Campfires crash again?' })
  expect(calls.filter(c => c.join(' ').includes('recall.py')).length).toBe(before + 1) // fell back
})

test('every captureEvery messages, the new part of the conversation is read during the session', { options: { captureEvery: 3 } }, async ($, on) => {
  const calls: string[][] = []
  engine(on, calls)
  const writes = { some: (f: (w: string) => boolean) => calls.some(c => c[0] === 'write' && f(c[1] ?? '')),
    filter: (f: (w: string) => boolean) => calls.filter(c => c[0] === 'write' && f(c[1] ?? '')) }
  let users = 0
  on('turn.complete', () => ({ text: 'ok' }))
  on('session.messages', () => ({
    value: Array.from({ length: users }, (_, i) => [{ role: 'user', text: `message ${i}` }, { role: 'assistant', text: 'ok' }]).flat(),
  }))
  await $.session.start({ cwd: 'C:/mods/Campfires', surface: 'terminal', isInteractive: true })
  const turn = async () => {
    users += 1
    await $.turn.complete({ reason: 'answer', answer: 'ok', durationMs: 1, isAborted: false, turnId: `t${users}` } as never)
    await new Promise(done => setTimeout(done, 20)) // the save runs after the turn
  }
  await turn()
  await turn()
  expect(writes.some(w => /[\\/]pending[\\/]/.test(w))).toBe(false)
  await turn() // the third message of the person's
  expect(writes.some(w => /[\\/]pending[\\/]/.test(w))).toBe(true)
  expect(calls.some(c => c.join(' ').includes('capture.py pending'))).toBe(true)
  const queued = writes.filter(w => /[\\/]pending[\\/]/.test(w)).length
  await turn()
  expect(writes.filter(w => /[\\/]pending[\\/]/.test(w)).length).toBe(queued) // not again until three more
})
