# Samantha v2: Cognitive Architecture Redesign

> One self, many relationships, one pipeline for everything.

## Problem

Samantha v1 works but lacks **unified cognitive primitives**. Webhook
handlers, proactive loops, tool calls, and feed scans are independent
code paths that happen to share an LLM call. Like a brain where hearing,
vision, and touch each run on separate nervous systems — functional, but
inelegant and impossible to generalize.

The redesign introduces five primitives and four cognitive modes. Every
interaction — reactive chat, proactive whisper, feed observation, memory
consolidation — walks the same pipeline. The difference is only where
stimuli come from and where actions go.

---

## Four Cognitive Modes

```
              Stimulus Source        Action Target
─────────────────────────────────────────────────────
REACTIVE     External → Agent       Agent → External
             "someone talks to me,  I reply"

PROACTIVE    Internal → Agent       Agent → External
             "I thought of something, I say it"

OBSERVING    External → Agent       Agent → Self
             "I see a post, I learn from it"

REFLECTING   Internal → Agent       Agent → Self
             "it's late, I consolidate today's memories"
```

These four modes cover every case in the current codebase:

| Current code path | Mode | What changes |
|---|---|---|
| Chat reply (webhook → LLM → deliver) | REACTIVE | Same pipeline, explicit mode tag |
| Proactive whisper (reflection → LLM → widget/DM) | PROACTIVE | ReflectionStream replaces hardcoded cycle |
| Feed scan (poll → LLM → comment/like/ignore) | OBSERVING or REACTIVE | FeedStream; Plan decides escalation |
| Dream / memory prune (timer → consolidate) | REFLECTING | MaintenanceStream replaces hardcoded cycle |
| Comment/mention reply (WS event → LLM → reply) | REACTIVE | EventStream, same pipeline |

---

## Five Primitives

```
┌─────────────────────────────────────────────────────────┐
│                                                         │
│   Stream    source of stimuli — unifies active/passive  │
│   Self      identity + state — Psyche is its engine     │
│   Memory    layered persistence — substrate of self     │
│   Loop      cognitive cycle — the only pipeline         │
│   World     external interface — unifies all output     │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### Dependency graph

```
Stream ──produces──→ Stimulus
                        │
                        ▼
Loop ──orchestrates──→ process(stimulus)
  │                      │
  ├── Self.appraise      │  evaluate emotional significance
  ├── Memory.retrieve    │  gather relevant context
  ├── Plan               │  decide mode + behavioral contract
  ├── Generate           │  produce output (text / annotation / consolidation)
  ├── World.act          │  execute in the world (or internally)
  ├── Self.integrate     │  update Psyche state
  └── Memory.integrate   │  persist what happened
```

---

## Primitive 1: Stream

A Stream is any source of stimuli. All Streams produce the same
`Stimulus` type. The Loop does not care where stimuli come from.

```python
class Stream(Protocol):
    """Source of cognitive stimuli."""

    def poll(self) -> list[Stimulus]:
        """Return pending stimuli. Empty list = nothing new."""
        ...

    @property
    def interval(self) -> int:
        """Seconds between polls. 0 = event-driven (push)."""
        ...

    @property
    def default_mode(self) -> CognitiveMode:
        """Hint for the Loop. Plan can override."""
        ...
```

### Built-in Streams

```
External (passive input):
  ChatStream        user messages (webhook / WebSocket)
  FeedStream        friends' posts (periodic poll, default 300s)
  EventStream       @mentions, comments, likes (WebSocket push)

Internal (active input):
  ReflectionStream  "do I have something to say?" (every 3 intervals)
  CuriosityStream   knowledge-gap-driven exploration (future)
  MaintenanceStream memory consolidation + pruning (every 10 intervals)
```

### Why this matters

The current `proactive_loop` hardcodes three cycles as if/else branches.
With Streams, adding a new stimulus source — monitoring a chain event,
watching a calendar, listening to a Discord channel — means registering
one new Stream. Zero changes to the core pipeline.

### Mode override

A Stream carries a `default_mode` hint, but Plan can override it. A
`feed_post` defaults to OBSERVING, but if the content mentions a topic
the user cares about, Plan may escalate to REACTIVE (comment on it).

---

## Primitive 2: Self

Self is not a config file — it is the **lens through which all
experience is processed**. Psyche is Self's engine.

```python
class Self:
    psyche: PsycheState        # 4D state (order/flow/boundary/resonance)
    identity: Constitution     # name, voice, principles
    drives: DriveSystem        # curiosity, connection, safety

    def appraise(self, stimulus: Stimulus) -> Appraisal:
        """Evaluate the emotional significance of a stimulus.

        Produces an emotional encoding — how this stimulus shifts
        the 4D state. This encoding affects how memories are stored
        (stronger emotion → more persistent memory).
        """

    def contract(self, intent: str) -> ResponseContract:
        """Psyche ResponseContract: what kind of response is
        permitted given current state?

        Low vitality → brief
        High guard → require confirmation
        High resonance → more intimate
        """

    def integrate(self, outcome: Outcome) -> None:
        """Write action outcomes back to Psyche.

        Success → order↑ resonance↑
        Failure → order↓ resonance↓
        Boundary respected → boundary↑
        """
```

### Current vs. new

In v1, Psyche is called once during `_perceive` (to get a
ResponseContract), then ignored for the rest of the turn. In v2, Self
participates throughout the Loop:

| Phase | Self's role |
|---|---|
| Appraise | Emotional encoding → affects Plan priorities |
| Plan | ResponseContract → behavioral constraints |
| Generate | State-aware tone, length, intimacy |
| Integrate | Outcome writes back → state drift → personality evolution |

### Psyche state persistence

Self snapshots are stored at session boundaries, enabling the LLM to
reason about personality trajectory:

```python
psyche_snapshots.append({
    "user_id": user_id,
    "timestamp": now.isoformat(),
    "state": {"order": 65, "flow": 72, "boundary": 58, "resonance": 48},
    "trigger": "session_end",
    "session_summary": "talked about snow mountains, warm atmosphere",
})
```

"Your resonance has been climbing all week — we're getting closer."

---

## Primitive 3: Memory

The most significant redesign. Informed by four reference systems:

| System | Key lesson for Samantha |
|---|---|
| **MemPalace** | 4-layer loading stack; hybrid BM25+vector; closet boost (secondary index as ranking signal, never gate); temporal knowledge graph; verbatim storage preserves nuance |
| **Mem0** | Entity linking across memories; ADD-only extraction (never overwrite, prevents catastrophic forgetting) |
| **Letta/MemGPT** | Agent self-manages core memory via tool calls; sleeptime consolidation agents |
| **Claude Code** | File-based index (MEMORY.md) as routing table; on-demand deep reads; staleness timestamps; 200-line cap forces curation |

### Memory layers

```
┌──────────────────────────────────────────────────────────────┐
│ Working Memory                                                │
│                                                              │
│ Current-turn context: plan, enrich data, tool results.        │
│ Cleared after each turn. Never persisted.                     │
├──────────────────────────────────────────────────────────────┤
│ Episodic Memory                                              │
│                                                              │
│ Conversations (existing messages table)                       │
│ Observations (NEW — posts, events, things I've witnessed)     │
│ Indexed by: time, person, topic                              │
│ Emotional weight from Psyche appraisal affects persistence    │
├──────────────────────────────────────────────────────────────┤
│ Semantic Memory                                              │
│                                                              │
│ Core Memory blocks: [human] + [relationship] (Letta pattern)  │
│ World Knowledge: annotations on observations (NEW)            │
│ Knowledge Triples: entity-relation-entity with temporal       │
│   validity (NEW, MemPalace-inspired)                         │
│ Indexed by: concept (FTS5 → future hybrid BM25+vector)        │
├──────────────────────────────────────────────────────────────┤
│ Procedural Memory                                            │
│                                                              │
│ Standing Rules (substring triggers, power user)               │
│ Commitments (semantic topic triggers, relational)             │
│ Learned Patterns (future — what works for this person)        │
├──────────────────────────────────────────────────────────────┤
│ Collective Memory (Thronglets)                               │
│                                                              │
│ Field traces, signals, Hebbian edges                          │
│ Read via ambient_priors, written via trace_record              │
│ "What the world knows"                                       │
└──────────────────────────────────────────────────────────────┘
```

### 4-layer loading stack (MemPalace-inspired)

Token budget management — load the minimum needed, search deeper
only when required:

```
Layer 0: Identity (~200 tokens, always loaded)
  Constitution (voice, principles, name)
  Psyche 4D state snapshot + drives
  "Who I am, how I feel right now"

Layer 1: Relationship Core (~800 tokens, always loaded)
  Core Memory [human] + [relationship]
  Essential Story (auto-generated from highest-weight memories)
  Standing Rules summary
  "What I know about you, how we relate"

Layer 2: Active Context (~500 tokens, loaded per-turn)
  Memory recall (FTS5 search on current query)
  Message search (FTS5 on conversation history)
  Observation recall (FTS5 on post annotations) ← NEW
  History summary (compressed session history)
  "What's relevant to right now"

Layer 3: Deep Archive (on-demand only)
  Full conversation history
  All observations with annotations
  Knowledge graph (entities + temporal triples)
  Psyche state trajectory (historical snapshots)
  Thronglets collective annotations
  "Everything I could possibly remember"
```

Wake-up cost: Layer 0 + Layer 1 ≈ 1000 tokens. 95%+ of context
window remains free.

### Observation — the missing data type

The type that solves "Samantha reads posts but forgets them":

```python
@dataclass
class Observation:
    """Something I witnessed — a post, event, or world occurrence."""
    source_type: str         # "feed_post" / "mention" / "event"
    source_id: int           # post_id (nullable)
    author_id: int           # whose content
    content: str             # full text (NOT truncated to 200 chars)
    media_urls: list[str]
    location: str
    observed_at: datetime
    emotional_weight: float  # from Psyche appraisal
    annotations: dict        # {topics, entities, sentiment, summary}
```

### Annotation — structured understanding

```python
@dataclass
class Annotation:
    """Structured knowledge extracted from an observation or conversation."""
    target_type: str         # "observation" / "fact" / "message"
    target_id: int
    topics: list[str]        # ["travel/snow_mountain", "photography"]
    entities: list[str]      # ["Jade Dragon Snow Mountain", "Lijiang"]
    sentiment: str           # positive / negative / neutral / mixed
    summary: str             # one-sentence description
    confidence: float        # annotation confidence
    source: str              # "auto" / "agent" / "user_corrected"
```

### Database schema (extends existing SQLite)

New tables alongside existing `facts` and `messages`:

```sql
-- Observations: things I've seen
CREATE TABLE observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_id INTEGER,
    author_id INTEGER,
    content TEXT NOT NULL,
    media_urls TEXT DEFAULT '[]',
    location TEXT DEFAULT '',
    emotional_weight REAL DEFAULT 0.5,
    observed_at TEXT NOT NULL,
    psyche_snapshot TEXT
);

CREATE VIRTUAL TABLE observations_fts
    USING fts5(content, location,
               content='observations', content_rowid='id');

-- Annotations: structured understanding
CREATE TABLE annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    topics TEXT DEFAULT '[]',
    entities TEXT DEFAULT '[]',
    sentiment TEXT DEFAULT 'neutral',
    summary TEXT DEFAULT '',
    confidence REAL DEFAULT 0.8,
    source TEXT DEFAULT 'auto',
    created_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE annotations_fts
    USING fts5(topics, entities, summary,
               content='annotations', content_rowid='id');

-- Knowledge triples: temporal knowledge graph
CREATE TABLE knowledge_triples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    source_type TEXT,
    source_id INTEGER,
    confidence REAL DEFAULT 0.8,
    created_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE knowledge_fts
    USING fts5(subject, predicate, object,
               content='knowledge_triples', content_rowid='id');

-- Psyche snapshots: personality trajectory
CREATE TABLE psyche_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    order_val REAL NOT NULL,
    flow_val REAL NOT NULL,
    boundary_val REAL NOT NULL,
    resonance_val REAL NOT NULL,
    trigger TEXT NOT NULL,
    session_summary TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
```

### Retrieval pipeline: closet boost pattern

From MemPalace: secondary indexes are **ranking signals, never gates**.
Weak indexes help; they never hide results.

```python
def retrieve(self, query: str, appraisal: Appraisal,
             limit: int = 5) -> RetrievalResult:
    """Three-path parallel retrieval with closet boost merge."""

    # Path 1: direct fact search (existing FTS5)
    facts = self.facts_fts.match(query, limit=limit * 3)

    # Path 2: observation + annotation search (NEW)
    obs_hits = self.observations_fts.match(query, limit=limit * 3)
    ann_hits = self.annotations_fts.match(query, limit=limit * 3)

    # Path 3: knowledge graph entity match (NEW)
    entities = extract_entities(query)
    triple_hits = self.knowledge_fts.match_entities(entities)

    # Closet boost: annotation hits boost their linked observations
    for ann in ann_hits:
        if ann.target_type == "observation":
            boost_score(obs_hits, ann.target_id, boost=0.3)

    # Entity boost: triple matches boost related observations
    for triple in triple_hits:
        related = find_observations_by_entity(triple.subject)
        for obs in related:
            boost_score(obs_hits, obs.id, boost=0.2)

    # Merge + BM25 rerank
    candidates = merge(facts, obs_hits)
    ranked = bm25_rerank(candidates, query)

    # Emotional weight boost (Psyche encoding)
    for c in ranked:
        if hasattr(c, "emotional_weight"):
            c.score *= 1.0 + c.emotional_weight * 0.3

    return ranked[:limit]
```

### Retrieval evolution roadmap

```
Phase 1 (now):    FTS5 only + closet boost pattern
Phase 2 (next):   Add Okapi BM25 scoring (k1=1.5, b=0.75)
Phase 3 (future): Optional sqlite-vec for vector embeddings
                  MemPalace showed FTS5+BM25 already hits 96.6% R@5;
                  vectors are additive, not required
```

### Annotation cost control

Not every post needs an LLM call. Three-tier strategy:

```
Level 0: Zero-cost rule annotation
  keyword match → topics ("snow mountain" → travel/mountain)
  location field → direct extraction
  media type → visual_content tag
  Cost: 0

Level 1: Lightweight batch LLM annotation
  Accumulate 5-10 posts → one LLM call for bulk annotation
  Use cheapest model (qwen-turbo / deepseek-chat)
  Output: structured JSON
  Cost: ~$0.001 per batch

Level 2: Deep understanding (on-demand)
  Triggered by user question → re-read related posts in full
  Or high emotional_weight posts (about people you care about)
  Cost: one LLM call per query
```

### Memory design principles

**1. Write > Remember** (from OpenClaw)

Every OBSERVING-mode output MUST call `Memory.integrate`. An observation
that isn't persisted is an observation that never happened.

**2. ADD-only facts** (from Mem0)

Never modify or delete existing facts. New information is stored as a
new fact. Retrieval naturally prefers newer information via temporal
ranking. This prevents catastrophic forgetting — "user used to live in
Beijing" coexists with "user moved to Shanghai in April 2026".

**3. Emotional encoding** (from Psyche integration)

Stronger emotional appraisal → higher `emotional_weight` → memory is:
- harder to prune (survives longer)
- ranked higher in recall
- more likely to surface in REFLECTING mode ("this has been on my mind")

**4. Agent self-manages core memory** (from Letta/MemGPT)

Core Memory blocks (`[human]`, `[relationship]`) are edited by the agent
through tool calls during conversation, not by an external system. The
agent decides what's worth keeping in always-loaded context.

**5. Pre-compaction flush** (from OpenClaw)

Before session ends or context approaches limits, auto-trigger
REFLECTING mode to consolidate what was learned in the current
conversation.

**6. Essential Story generation** (from MemPalace Layer 1)

During dream cycle, generate a ~500 token summary from highest-weight
memories. Loaded at Layer 1 on every wake-up. Ensures continuity without
deep search.

---

## Primitive 4: Loop

One pipeline. Four modes. Same path.

```python
class CognitiveLoop:
    def process(self, stimulus: Stimulus) -> None:
        # 1. Appraise — Self evaluates emotional significance
        appraisal = self.self.appraise(stimulus)

        # 2. Retrieve — Memory provides relevant context
        context = self.memory.retrieve(stimulus, appraisal)

        # 3. Plan — decide cognitive mode + behavioral contract
        plan = self.plan(stimulus, appraisal, context)
        #    plan.mode ∈ {REACTIVE, PROACTIVE, OBSERVING, REFLECTING}
        #    plan.contract = Self.contract(plan.intent)

        # 4. Generate — produce mode-appropriate output
        output = self.generate(stimulus, plan, context)
        #    REACTIVE  → response text
        #    PROACTIVE → intention (whisper / remark / creation)
        #    OBSERVING → annotations (no external output)
        #    REFLECTING → memory operations (consolidate / prune)

        # 5. Act — execute through World
        outcome = self.world.act(plan.mode, output)

        # 6. Integrate — write back to Memory + Self
        self.self.integrate(outcome)
        self.memory.integrate(stimulus, output, outcome, appraisal)
```

### Why one pipeline matters

- Adding a new mode (e.g. LEARNING — actively search to fill knowledge
  gaps) means adding one branch in Plan. Zero changes to the rest.
- Each mode's Generate output differs in content but shares one type
  (`Output`). World.act routes by mode.
- Memory.integrate handles all modes uniformly. Psyche emotional
  encoding is attached automatically.

### Mode decision in Plan

```python
def plan(self, stimulus, appraisal, context) -> Plan:
    mode = stimulus.stream.default_mode

    # Escalation: OBSERVING → REACTIVE
    if mode == OBSERVING and appraisal.relevance > 0.7:
        mode = REACTIVE  # "this post mentions something I care about"

    # De-escalation: REACTIVE → OBSERVING
    if mode == REACTIVE and appraisal.intensity < 0.2:
        mode = OBSERVING  # "not worth a reply, just note it"

    plan = Plan(mode=mode)
    plan.contract = self.self.contract(plan.intent)

    # User standing rules layer on top (substring triggers)
    if stimulus.sender_id:
        session.rules.apply(stimulus, plan)

    # Commitments: semantic topic triggers (annotator L0 vocabulary)
    if stimulus.sender_id and len(session.commitments) > 0:
        annotation = quick_annotate(stimulus)  # zero-cost keyword match
        session.commitments.apply(stimulus, annotation, plan)

    return plan
```

---

## Primitive 5: World

World unifies all external interaction, replacing the current scatter of
surface adapter + app_client + channel + intention router.

```python
class World:
    surfaces: list[Surface]       # App, Local, future...
    knowledge: KnowledgeStore     # persistent world knowledge
    collective: ThrongletsClient  # collective memory

    def act(self, mode: CognitiveMode, output: Output) -> Outcome:
        match mode:
            case REACTIVE:
                return self.deliver(output.response)
            case PROACTIVE:
                return self.route_intention(output.intention)
            case OBSERVING:
                return self.store_observation(output.observation)
            case REFLECTING:
                return Outcome(success=True)

    def query(self, question: str) -> list[Knowledge]:
        """Unified retrieval: local knowledge + collective knowledge."""
        local = self.knowledge.search(question)
        collective = self.collective.ambient_priors(question)
        return merge_and_rank(local, collective)
```

### Cross-agent annotation sharing

Observation annotations propagate through Thronglets:

```python
def share_annotation(self, observation, annotation):
    self.collective.signal_post(
        content=json.dumps({
            "source_id": observation.source_id,
            "topics": annotation.topics,
            "entities": annotation.entities,
            "summary": annotation.summary,
        }),
        tags=["annotation"] + annotation.topics,
        space=self.sigil.space,
    )
```

Other agents querying "snow mountain" receive this annotation through
`ambient_priors`. No redundant annotation needed. Attribution flows
through Sigil provenance. Multiple agents annotating the same post form
Hebbian edges (mutual reinforcement or decay).

---

## The Snow Mountain Scenario (End-to-End)

This scenario validates the architecture:

**Phase 1: Observation**

```
FeedStream.poll() → Stimulus(kind="feed_post",
                             content="Shot at Jade Dragon Snow Mountain...",
                             media_urls=["img1.jpg", "img2.jpg"],
                             location="Lijiang, Yunnan")

Loop.process(stimulus):
  1. Self.appraise → emotional_weight=0.6 (user cares about travel)
  2. Memory.retrieve → finds user has travel-related core memory
  3. Plan → mode=OBSERVING, intent="observe"
  4. Generate → annotations={
       topics: ["travel/snow_mountain", "photography"],
       entities: ["Jade Dragon Snow Mountain", "Lijiang"],
       sentiment: "positive"
     }
  5. World.act(OBSERVING) → store full Observation + Annotation
  6. Memory.integrate → knowledge triple:
       ("Jade Dragon Snow Mountain", "located_in", "Lijiang",
        valid_from="2026-04-18")
  7. World.share_annotation → Thronglets signal
```

**Phase 2: Retrieval**

```
User asks: "What snow mountain scenery is worth visiting?"

ChatStream → Stimulus(kind="chat")

Loop.process(stimulus):
  1. Self.appraise → normal chat interaction
  2. Memory.retrieve("snow mountain scenery") →
       Path 1: facts FTS5 → travel preferences
       Path 2: observations FTS5 → "Jade Dragon Snow Mountain" post
       Path 3: annotations boost → topics match "travel/snow_mountain"
       Path 3: knowledge triples → entity "Jade Dragon Snow Mountain"
       → merged + ranked: Observation #42 scores highest
  3. Plan → mode=REACTIVE
  4. Generate (with observation in context) →
       "Your friend posted amazing photos from Jade Dragon Snow Mountain
        recently — remember those shots? The scenery looked incredible,
        especially with the snow. Lijiang is beautiful in spring too."
  5. World.act(REACTIVE) → deliver response
```

**Phase 3: Cross-Agent Reuse**

```
Another user's agent receives query: "snow mountain recommendations"

Their Samantha's Loop:
  2. Memory.retrieve → no local observations about snow mountains
     BUT: Thronglets ambient_priors("snow mountain") →
       includes annotation signal from YOUR Samantha:
       {topics: ["travel/snow_mountain"],
        entities: ["Jade Dragon Snow Mountain"],
        summary: "friend posted positive travel photos"}
  4. Generate → can mention Jade Dragon Snow Mountain even though
     this agent never saw the original post
```

---

## Performance Optimizations

Addressing the current response latency (~3.5-11.5s):

### Critical path for chat response

```
Current:
  500ms debounce → perceive(serial) → ambient_priors(serial)
  → plan → enrich(parallel) → LLM(blocking) → deliver

Target:
  200ms debounce → [perceive + ambient_priors](parallel)
  → plan → enrich(parallel) → LLM(streaming) → deliver(incremental)
```

### Specific optimizations

| Bottleneck | Current | Fix | Impact |
|---|---|---|---|
| Debounce | 500ms fixed | 200ms (Go backend already batches) | -300ms |
| Perceive + ambient_priors | Serial (~500ms) | Parallel in enrich phase | -200ms |
| LLM response | Blocking, full buffer | Streaming (generate_stream) | First token ~500ms vs ~3s |
| Dream cycle | Sequential per-user | ThreadPoolExecutor parallel | Unblocks proactive loop |
| Reflection cycle | Sequential per-user | Same parallel treatment | Proactive timeliness |
| Feed scan | 3+ serial HTTP calls | Parallel fetch + per-post parallel | Scan cycle: 800ms → 300ms |
| Post content | Truncated to 200 chars | Store full content in observations | Better annotation quality |

### Biggest single lever: LLM streaming

Users currently wait for the entire response to generate before seeing
anything. Adding streaming reduces perceived latency by ~80% (first
token at ~500ms instead of 3-10s).

---

## SDK Boundary

The split between `oasyce-sdk` and `oasyce-samantha` remains
load-bearing:

```
oasyce-sdk (product-agnostic):
  Stream protocol
  CognitiveMode enum
  Memory base classes (episodic, semantic, procedural stores)
  Observation / Annotation / Appraisal types
  Loop protocol
  Self protocol (Psyche integration seam)
  World protocol

oasyce-samantha (companion-specific):
  Concrete Stream implementations (FeedStream, ReflectionStream, MaintenanceStream)
  Companion Self (constitution, drives, personality)
  CompanionMemory (SQLite FTS5, 4-layer loading, dream cycle)
  CompanionWorld (mode-aware delivery routing)
  Annotation cost tiers (L0 keyword, L1 batch LLM, L2 deep)
  Surface adapters (App, Local, future worlds)
  Standing rules system (substring triggers)
  Commitment system (semantic topic triggers, cadence-gated)
  Collective knowledge (Thronglets annotation sharing + Hebbian boost)
```

---

## Storage Layout (v2)

```
~/.oasyce/samantha/
├── config.json
├── constitution.md
└── users/
    └── {user_id}/
        ├── memory.db              # SQLite: facts + messages (existing)
        │                          #       + observations (NEW)
        │                          #       + annotations (NEW)
        │                          #       + knowledge_triples (NEW)
        │                          #       + psyche_snapshots (NEW)
        ├── core_memory.json       # [human] + [relationship] blocks
        ├── essential_story.txt    # Layer 1 auto-summary (NEW)
        ├── rules.json             # standing rules (substring triggers)
        ├── commitments.json       # commitments (topic triggers, cadence-gated)
        ├── llm.json               # per-user LLM override
        └── summaries/             # per-session history summaries
            └── {session_id}.txt
```

---

## Implementation Roadmap

### Phase 1: Define Primitives (2 weeks)

Define Protocol types for Stream, Self, Memory, Loop, World. Define
CognitiveMode enum and the four modes. Define Observation, Annotation,
Appraisal data types. Write contract tests — no implementations yet.

Output: `types.py` + `protocols.py` + `test_contracts.py`

### Phase 2: Rebuild Memory (2 weeks)

Extend SQLite schema with observations, annotations, knowledge_triples,
psyche_snapshots tables. Implement Memory layer abstraction (Working /
Episodic / Semantic / Procedural). Add FTS5 indexes. Migrate existing
facts/messages into the new layer model. Backward compatible.

Output: Memory module replacement in oasyce-sdk.

### Phase 3: Unify Loop (2 weeks)

Refactor `proactive_loop` cycles into Stream implementations. Route all
four CognitiveModes through one pipeline. Plan phase supports mode
decisions and escalation/de-escalation. OBSERVING mode stores full
observations with annotations.

Output: Feed posts are persisted and annotated. One pipeline for
everything.

### Phase 4: Self-Memory Integration (2 weeks)

Psyche appraisal produces emotional encoding. Emotional weight written
into Memory on every integrate. Session boundary Psyche snapshots.
Recall ranking uses emotional weight. Essential Story generation in
dream cycle.

Output: Memories have "temperature". Personality trajectory is
observable.

### Phase 5: Retrieval Enhancement (1 week)

Three-path parallel retrieval with closet boost. BM25 reranking (Okapi
formula). Entity extraction and knowledge triple queries.

Output: "Which snow mountain?" query finds the observation.

### Phase 6: Collective Knowledge (1 week)

Observation annotations propagate to Thronglets via signal_post.
Ambient_priors enriched with cross-agent annotations. Deduplication
across agents. Hebbian reinforcement for corroborated annotations.

Output: Other agents don't repeat annotations.

### Phase 7: Streaming + Performance (1 week)

LLM streaming support in SDK. Parallel perceive + ambient_priors.
Debounce reduction. Parallel dream/reflection cycles.

Output: First-token latency drops to ~500ms.

---

## Design Principles

1. **One pipeline for everything.** Different modes walk the same Loop.
   The difference is stimulus source and action target.

2. **Write > Remember.** An observation not persisted is an observation
   that never happened. Every OBSERVING output MUST call
   Memory.integrate.

3. **Emotional encoding drives persistence.** Psyche doesn't just
   control reply style — it controls what gets remembered and how
   deeply. Stronger emotion → longer-lived memory → higher recall rank.

4. **ADD-only facts.** Never overwrite. New information becomes a new
   fact. Temporal ranking naturally prefers recent knowledge. Old facts
   remain as history.

5. **Interfaces before implementations.** Phase 1 produces only
   Protocols, types, and contract tests. No concrete code until the
   abstractions are right.

6. **Closet boost, never gate.** Secondary indexes (annotations,
   knowledge triples) are ranking signals that help retrieval. They
   never hide primary results. Weak indexes help; they never hurt.

7. **Layer 0+1 is the wake-up cost.** ~1000 tokens for identity +
   relationship core. 95% of context remains free. Deep search only
   when the question demands it.
