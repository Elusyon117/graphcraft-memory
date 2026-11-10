# graphcraft-memory: working on this repo

A Claude Code mod (plugin) that gives Claude knowledge-graph memory. Design: [docs/DESIGN.md](docs/DESIGN.md).
Read it before changing the schema, the vocabulary or the feeder contract.

## Layout (planned)
- `.claude-plugin/`: plugin and marketplace manifests.
- `hooks/`: the mod's TypeScript modules (`hooks.json` lists them), same style as rich-blocks and zonecraft.
- `graph/`: Python, standard library only: `store.py` (schema, upserts), `recall.py` (seed + walk), `capture.py`
  (session digest → extraction → upsert), `feeders.py` (discover and run other mods' feeders).
- `vocabulary/core.json`: the closed list of entity types and predicates.
- `tests/`: deterministic tests on a small fixture graph.

## Rules
1. **Recall is code, not a model.** Nothing in the prompt hook calls a model; it must stay fast (target < 50 ms).
2. **User data never enters the repo.** The graph lives in `~/.claude/graphcraft-memory/`. Never under
   `%LOCALAPPDATA%`: Windows redirects the desktop app's AppData, so it and a terminal session would split memory.
3. **Closed vocabulary.** New types or predicates are added to a vocabulary file on purpose, never invented by
   extraction. Facts outside the vocabulary are dropped and logged.
4. **Other mods are optional.** graphcraft-memory must work with no feeders, and a mod must work without
   graphcraft-memory. Never import another mod's code.
5. **Every fact records where it came from** (session, file or feeder), so it can be traced and removed.
