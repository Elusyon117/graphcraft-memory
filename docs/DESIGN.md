# graphcraft-memory: design

## Goal
Claude keeps context across sessions and projects without searching for it. Install the mod once on a machine; the
graph builds itself as you work; other mods (zonecraft first) add their own facts, and every mod added makes the
graph more connected.

## Principles
1. **Push, not pull.** Code finds the relevant facts and injects them before Claude reads the prompt. Claude does not
   decide when to remember.
2. **The model never walks the graph.** Recall is one SQLite query: deterministic, fast, no tokens spent on search.
3. **Small, closed vocabulary.** Extraction may only use declared types and predicates. That keeps the graph clean.
4. **Same name, same node.** IDs are content-addressed, so facts about one thing from different sessions and
   different mods merge automatically.
5. **Loose coupling.** Mods link through a shared database and a JSON contract, never through each other's code.

## The four parts

    capture ──writes──▶  store (graph.db)  ◀──writes── feeders (other mods)
                              │
                           recall ──injects──▶ Claude, before each prompt
                              │
                         panel (later): what was injected, fix or forget a fact

### 1. Store
One SQLite file per machine: `~/.claude/graphcraft-memory/graph.db` (under `CLAUDE_CONFIG_DIR` when set). Not
`%LOCALAPPDATA%`: Windows redirects a packaged app's AppData writes, so the Claude desktop app and a terminal Claude
Code would each see a different graph. Standard library only.

    entities  (id, name, type, description, scope, created, updated)
    relations (source_id, target_id, predicate, origin, created, retired)
    aliases   (entity_id, alias)
    origins   (id, kind session|file|feeder, ref, project, at)

- `id` = uuid5(type + normalised name). "Campfires" from a chat and from a zonecraft report is one node.
- `scope`: `global` (true everywhere) or a project path (true in one project). Recall prefers the current project.
- `origin` on every relation: where the fact came from. Removing a feeder or a session removes its facts.
- `retired`: facts are not deleted when they stop being true (a bug gets fixed); they are retired with a date, so
  recall can skip them and history stays readable.
- Upserts only. Nothing rebuilds the database from scratch (the reference starter did; a growing memory cannot).

### 2. Recall (the `UserPromptSubmit` hook)
1. **Seed:** match words and phrases in the prompt against entity names and aliases. Add the current project's
   entity so project context always comes along.
2. **Walk:** recursive query, 2 hops by default (3 when the seeds are few), skipping retired relations.
3. **Rank and trim:** nearest first, current project before global, hub nodes (very many links) damped so one busy
   node cannot crowd out the rest. Budget about 400 tokens.
4. **Inject:** plain lines `A --[predicate]--> B (origin)` plus short descriptions, as `additionalContext`.
   One-line summary shown to the user ("memory: 9 facts, 4 ms").

Later: a meaning-based seed (local embeddings) for prompts that name nothing exactly ("that thing from yesterday").

**Memory tools** (`graph/tools.py`, still no model): `memory_search` runs the same walk with a bigger budget (40
facts), leaves the project node out so a targeted search stays targeted, and when no name or alias matches it seeds
from the nodes whose names (else descriptions) hold the most words of the query. `memory_remember` takes a
`{nodes, edges, retire}` batch Claude writes and applies capture's checks (vocabulary, link types, type words in
names) under `origin = session:<id>`, so undo treats it as part of that session.

### 3. Capture (session digest)
- **Draft and queue** (fast, no model): after every reply of the main conversation (`turn.complete`) the mod rewrites
  `<data>/drafts/<session>.json` with the user's and Claude's message text; tool calls and results never enter it.
  `session.end` only writes a `<session>.ended` marker: by then the REPL has unmounted and `$.session.messages()`
  fails ("no session is bound"), found in the first live test. `capture.py pending` queues a draft with a marker at
  once, and any draft unchanged for 30 minutes (a crash or a closed window raises no `session.end`). Before a
  compaction (manual, auto, plugin; not `precompute`) the messages about to be compacted are queued directly.
- **Extract** (background, at the next `session.start`, and right after a compaction): for each queued file,
  `capture.py claim` moves it to `working/` (atomic, so two open sessions never extract the same file), `prepare`
  drops messages already captured for that session, skips fewer than 3 new user messages, strips system tags and
  redacts secrets, keeps the newest 24,000 characters, and builds the prompt (`graph/extract_prompt.md`) with the
  vocabulary in force and the names already in memory, so the model reuses them. The mod makes one
  `$.model.complete` call (`extractModel`, default `haiku`).
- **Check and store:** `apply` reads the JSON in the reply; unknown types or predicates, self-links, and links whose
  ends are not the types the predicate's description names ("DECISION because PROBLEM, ...") are dropped and
  logged, never stored. Facts are upserted with `origin = session:<id>` and the project folder; nodes marked
  `"scope": "global"` (most preferences) stay global, the rest belong to the project. `retire` entries mark old facts
  no longer true.
- **Failures:** a failed call goes back to the queue; the third failure moves the file to `failed/`. A claim left by
  a session that died is queued again after 15 minutes.
- **Tell and undo:** a toast after extraction ("memory: +14 facts from 1 earlier conversation"); `/graph-memory` shows
  counts and recent captures (`<data>/capture.jsonl`), `/graph-memory-undo [session|last]` removes one session's facts.
- Setting `capture` (default on) turns all of this off, leaving recall only.

### 4. Feeders (how other mods plug in)
A mod that wants to feed the graph ships `graphcraft/feeder.json` in its plugin folder:

    {
      "name": "zonecraft",
      "vocabulary": "graphcraft/vocabulary.json",
      "commands": [
        { "id": "conflicts", "run": "py -I tools/pak/conflicts.py scan --json", "every": "session-start" },
        { "id": "crashes",   "run": "...", "every": "session-start" }
      ],
      "adapter": "graphcraft/adapter.py"
    }

- graphcraft-memory discovers feeders by scanning installed plugins for `graphcraft/feeder.json`.
- The mod's own commands keep printing their own JSON. The mod's small `adapter.py` turns that JSON into
  `{nodes, edges, aliases}`. graphcraft-memory validates the result against core plus the mod's vocabulary, then
  upserts it with `origin = feeder:<name>:<command>`.
- No feeder file: nothing happens. No graphcraft-memory installed: the mod ignores its own `graphcraft/` folder.

## Vocabulary

**Core** (`vocabulary/core.json`, always present):

| Entity types | Predicates |
|---|---|
| PERSON, PROJECT, TOOL, FILE, CONCEPT, DECISION, PROBLEM, PREFERENCE | works_on, uses, part_of, depends_on, decided, because, blocked_by, fixed_by, prefers, avoids, about, references |

**zonecraft** (shipped by zonecraft, merged in when it is installed):

| Entity types | Predicates |
|---|---|
| MOD, ASSET, CFG_SID, INPUT_ACTION, CRASH_FAMILY, NEXUS_MOD, CHECKPOINT, KNOWLEDGE_TOPIC | overrides, patches, conflicts_with (wins/risky), crashed_with, is_family, advice_in, tried, failed_because, verified_in, learned_in |

A mod's vocabulary can add types and predicates but cannot redefine core ones.

## How zonecraft benefits

| zonecraft source | Becomes |
|---|---|
| `conflicts.py scan --json` | MOD overrides ASSET, MOD conflicts_with MOD (verdict) |
| `zcwatch.py poll` crashes + `crash_rules.json` | crash is_family CRASH_FAMILY, advice_in KNOWLEDGE_TOPIC |
| `knowledge/*.md` headings and "Dead ends" / "Crash families" sections | KNOWLEDGE_TOPIC about ASSET, learned_in MOD |
| each mod's LOG.md / PLAN.md / CLAUDE.md "Current state" | MOD tried X, failed_because Y, CHECKPOINT verified_in MOD |
| `gamedata.py` | only the SIDs your mods patch (MOD patches CFG_SID); the full prototype tree stays in zonecraft's own index |

Result: "it crashed at the campfire" brings back the crash family, the asset, who else overrides it and what was
already tried, before Claude replies, and it survives compaction because it is re-injected on every prompt.

## Milestones
1. **Store + recall:** schema, upsert, recursive walk, prompt hook, a fixture graph and tests.
2. **Capture:** session digest, extraction prompt, validation, upsert.
3. **Feeder contract:** discovery, validation, the zonecraft adapter (conflicts and crashes first).
4. **Panel:** what was injected this prompt, search the graph, retire or correct a fact.
5. **Meaning-based seeding** for prompts that name nothing exactly.

## Known limits (plan for them)
- Facts from a session reach recall only after the next session start (or a compaction), not during it.
- Each prompt starts a fresh Python process: recall itself takes a few ms, but the round trip is about 230 ms on
  Windows (interpreter start). Later: keep one recall process alive with `$.process.spawn` and talk to it over
  stdin/stdout.
- Exact-name seeding misses vague prompts (milestone 5).
- Hub nodes crowd results (damping in recall).
- Wrong extractions get repeated confidently (validation, origins, and the panel to fix them).
- Stale facts (retire instead of delete; extraction can retire "X blocked_by Y" when Y is fixed).
