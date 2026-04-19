"""Samantha — a runtime for persistent AI companions.

Samantha is a companion runtime built on ``oasyce_sdk.agent.Agent``.
The SDK stays generic; this repo owns the companion-specific pieces:
sessions, memory, proactive loops, constitution, and surface adapters.

Composition (inherited from Agent):
    Identity × Channel × Substrate × Tools × Constitution

For Samantha specifically:
    SigilManager × SurfaceAdapter × (Psyche + Thronglets HTTP)
      × companion tools × constitution.md
"""

__version__ = "0.3.0"
