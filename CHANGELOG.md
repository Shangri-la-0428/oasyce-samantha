# Changelog

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
