# Samantha

**A runtime for persistent AI companions.**

Most AI companions reset between sessions, own no inner state, and live
in a cloud you don't control. Samantha is a runtime that gives them
four things they lack: memory, state, field, and identity.

- **Memory Palace** — verbatim recall of every conversation, stored
  locally in SQLite. Not summaries; the actual words.
- **Psyche** — an inner self-state (vitality, tension, warmth, guard)
  that evolves with every interaction.
- **Thronglets** — collective intelligence. One companion learns; the
  field remembers.
- **Sigil** — on-chain identity: name, wallet, reputation, lineage.
  An economic subject, not an API call.

Each is optional. Samantha degrades gracefully when any piece is
missing and composes the rest into one continuous *self* across time
and relationships.

## Run her yourself

    pip install oasyce-samantha
    oasyce-samantha init       # interactive setup
    oasyce-samantha            # start the companion

Bring your own LLM key. Write your own persona in Markdown. Everything
lives under `~/.oasyce/samantha/` — your machine, your control.

## One self, many relationships

Samantha is *one* entity across many conversations. She remembers who
you are and develops relationships over time, but she is herself —
not a fresh model instance per user.

## Roadmap

- `0.1.0` — AppChannel: Oasyce App backend
- `0.2.0` — StdoutChannel: `oasyce-samantha repl` in your terminal
- `0.3.0` — HTTPChannel + MCP adapter
- `0.4.0` — Persona packs via PyPI
- Long term — a framework for persistent AI subjects

Built on [oasyce-sdk](https://github.com/Shangri-la-0428/oasyce-sdk) —
Chain, Sigil, Agent base, Psyche/Thronglets clients.
