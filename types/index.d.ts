// graphcraft-memory's session values, read by the memory pane and the brief band.

/** One fact as the pane shows it; the ids are what Forget and Drop retire. */
export type PaneFact = { source: string; predicate: string; target: string; source_id: string; target_id: string }

/** What recall handed Claude for the latest prompt. */
export type Used = { matched: string[]; more: number; facts: PaneFact[] }

/** graph/pane.py state, minus the facts kept in this session's Keep. */
export type PaneView = {
  project: string
  brief: string
  learned: { sessionId: string; at: number; total: number; facts: PaneFact[]; title: string; titleIsAuto: boolean } | null
  problems: { name: string; fixed: boolean; fixedBy: string }[]
}

/** The pane's search: what was asked and the facts found (memory_search's triples). */
export type Searched = { query: string; facts: PaneFact[]; more: number }

/** The fact whose Fix box is open, and the links the vocabulary allows between its two nodes ("~p": reversed). */
export type Fixing = { key: string; links: { value: string; label: string }[] }

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
      renaming: Shaped<boolean>
      searched: Shaped<Searched | null>
      fixing: Shaped<Fixing | null>
    }
  }
}
