import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { GraphView } from '../types'

// The decision graph lives in dev-decide's local hint server (hookd.py, started by its SessionStart hook):
// this panel only fetches and draws it. Nothing leaves the machine.
const PANE = 'dev-decide-graph'
const TITLE = 'Decision graph'
const BASE = 'http://127.0.0.1:47321'
const EVERY_MS = 5000
const RECENT = 6
const PERIODS = [
  { days: 1 / 24, label: '1 h' },
  { days: 1, label: '24 h' },
  { days: 7, label: '7 days' },
  { days: 30, label: '30 days' },
]
const EMPTY: GraphView = { status: 'idle', days: 1, svg: '', tree: [], recent: [], summary: '', root: '', updated: 0 }
// Change the tag when GraphView's fields change: it declines what an earlier version stored.
const view = atom({ plugin: 'dev-decide-graph', key: 'view' } as const, EMPTY, { shape: 'view-1' })

let timer: { cancel: () => void } | undefined

type Step = { step: string; answer?: string; confidence?: number; result: string; ms?: number }
type Used = { name: string; tool: string; ms?: number | null; followed?: boolean }
type Item = {
  kind: 'hint' | 'decision' | 'used'
  ts: number
  task_type?: string
  domain?: string
  hint?: string
  decided_by?: string
  steps?: Step[]
  ms?: number
  bundles?: string[]
  questions?: string[]
  gates?: Record<string, string>
  name?: string
  tool?: string
  text?: string
  used?: Used[]
}
type Stats = {
  prompts: number
  hinted: number
  decisions: number
  used: number
  followed: number
  after_hint: number
  avg_hint_ms: number | null
}
type Trace = { summary: { timeline: Item[]; stats: Stats }; text: string[] }

function ms(v: number | null | undefined): string {
  if (v === null || v === undefined) return ''
  if (v >= 60000) return `${(v / 60000).toFixed(1)} min`
  if (v >= 1000) return `${(v / 1000).toFixed(1)} s`
  return v >= 10 ? `${Math.round(v)} ms` : `${v.toFixed(1)} ms`
}

function clock(ts: number): string {
  const d = new Date(ts)
  const two = (n: number) => String(n).padStart(2, '0')
  return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`
}

function line(it: Item): string {
  // the prompt's first characters (as kept by dev-decide's prompt_chars setting) say which request it was
  const said = it.text ? `“${it.text}” ` : ''
  const task = [it.task_type, it.domain].filter(Boolean).join(' · ') || 'unclassified'
  if (it.kind === 'used') {
    const who = it.tool === 'typed' ? 'you typed it' : "Claude's own choice"
    return `${clock(it.ts)}  ${said}used ${it.name} (${who}${it.ms ? `, ran ${ms(it.ms)}` : ''})`
  }
  if (it.kind === 'decision') {
    const what = (it.bundles?.length ? it.bundles : it.questions ?? []).join(',')
    const gates = Object.entries(it.gates ?? {}).map(([g, v]) => ` · gate ${g}: ${v}`).join('')
    return `${clock(it.ts)}  ${said}${task} → /dev-decide ${what}${gates} · ${ms(it.ms)}`
  }
  const last = it.steps?.[it.steps.length - 1]
  const how = last
    ? `${last.step}${last.answer ? ` ${last.answer}` : ''}${last.confidence != null ? ` ${last.confidence.toFixed(2)}` : ''}`
    : it.decided_by ?? ''
  const used = (it.used ?? [])
    .map(u => ` → used ${u.name}${u.followed === true ? ' ✓' : u.followed === false ? ' ✗' : ''}`)
    .join('')
  return `${clock(it.ts)}  ${said}${task} → ${it.hint ?? 'no hint'} (${how}, ${ms(it.ms)})${used}`
}

function summary(s: Stats, days: number): string {
  const period = PERIODS.find(p => p.days === days)?.label ?? `${days} days`
  const followed = s.after_hint ? ` · hint followed ${s.followed}/${s.after_hint}` : ''
  const speed = s.avg_hint_ms != null ? ` · avg hint ${ms(s.avg_hint_ms)}` : ''
  return `Last ${period}: ${s.prompts} prompts, ${s.hinted} hints${speed} · ${s.used} agents/skills used${followed} · ${s.decisions} /dev-decide answers`
}

async function theme($: EngineInterface): Promise<string> {
  try {
    const row = (await $.config.list()).find(r => r.key === 'theme')
    const value = String(row?.value ?? '')
    return value.startsWith('light') ? 'light' : value.startsWith('dark') ? 'dark' : 'auto'
  } catch {
    return 'auto'
  }
}

// Fetches the graph for one project and period; the hooks store it (read and update stay in the hooks).
async function fetchGraph($: EngineInterface, root: string, days: number): Promise<Partial<GraphView>> {
  const q = `root=${encodeURIComponent(root)}&days=${days}`
  try {
    const [t, g] = await Promise.all([
      $.http.fetch(`${BASE}/trace?${q}`),
      $.http.fetch(`${BASE}/graph.svg?${q}&theme=${await theme($)}&width=760&header=1&anim=1`),
    ])
    if (!t.ok || !g.ok) return { status: 'down', root }
    const data = JSON.parse(t.text) as Trace
    return {
      status: 'ok',
      root,
      svg: g.text.length <= 131072 ? g.text : '',
      tree: data.text,
      recent: data.summary.timeline.slice(0, RECENT).map(line),
      summary: summary(data.summary.stats, days),
      updated: await $.clock.now(),
    }
  } catch {
    return { status: 'down', root }
  }
}

async function openPage($: EngineInterface, root: string): Promise<void> {
  const url = `${BASE}/graph?root=${encodeURIComponent(root)}`
  for (const argv of [['cmd', '/c', 'start', '', url], ['open', url], ['xdg-open', url]]) {
    try {
      if ((await $.process.run(argv, { timeoutMs: 5000 })).exitCode === 0) return
    } catch {
      // not this platform's opener: try the next
    }
  }
  $.ui.toast(`Open ${url} in a browser`)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'dev-decide-graph',
      description: 'Show the decision graph: which agent or skill was suggested for which task, what Claude used, and timings',
    })
    return next(e)
  })

  // Opened on request only; it refreshes every few seconds while it is open.
  on('command.run', { command: 'dev-decide-graph' }, async $ => {
    const refresh = async () => {
      const v = await read($, view)
      const got = await fetchGraph($, v.root || (await $.session.root()), v.days)
      await update($, view, x => ({ ...x, ...got }))
    }
    await refresh()
    await $.ui.open({ id: PANE, title: TITLE })
    timer?.cancel()
    timer = $.clock.every(EVERY_MS, () => void refresh())
    const v = await read($, view)
    return {
      text:
        v.status === 'ok'
          ? `Decision graph panel opened. ${v.summary}. Full page: ${BASE}/graph`
          : 'Decision graph panel opened, but the dev-decide hint server is not running yet: it starts with each Claude Code session.',
    }
  })

  on('ui.close', async ($, e, next) => {
    if (e.id === PANE) {
      timer?.cancel()
      timer = undefined
    }
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const ui = $.ui.resolve(e)
    const { Box, Text, Button, Link } = ui
    const v = await read($, view)
    const url = `${BASE}/graph${v.root ? `?root=${encodeURIComponent(v.root)}` : ''}`
    const refresh = async (days = v.days) => {
      const got = await fetchGraph($, v.root || (await $.session.root()), days)
      await update($, view, x => ({ ...x, ...got, days }))
    }
    const setDays = (days: number) => () => void refresh(days)
    let body
    if (v.status === 'down') {
      body = (
        <Text dimColor>
          The dev-decide hint server is not answering on {BASE}. It starts with each Claude Code session (the
          plugin's SessionStart hook); start it now with: python hookd.py --ensure
        </Text>
      )
    } else if (v.status === 'idle') {
      body = <Text dimColor>Loading…</Text>
    } else if (e.surface !== 'terminal' && 'Svg' in ui && v.svg) {  // the terminal draws no SVG: a text tree there
      const { Svg } = ui
      body = (
        <Svg
          source={v.svg}
          alt={`Decision graph. ${v.summary}. ${v.tree.join('; ')}`}
          isInteractive
        />
      )
    } else {
      body = (
        <Box flexDirection="column">
          {v.tree.map(row => (
            <Text wrap="truncate-end">{row}</Text>
          ))}
        </Box>
      )
    }
    return (
      <Box flexDirection="column" gap={1}>
        <Box flexDirection="row" gap={1} flexWrap="wrap">
          {PERIODS.map(p => (
            <Button
              key={`days-${p.label}`}
              label={p.label}
              variant={v.days === p.days ? 'primary' : 'secondary'}
              onPress={setDays(p.days)}
            />
          ))}
          <Button key="refresh" label="Refresh" onPress={() => void refresh()} />
          <Button key="open" label="Open full page" onPress={() => void openPage($, v.root)} />
        </Box>
        {v.summary ? <Text dimColor wrap="wrap">{v.summary}</Text> : null}
        {body}
        {v.recent.length > 0 ? (
          <Box flexDirection="column">
            <Text bold>Recent requests</Text>
            {v.recent.map(row => (
              <Text wrap="truncate-end" dimColor>
                {row}
              </Text>
            ))}
          </Box>
        ) : null}
        {v.updated ? (
          <Text dimColor>
            Updated {clock(v.updated)} · refreshes every {EVERY_MS / 1000} s while open · full page:
          </Text>
        ) : null}
        <Link href={url} label={url} />
      </Box>
    )
  })
}
