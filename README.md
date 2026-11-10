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

> Status: milestone 1 of 5 (store + recall + prompt hook). Capture and feeders come next, so the graph is only
> filled by hand for now. See [docs/DESIGN.md](docs/DESIGN.md).

## Try it

Needs Python 3 (standard library only).

    py -3 -I graph/store.py load tests/fixture.json --ref fixture --vocab tests/fixture_vocab.json
    py -3 -I graph/recall.py "the game crashed when I sat down in Campfires"
    py -3 -I tests/test_graph.py

Set `GRAPHCRAFT_DB` to a scratch path first if you don't want the example facts in your real graph.

## Credits

The push-recall design (typed graph in SQLite, recursive walk, prompt-hook injection) follows the ideas published by
Glitch Cat Club in [graph-memory-starter](https://github.com/Glitch-Cat-Club/graph-memory-starter) (MIT). This repo
is an independent implementation.

## License

MIT
