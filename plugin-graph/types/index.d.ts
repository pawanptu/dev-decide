// What the panel last fetched from dev-decide's local hint server (hookd.py): the graph, ready to draw.
export type GraphView = {
  status: 'idle' | 'ok' | 'down'
  days: number
  svg: string
  tree: string[]
  recent: string[]
  summary: string
  root: string
  updated: number
}

declare module 'claude-code' {
  interface PluginState {
    'dev-decide-graph': { view: Shaped<GraphView> }
  }
}
