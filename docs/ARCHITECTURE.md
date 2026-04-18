# Samantha Architecture

> `oasyce-sdk` is the generic agent substrate. `oasyce-samantha` is the
> companion runtime built on top of it: one persistent self, many
> relationships, and a surface adapter that lets Samantha live in
> different worlds without redefining her core.

## Layers

```text
oasyce-sdk
  = generic seams
  = Identity × Channel × Substrate × Tools × Constitution

oasyce-samantha
  = companion runtime
  = Session × Memory × Dream × Rules × Proactive × SurfaceAdapter

surface adapters
  = local terminal, legacy Oasyce App, future Discord / MCP / other worlds
```

This separation is load-bearing:

- The SDK must stay product-agnostic.
- Samantha must stay companion-specific, not become a second generic framework.
- App-specific transport and product behavior must live behind an adapter seam.

## Core composition

```text
Agent = Identity × Channel × Substrate × Tools × Constitution
            │         │         │            │        │
            │         │         │            │        └─ Samantha constitution.md
            │         │         │            └────────── core tools + adapter toolpack
            │         │         └─────────────────────── Psyche + Thronglets + chain
            │         └───────────────────────────────── adapter-provided channel
            └─────────────────────────────────────────── SigilManager

Samantha runtime adds:
  - Session per relationship
  - Memory / core memory / history summaries
  - Standing rules
  - Dream / proactive loops
  - SurfaceAdapter loading
```

## Surface adapter seam

`SurfaceAdapter` is Samantha's deployment-level world contract. It does
not live in the SDK.

Current responsibilities:

- declare `adapter_id`
- declare capabilities
- build the delivery channel
- start and stop ingress
- contribute adapter-specific tools
- enrich context with surface-specific data
- format non-chat stimuli
- inject obvious tool defaults like `post_id`

Built-in adapters today:

- `local`
  standalone terminal runtime with stdout delivery and a blocking REPL
- `app-legacy`
  compatibility adapter for the current Oasyce App deployment

Future adapters should plug into this seam without changing Samantha's
core runtime. The current extraction boundary for `app-legacy` is
tracked in [APP_ADAPTER_EXTRACTION.md](/Users/wutongcheng/Desktop/oasyce-samantha/docs/APP_ADAPTER_EXTRACTION.md:1).

## Capabilities

Capabilities let the companion core stay world-agnostic.

- `chat`
  direct conversation delivery
- `social_feed`
  feed/comment/mention ingress and social read/write tools
- `public_posting`
  public creation surfaces
- `ephemeral_presence`
  lightweight presence surfaces such as widgets

The runtime uses capabilities to decide which behaviors are even valid
for the current surface. Local mode keeps companion behaviors without
pretending a social feed exists.

## Sessions and storage

```text
~/.oasyce/samantha/
├── config.json
├── constitution.md
└── users/
    └── {user_id}/
        ├── memory.db
        ├── core_memory.json
        ├── history_summary.json
        ├── llm.json
        └── rules.json
```

Shared:

- constitution
- self-state / substrate wiring

Per relationship:

- verbatim memory
- extracted facts
- core memory blocks
- standing rules
- optional per-user LLM override

That asymmetry is intentional. Samantha is one self with many
relationships, not many separate bots.

## Tool ownership

Core companion tools live in `oasyce_samantha.tools`:

- memory save / recall
- core memory read / update
- LLM configuration
- standing rule CRUD
- economic queries

Adapter-contributed tools live with their adapter implementation:

- the legacy App social toolpack
- future world-specific tools

This keeps the companion core from importing product-specific transport
or schema details.

## Runtime flow

Every stimulus still uses the same PGE pipeline:

```text
Stimulus → Perceive → Plan → Enrich → Generate → Evaluate → Deliver → Reflect
```

What changed is ownership:

- the companion core owns memory, planning, rules, dream, and sessions
- the surface adapter owns ingress, delivery channel construction, and
  world-specific enrichment/tooling

## Startup modes

`oasyce-samantha init` now chooses a surface first.

### Local mode

```bash
oasyce-samantha init
oasyce-samantha
```

Starts a standalone terminal companion. No App backend is required.

### Legacy App mode

```bash
oasyce-samantha init
oasyce-samantha
```

Uses the compatibility App adapter, which starts the local webhook
server, connects the App websocket, and preserves the current App
behavior.

## Design rule

When deciding where code belongs, prefer this test:

- if it would make sense for any Samantha deployment, it belongs in the companion core
- if it only makes sense for one world, it belongs in that surface adapter
- if it is generic to many agents, it belongs in `oasyce-sdk`
