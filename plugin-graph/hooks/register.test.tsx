import { expect, test } from 'claude-code/testing'

const SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"><text>graph</text></svg>'
const TRACE = {
  projects: [],
  project: 'repo-12345678',
  days: 1,
  summary: {
    nodes: [],
    edges: [],
    paths: [],
    stats: { prompts: 3, hinted: 2, skipped: 0, decisions: 1, used: 1, followed: 1, after_hint: 1, avg_hint_ms: 5.2 },
    timeline: [
      {
        kind: 'hint',
        ts: 0,
        task_type: 'bugfix',
        domain: 'auth',
        text: 'the login page crashes with a null point…',
        hint: 'debugger',
        decided_by: 'trained model',
        ms: 5.2,
        steps: [{ step: 'trained model', answer: 'debugger', confidence: 0.9, result: 'suggested', ms: 4.1 }],
        used: [{ name: 'debugger', tool: 'Agent', ms: 2000, followed: true }],
      },
      {
        kind: 'hint',
        ts: 0,
        task_type: 'docs',
        text: 'update the readme with the install steps',
        hint: null,
        decided_by: 'trained model',
        ms: 4.0,
        steps: [{ step: 'trained model', answer: 'none', confidence: 1, result: 'no agent or skill fits', ms: 3.2 }],
        used: [],
        turn: { ms: 95000, tools: { Edit: 3, Read: 2 } },
      },
    ],
  },
  text: ['bugfix · auth  (1)', '  trained model → debugger → used debugger  1× · 5.2 ms'],
}

const PANE = {
  plugin: 'dev-decide-graph',
  component: 'Pane',
  requestId: 'dev-decide-graph',
  props: {
    title: 'Decision graph',
    isFocused: false,
    bodyColumns: 100,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 40 },
    view: {},
  },
} as const

test('/dev-decide-graph opens the panel with the graph and the latest routing, on every surface', async ($, on) => {
  // Nothing sits beneath the plugin: each hook here stands for the engine and dev-decide's hint server.
  const fetched: string[] = []
  on('command.register', () => ({ value: { command: 'dev-decide-graph' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.root', () => ({ value: '/repo' }))
  on('config.list', () => ({ value: [] }))
  on('clock.now', () => ({ value: 1 }))
  on('clock.every', () => ({ value: undefined }))
  on('http.fetch', (_, e) => {
    fetched.push(e.url)
    const text = e.url.includes('/trace?') ? JSON.stringify(TRACE) : SVG
    return { value: { status: 200, ok: true, headers: {}, text } }
  })

  await $.session.start({ cwd: '/repo', surface: 'desktop', isInteractive: true })
  const ran = await $.command.run({ command: 'dev-decide-graph' })
  expect(ran.text).toContain('Decision graph panel opened')
  expect(fetched.some(u => u.startsWith('http://127.0.0.1:47321/trace?root=%2Frepo&days=1'))).toBe(true)

  for (const surface of ['terminal', 'desktop', 'vscode', 'mobile'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Text', text: /3 prompts, 2 hints/ }))?.text).toContain('hint followed 1/1')
    const latest = await ui.find({ type: 'Text', text: /the login page crashes/ })
    expect(latest?.text).toContain('→ debugger')
    expect(latest?.text).toContain('→ debugger ✓ 2.0 s')
    const itself = await ui.find({ type: 'Text', text: /update the readme/ })
    expect(itself?.text).toContain('→ Claude itself (1.6 min, Edit 3, Read 2)')
    if (surface === 'terminal') {
      expect((await ui.find({ type: 'Text', text: /trained model → debugger/ }))?.text).toContain('used debugger')
    }
    await ui.unmount()
  }
})

test('the panel says how to start the hint server when it is not running', async ($, on) => {
  on('command.register', () => ({ value: { command: 'dev-decide-graph' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.root', () => ({ value: '/repo' }))
  on('config.list', () => ({ value: [] }))
  on('clock.every', () => ({ value: undefined }))
  on('http.fetch', () => ({ deny: 'connection refused' }))

  await $.session.start({ cwd: '/repo', surface: 'desktop', isInteractive: true })
  const ran = await $.command.run({ command: 'dev-decide-graph' })
  expect(ran.text).toContain('not running yet')

  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect((await ui.find({ type: 'Text', text: /not answering/ }))?.text).toContain('hookd.py --ensure')
  await ui.unmount()
})
