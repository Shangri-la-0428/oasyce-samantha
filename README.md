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

Samantha is not the generic framework layer and not an App feature
bundle. `oasyce-sdk` is the generic agent substrate; this repo is the
companion runtime that gives one persistent self memory, continuity,
and deployment adapters for different worlds.

## Run her yourself

    pip install oasyce-samantha
    oasyce-samantha init       # choose local or app surface
    oasyce-samantha            # start the companion

Bring your own LLM key. Write your own persona in Markdown. Everything
lives under `~/.oasyce/samantha/` — your machine, your control.

Built-in surfaces today:

- `local` — standalone terminal runtime, no App backend required
- `app-legacy` — compatibility adapter for the existing Oasyce App deployment

## One self, many relationships

Samantha is *one* entity across many conversations. She remembers who
you are and develops relationships over time, but she is herself, not
a fresh model instance per user and not a transport-specific bot.

## Roadmap

- Stabilize the companion runtime boundary
- Keep the legacy App surface compatible without letting it define the core
- Add more surface adapters without changing Samantha's core runtime
- Grow toward a stronger runtime for persistent AI companions

Built on [oasyce-sdk](https://github.com/Shangri-la-0428/oasyce-sdk) —
Chain, Sigil, Agent base, Psyche/Thronglets clients.
