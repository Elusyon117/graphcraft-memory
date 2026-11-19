# graphcraft-memory

Knowledge-graph memory for Claude Code.

Claude forgets between sessions, and searching files to remember is slow and easy to get wrong. graphcraft-memory
keeps what you work on as a graph of connected facts: things (projects, tools, files, decisions, problems) and typed
links between them. Before every prompt, code looks up the things you mentioned, follows their links a few steps and
hands Claude the result. Claude never has to search for it.

- **Install once per machine.** One graph for all your projects, stored in `~/.claude/graphcraft-memory/`, which the desktop app and
  terminal Claude Code share.
- **Grows by itself.** At the end of each session the facts worth keeping are extracted and linked in.
- **Mods plug in.** Other mods can feed it their own facts. With [zonecraft](https://github.com/Elusyon117/zonecraft)
  installed, its conflicts, crash families, knowledge and mod logs join the same graph.

> Status: milestones 1 and 2 of 5 (recall and capture). Feeders for other mods come next.
> See [docs/DESIGN.md](docs/DESIGN.md).

## Install

You need **Python 3** on the PC (python.org, with "Add to PATH" ticked) and access to this repository on GitHub
(it is private: the owner invites you, and Git on your PC must be signed in to GitHub, for example through GitHub
Desktop).

In a Claude Code terminal session, type:

```
/plugin install graphcraft-memory --marketplace Elusyon117/graphcraft-memory
```

Answer `y` to add the marketplace, then pick the **user** scope so it loads in every session. From a shell instead:

```bash
claude plugin install graphcraft-memory --marketplace Elusyon117/graphcraft-memory
```

**Desktop app:** click **+** next to the prompt box → **Plugins** → **Add marketplace** → `Elusyon117/graphcraft-memory`
→ **Add plugin**.

Update later with `claude plugin marketplace update elusyon-graphcraft-memory`, then
`claude plugin update graphcraft-memory@elusyon-graphcraft-memory`.

## How it works for you

Nothing to run once installed:

1. **Every prompt:** the facts linked to what you mention ride along to Claude. The status line shows
   `memory: N facts` when there were any.
2. **When a session ends or compacts:** the conversation (your words and Claude's, no tool output) is queued.
3. **At your next session start:** a small model (Haiku by default) turns it into facts in the background, and a
   toast says how many it learned.

Commands: `/graph-memory` shows what is remembered and recent captures; `/graph-memory-undo` takes the last session's
facts back out (or `/graph-memory-undo <session id>`); `/graph-view` opens an offline map of the whole graph in your
browser (structured or free layout, a "Why chains" filter, click a node for its facts). The **Memory pane** opens beside
the chat: the facts used for your last prompt (Forget), what the last session taught it (Keep, Drop, Undo session) and
this project's problems; close it and `/graph-pane` brings it back. With the clawd-pet mod installed, the pane's Clawd
uses that mod's animations (read from your install, never copied here); without it, a small Clawd of its own. A one-line brief of the project sits above the prompt
until your first message. Settings: `capture` (on/off) and `extractModel`
(`haiku`/`sonnet`).

Needs Python 3 (standard library only).

## Develop

    py -3 -I tests/test_graph.py
    py -3 -I tests/test_capture.py
    claude --plugin-dir .            (a session with the mod loaded from this folder)

Set `GRAPHCRAFT_DB` to a scratch path to keep test facts out of your real graph.

## Credits

The push-recall design (typed graph in SQLite, recursive walk, prompt-hook injection) follows the ideas published by
Glitch Cat Club in [graph-memory-starter](https://github.com/Glitch-Cat-Club/graph-memory-starter) (MIT). This repo
is an independent implementation.

## License

MIT
