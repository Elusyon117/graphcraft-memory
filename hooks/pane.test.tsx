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
  on('fs.write', () => ({ value: undefined }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('prompt.submit', (_, e) => e)
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
      : argv.includes('tools.py search') ? { facts: 1, text: 'Memory facts linked to it: Campfires --[decided]--> Linked anim layer' }
      : argv.includes('tools.py remember') ? { facts: 1, nodes: 2, retired: 0, dropped: ["edge 'X' (TOOL) 'about' 'Y'"] }
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
