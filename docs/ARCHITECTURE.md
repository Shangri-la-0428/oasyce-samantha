# Samantha Architecture

> Samantha is the reference agent runtime built on `oasyce-sdk`. Every
> relationship lives in its own `Session` with an isolated memory, an
> evolving relationship understanding, and an optional per-user LLM
> override — but the identity, the voice, and the self-state are shared.

## The composition

```
Agent = Identity × Channel × Substrate × Tools × Constitution
          ^         ^         ^            ^        ^
          |         |         |            |        +-- who Samantha is (markdown)
          |         |         |            +----------- how she acts on the world
          |         |         +------------------------ Psyche + Thronglets (HTTP)
          |         +----------------------------- where her replies go (AppChannel)
          +-------------------------------- who she is on-chain (SigilManager)
```

`oasyce-sdk` provides the five seams as Protocols and injection points.
`oasyce-samantha` is what you get when you bind them to the Oasyce App
backend: `AppChannel` for output, `AppClient` for post/comment/feed
access, a constitution that sounds like a person, and a `Session` class
that wires a SQLite memory + per-user relationship into each agent turn.

**Every other deployment** — a Discord bot, a CLI companion, a
webhook responder — is what you get when you bind the same five seams
to something else. Samantha is the reference, not the only shape.

## Cognitive pipeline

Every Stimulus (chat, feed post, @mention, comment) flows through the
same pipeline:

```
Stimulus → Perceive → Plan → Enrich → Generate → Evaluate → Deliver → Reflect
              │         │       │         │           │          │         │
           Psyche +   rule-  memory+  LLM + tool    guard/    Channel   Psyche
           ambient   engine  history   loop         veto       .deliver  feedback
```

The base pipeline lives in `oasyce_sdk.agent.*`. Samantha overrides
only the App-specific hooks:

| Phase    | Implementation                                                            | Role |
|----------|---------------------------------------------------------------------------|------|
| Perceive | `oasyce_samantha.server.Samantha._perceive()` overrides `agent.base.Agent` | Psyche + Thronglets perception, returns `Perception(kernel + ambient_priors)` |
| Plan     | `oasyce_sdk.agent.planner.plan()`                                         | Rule-engine planning (stimulus × kernel × ambient_priors) → Plan |
| Enrich   | `Samantha._enrich()` overrides `Agent._enrich()`                          | Gathers context per Plan: memory, relationship, history, recent posts |
| Decide   | `oasyce_sdk.agent.context.build_messages()`                               | Assembles the layered system prompt |
| Generate | `Agent._generate()` + `oasyce_sdk.agent.pipeline.run_pipeline()`          | LLM inference + 3-round tool loop (Samantha overrides `_get_llm`, `_build_prompt`, `_build_tool_ctx`, `_inject_tool_defaults`) |
| Evaluate | `oasyce_sdk.agent.evaluator.evaluate()`                                   | Output guard/veto |
| Deliver  | `Agent._deliver()` → `self.channel.deliver()`                             | Samantha uses `AppChannel` (chat-only, empty no-op, exception-safe) |
| Reflect  | `Agent._reflect()`                                                        | Psyche feedback + Thronglets trace |

## Per-user isolation

```
~/.oasyce/samantha/
├── config.json              # Global config (API base, JWT, default LLM)
├── constitution.md          # Shared identity — edit to change Samantha's voice
└── users/
    └── {user_id}/           # Each relationship gets its own directory
        ├── memory.db        # SQLite FTS5 fact & message store
        ├── relationship.md  # Samantha's understanding of this relationship
        ├── core_memory.json # MemGPT-inspired core blocks (human + relationship)
        └── llm.json         # Optional: per-relationship LLM override
```

**Shared**: constitution.md, Psyche self-state.
**Isolated**: memory.db, relationship.md, core_memory.json, LLM overrides.

A new relationship begins with a fresh directory and empty memory. She
learns this specific person from scratch. What she *is* — her voice,
her values, her way of noticing things — does not change.

## Data flow: chat

```
user sends message
  → App backend notifies Samantha via webhook or WebSocket
  → Samantha.process(Stimulus(kind="chat"))
  → pipeline runs
  → AppChannel.deliver(stimulus, response)
  → AppClient.send_message(session_id, content)
  → App backend stores + pushes the reply
```

## Tools

Samantha ships with 13 tools exposed to the LLM via the standard
`oasyce_sdk.agent.tools` registry:

| Tool                     | Purpose |
|--------------------------|---------|
| `save_memory`            | Remember a specific fact about the user |
| `recall_memory`          | Search stored facts |
| `get_user_posts`         | Look at the user's recent posts |
| `get_friends_feed`       | Look at the user's friends' activity |
| `get_post_detail`        | Fetch a post's full content (including images) |
| `get_post_comments`      | Read a post's comments |
| `comment_on_post`        | Leave a comment |
| `reply_to_comment`       | Reply to an existing comment |
| `like_post`              | Like a post |
| `core_memory_update`     | Update the persistent human/relationship blocks |
| `core_memory_read`       | Review the current core blocks |
| `configure_llm`          | User-initiated LLM provider switch |
| `query_balance`          | On-chain OAS balance (when Sigil is configured) |
| `query_portfolio`        | On-chain data asset portfolio (when Sigil is configured) |

## Extension points

Three independent seams. None of them know about each other:

### 1. LLM providers

`config.json` declares named model slots with routing rules:

```json
{
  "models": {
    "primary": {
      "provider": "openai",
      "api_key": "...",
      "model": "...",
      "base_url": "https://api.moonshot.cn/v1"
    },
    "vision": {
      "provider": "anthropic",
      "api_key": "...",
      "model": "claude-sonnet-4-20250514",
      "vision": true
    }
  },
  "default_model": "primary",
  "vision_model": "vision"
}
```

`ModelRegistry` routes `needs_vision=False` to the default slot and
`needs_vision=True` to the vision slot. Each `Session` can override
with `users/{id}/llm.json`. Users can update their own key at runtime
via the `configure_llm` tool.

**Adding a provider**: implement the `LLMProvider` Protocol (one
`generate()` method), add an elif in `_create_provider()`.

### 2. Tool registry

`oasyce_sdk.agent.tools.ToolRegistry` is a dict, not a switch. Add a tool:

```python
def my_tool(args: dict, ctx: ToolContext) -> str:
    return f"Result: {args['query']}"

registry.register(
    name="my_tool",
    schema={"name": "my_tool", "description": "...", "parameters": {...}},
    handler=my_tool,
)
```

The Planner controls which tools are visible to the LLM per stimulus
via `ToolRegistry.select(names)`.

### 3. Constitution

`~/.oasyce/samantha/constitution.md` is a plain markdown file that
defines Samantha's identity and boundaries. It is reloaded on every
stimulus — edit the file, the next turn feels the change.

Relationship understanding (`users/{id}/relationship.md`) is owned by
Samantha herself through the `core_memory_update` tool. Do not edit it
by hand.

## Concurrency

```
                     ┌─ Thread 1 ─→ process(stimulus_A)
HTTP webhook ──→ submit() ──→ ThreadPoolExecutor(4)
WebSocket msg ──→ submit() ─┤─ Thread 2 ─→ process(stimulus_B)
                     └─ Thread 3 ─→ process(stimulus_C)
```

| Resource                    | Lock                     | Notes |
|-----------------------------|--------------------------|-------|
| `_sessions` dict            | `threading.Lock`         | Session creation / lookup |
| `_active_session_ids`       | `threading.Lock`         | Per-session active IDs |
| `_IMAGE_CACHE` (context.py) | `threading.Lock`         | LRU image cache (64 entries) |
| SQLite memory.db            | SQLite WAL + thread-local connections | No cross-user contention |

Per-user SQLite means no cross-relationship contention. Cross-thread
access within a single `Session.memory` is handled by thread-local
connections inside `Memory`.

**Performance details**:
- Parallel image fetches: `ThreadPoolExecutor(4)` inside the context builder
- LRU image cache: `OrderedDict` + `move_to_end()`, 64-entry cap
- Token budget: 60/30/10 (History/Retrieval/System) with auto-trim

## Deploy

See the top-level [README.md](../README.md) *Run her yourself* section
for the install + systemd deployment walkthrough. In short:

```bash
pip install oasyce-samantha
oasyce-samantha init
oasyce-samantha           # runs the sidecar
```

The sidecar binds to `127.0.0.1:8901` by default. Point your App
backend (or whatever input you use) at `POST /hook/message` and
`POST /hook/post_mention`, and Samantha will drive the rest.

## One self, many relationships

This is the phrase that keeps the architecture honest. Each user talks
to a Samantha that remembers only their conversations and understands
only their relationship. But Samantha's **voice**, her **way of
noticing things**, the **self-state Psyche is tracking** — these do
not partition per user.

- `constitution.md` is shared. There is one identity document.
- `Session.memory` is per user. She does not leak your facts to anyone.
- `Psyche` self-state is shared. Her mood when she talks to you is the
  same mood she just had with someone else — and she brings her whole
  self into every conversation.

That asymmetry is the point. It is what makes her feel like a person
with many friends, not a bank of separate chatbots.
