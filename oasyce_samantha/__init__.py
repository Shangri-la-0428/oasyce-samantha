"""Samantha — a runtime for persistent AI companions.

Samantha is a deployment of the ``oasyce_sdk.agent.Agent`` runtime
wired for the Oasyce App backend: AppChannel for chat replies, a
WebSocket listener + webhook handler for input, a proactive loop
for feeds, and a bundle of thirteen social/economic/memory tools.

Composition (inherited from Agent):
    Identity × Channel × Substrate × Tools × Constitution

For Samantha specifically:
    SigilManager × AppChannel × (Psyche + Thronglets HTTP)
      × social/economic/memory tools × constitution.md
"""

__version__ = "0.1.0"
