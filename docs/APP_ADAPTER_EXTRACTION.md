# App Adapter Extraction

This note is the handoff boundary for the day `app-legacy` moves out of
`oasyce-samantha` and becomes an adapter package owned by `Oasis_App`.

## Goal

Keep Samantha's companion core stable while making the App surface easy
to relocate.

## Files that are already App-surface owned in spirit

These files are the primary extraction set:

- [oasyce_samantha/adapters/legacy_app.py](/Users/wutongcheng/Desktop/oasyce-samantha/oasyce_samantha/adapters/legacy_app.py:1)
- [oasyce_samantha/adapters/legacy_app_surface.py](/Users/wutongcheng/Desktop/oasyce-samantha/oasyce_samantha/adapters/legacy_app_surface.py:1)
- [oasyce_samantha/adapters/legacy_app_tools.py](/Users/wutongcheng/Desktop/oasyce-samantha/oasyce_samantha/adapters/legacy_app_tools.py:1)
- [oasyce_samantha/app_client.py](/Users/wutongcheng/Desktop/oasyce-samantha/oasyce_samantha/app_client.py:1)
- [oasyce_samantha/channel.py](/Users/wutongcheng/Desktop/oasyce-samantha/oasyce_samantha/channel.py:1)
- [oasyce_samantha/http.py](/Users/wutongcheng/Desktop/oasyce-samantha/oasyce_samantha/http.py:1)
- [oasyce_samantha/ws_client.py](/Users/wutongcheng/Desktop/oasyce-samantha/oasyce_samantha/ws_client.py:1)
- [oasyce_samantha/intention.py](/Users/wutongcheng/Desktop/oasyce-samantha/oasyce_samantha/intention.py:1)
- [oasyce_samantha/profiles.py](/Users/wutongcheng/Desktop/oasyce-samantha/oasyce_samantha/profiles.py:1)

If these move together, the companion core should need only import-path
updates and adapter loading config.

## Files that should stay in Samantha core

- [oasyce_samantha/server.py](/Users/wutongcheng/Desktop/oasyce-samantha/oasyce_samantha/server.py:1)
- [oasyce_samantha/tools.py](/Users/wutongcheng/Desktop/oasyce-samantha/oasyce_samantha/tools.py:1)
- [oasyce_samantha/loop.py](/Users/wutongcheng/Desktop/oasyce-samantha/oasyce_samantha/loop.py:1)
- [oasyce_samantha/rules.py](/Users/wutongcheng/Desktop/oasyce-samantha/oasyce_samantha/rules.py:1)
- [oasyce_samantha/constitution.py](/Users/wutongcheng/Desktop/oasyce-samantha/oasyce_samantha/constitution.py:1)
- [oasyce_samantha/commands.py](/Users/wutongcheng/Desktop/oasyce-samantha/oasyce_samantha/commands.py:1)
- [oasyce_samantha/adapters/base.py](/Users/wutongcheng/Desktop/oasyce-samantha/oasyce_samantha/adapters/base.py:1)
- [oasyce_samantha/adapters/local.py](/Users/wutongcheng/Desktop/oasyce-samantha/oasyce_samantha/adapters/local.py:1)

These define Samantha as a standalone companion runtime rather than an
App-bound sidecar.

## Extraction sequence

1. Copy the App-surface files into an `Oasis_App` Python package.
2. Export a factory or class such as `oasis_app.agent:create_adapter`.
3. Point Samantha config at that package via `adapter_import`.
4. Keep `adapter="app-legacy"` as a short-lived compatibility alias.
5. Remove the in-repo legacy adapter after one stable migration window.

## What should not move

Do not move generic Agent abstractions into the App package.
Do not move Samantha memory, dream, rules, or session logic into the App package.
Do not change `oasyce-sdk` just to make the App adapter more convenient.

## Success condition

When the extraction is complete:

- Samantha still runs locally with `adapter=local`
- App behavior still works through an external adapter package
- adding a new surface does not require editing Samantha core
