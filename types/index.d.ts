// graphcraft-memory's session values, read by the memory pane and the brief band.

/** One fact as the pane shows it; the ids are what Forget and Drop retire. */
export type PaneFact = { source: string; predicate: string; target: string; source_id: string; target_id: string }

/** What recall handed Claude for the latest prompt. */
export type Used = { matched: string[]; more: number; facts: PaneFact[] }

/** graph/pane.py state, minus the facts kept in this session's Keep. */
export type PaneView = {
  project: string
  brief: string
  learned: { sessionId: string; at: number; total: number; facts: PaneFact[] } | null
  problems: { name: string; fixed: boolean; fixedBy: string }[]
}

/** graph/clawd.py svg: Clawd from the clawd-pet mod's frames, when that mod is installed. */
export type ClawdArt = { svg: string; width: number; height: number }
export type Clawd = { lit: ClawdArt; idle: ClawdArt; source: string }

declare module 'claude-code' {
  interface PluginState {
    'graphcraft-memory': {
      clawd: Shaped<Clawd | null>
      used: Shaped<Used | null>
      view: Shaped<PaneView | null>
      briefShown: Shaped<boolean>
    }
  }
}
