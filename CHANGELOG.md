# Changelog

## [0.3.0] - 2026-04-19

Requires `oasyce-sdk>=0.14.0` for the `World` Protocol, `CognitiveMode`
routing, and `Annotation` type.

### Added

- **v2 cognitive architecture** (Phases 1-10). One unified pipeline for
  all four cognitive modes: REACTIVE, PROACTIVE, OBSERVING, REFLECTING.
  Three concrete Streams (FeedStream, ReflectionStream, MaintenanceStream),
  CompanionWorld for mode-aware delivery routing, and a cognitive loop
  that polls all registered streams on their own schedules.

- **Observation + Annotation system.** Feed posts are persisted as full
  `Observation` objects with three-tier annotation: L0 zero-cost keyword
  matching, L1 batch LLM (~$0.001/batch), L2 deep on-demand.

- **CompanionMemory.** Unified 4-layer memory: episodic (facts +
  messages), observations, knowledge triples, psyche snapshots. Essential
  story generation in dream cycle. Four-path retrieval with closet boost.

- **Self/Appraisal integration.** Psyche kernel state drives emotional
  encoding of observations. High-intensity turns save psyche snapshots
  for personality trajectory tracking.

- **Collective knowledge.** Observation annotations shared to Thronglets
  via `signal_post`. Cross-agent annotation retrieval via `ambient_priors`.
  Hebbian boost for corroborated observations during dream cycle.

- **Pre-compaction flush.** Session counters track turns and estimated
  tokens. Threshold-triggered summarize + consolidate. Idle timeout
  detection in MaintenanceStream.

- **LLM streaming.** `process_stream(stimulus, on_token)` delivers text
  chunks incrementally. World delivery skipped — caller routes chunks.

- **Commitment system.** New module `oasyce_samantha.commitments` with
  `Commitment` / `CommitmentSet` / `load_commitments`. Commitments are
  semantic topic-triggered behavioral agreements — higher-level than
  standing rules:

  | | Rule | Commitment |
  |-|------|-----------|
  | Trigger | text substring | annotator L0 topic |
  | Identity | configuration | relational agreement |
  | State | stateless | active/paused, fired_count |
  | Cadence | every match | every / daily / contextual |

  Three new tools — `make_commitment`, `list_commitments`,
  `withdraw_commitment` — let the LLM manage commitments from inside
  conversation. `_quick_annotate` provides zero-cost topic detection
  for chat stimuli. Commitments compose into `Plan.focus` and
  `Plan.tools` alongside standing rules, never overwriting either.

  Storage: `{workspace}/commitments.json` with hot reload (mtime check).

### Changed

- **Pipeline now mode-aware.** `run_pipeline` accepts optional `World`
  and routes delivery through `World.act(mode, stimulus, response, plan)`
  instead of the raw `deliver` callback.

- **`_deliver` simplified.** Samantha's `_deliver` is now a passthrough
  to `channel.deliver`. SILENCE filtering and proactive routing moved
  to `CompanionWorld.act`.

## [0.2.0] - 2026-04-12

Requires `oasyce-sdk>=0.13.0` for the `Tool.terminal` flag and the
`Agent._plan` hook.

### Added

- **Per-user standing rules.** New module `oasyce_samantha.rules` with
  `UserRule` / `RuleSet` / `load_rules`. Each user can drop a
  `~/.oasyce/samantha/users/{id}/rules.json` file with directives like:

      {
        "rules": [
          {
            "name": "food-coach",
            "triggers": ["吃", "餐", "外卖"],
            "instruction": "估算这餐的热量, 评价营养均衡, 建议下一餐",
            "tools": ["save_memory"]
          }
        ]
      }

  Rules apply on every matching stimulus — chat, comment, mention, or
  feed_post — and compose into the Plan via `focus` and `tools`. They
  never overwrite Psyche- or Thronglets-driven decisions: the matched
  instructions are appended to whatever focus the SDK Planner already
  produced, and the tool whitelist is unioned, never narrowed.

  The `RuleSet` hot-reloads from disk on every `apply()` call (mtime
  check), so editing `rules.json` in another window takes effect on
  the next stimulus — no Samantha restart needed.

  Implementation uses the new SDK seam: `Samantha._plan` overrides
  `Agent._plan`, calls `super()._plan(...)` for the Psyche/Thronglets
  baseline, then layers `session.rules.apply(stimulus, plan)` on top.
  No edits to the SDK Planner.

- **`Session.rules: RuleSet`** — loaded at session creation from the
  user's workspace. Empty RuleSet if `rules.json` is missing or
  malformed (file errors are warnings, never crashes).

- **Chat-managed standing rules.** Three new tools —
  `add_standing_rule`, `list_standing_rules`, `remove_standing_rule` —
  let the LLM CRUD the user's `rules.json` from inside a conversation.
  "从现在开始, 每次我发吃的都估算一下热量" turns into
  `add_standing_rule(name=..., triggers=..., instruction=...)`, the
  `RuleSet` upserts by name and saves to disk, and the next stimulus
  picks up the rule via the hot-reload path. The JSON file stays the
  source of truth — power users can still edit it directly; the chat
  tools and the file editor are two front-ends on the same state.

  All three are **non-terminal**: after the CRUD call the LLM still
  owes the user a natural-language confirmation ("好的, 记下了"), so
  the tool loop must not break. This mirrors how `save_memory` and
  `recall_memory` work — read/write-to-own-state actions let the
  conversation continue in the same turn.

### Changed

- **Social-write tools are now terminal.** `comment_on_post`,
  `reply_to_comment`, and `like_post` are registered with
  `terminal=True`. The SDK 0.13.0 tool loop honours this flag and
  ends the turn after a successful call, fixing the multi-reply bug
  where a single mention with an image could produce 2-3 duplicate
  comments because the LLM re-emitted `comment_on_post` across
  successive tool-loop rounds.

  Read-side tools (`save_memory`, `recall_memory`, `query_balance`,
  feed/post fetchers) stay non-terminal so the LLM can chain
  recall → answer in one turn.

## [0.1.0] - 2026-04-11

First release as a standalone package, extracted from `oasyce-sdk` 0.11.6.

### Why the split

Samantha had grown into a full deployment of the `Agent` runtime: an
App backend client, a WebSocket listener, a webhook handler, a
proactive loop, thirteen social tools, a default persona. None of
that belonged in `oasyce-sdk` — the SDK's job is primitives (Chain,
Sigil, Agent base, Psyche/Thronglets clients), not a specific
companion. Keeping Samantha inside the SDK coupled every SDK release
to App-backend decisions, and made it hard to reason about either
project on its own terms.

0.11.6 of the SDK did the architectural work: `Agent = Identity ×
Channel × Substrate`, with `Channel` as a narrow Protocol. Once the
runtime was transport-agnostic, Samantha became "subclass Agent,
provide a Channel, provide tools" — a user of the SDK, not part of
it. This release is the packaging consequence of that architectural
move.

### Added

- **New package `oasyce_samantha`** depending on `oasyce-sdk>=0.12.0`.
  Imports rewritten from relative (`..agent.X`) to absolute
  (`oasyce_sdk.agent.X`) throughout.
- **`oasyce_samantha.profiles`** — named connection profiles for the
  Oasyce App backend, replacing the hardcoded production URL that
  lived in the old `samantha.cli`. Ships with `PUBLIC` (the official
  Aliyun-hosted backend) and `LOCAL` (`http://127.0.0.1:39277`).
  `OASYCE_APP_API_BASE` overrides both for CI / private deployments.
- **Neutral default constitution.** The bundled `constitution.md`
  template now describes the generic "Samantha" persona — users write
  their own identity document by editing
  `~/.oasyce/samantha/constitution.md`.
- **Single-entry CLI.** `oasyce-samantha` with no arguments starts the
  sidecar (unchanged for systemd). `oasyce-samantha init` and
  `oasyce-samantha status` are subcommands on the same entry point —
  no more `oasyce samantha ...` indirection through the SDK CLI.

### Migrated from oasyce-sdk 0.11.6

Eleven source files moved from `oasyce_sdk/samantha/` to
`oasyce_samantha/` with rewritten imports:

- `server.py`, `app_client.py`, `channel.py` (AppChannel), `cli.py`,
  `constitution.py`, `http.py`, `loop.py`, `tools.py`, `ws_client.py`,
  `__init__.py`, `__main__.py`.

Tests moved:

- `tests/test_samantha.py` — the Samantha-specific half of the SDK's
  old `test_samantha.py`: constitution defaults, App tool handlers,
  `Session` isolation, tool-schema shape. 12 tests. The generic half
  (Memory, Context, Planner, Evaluator, Dream helpers) stayed in the
  SDK as `tests/test_agent_modules.py`.
- `tests/test_app_channel.py` — extracted from the SDK's
  `test_channel.py` (the four `TestAppChannel` tests plus
  `test_app_channel_satisfies_protocol`). The generic Channel Protocol
  and Agent delivery seam tests stay in the SDK.

Docs:

- `docs/ARCHITECTURE.md` — rewritten from the SDK's
  `SAMANTHA_ARCHITECTURE.md` to be framework-focused: drops the
  personal deployment section and Joi references, keeps the pipeline
  design and composition model.
