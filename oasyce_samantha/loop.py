"""Cognitive loop — unified Stream-based polling for Samantha.

Replaces the hardcoded 3-cycle proactive_loop with composable Streams.
Each Stream declares its own polling interval; the loop polls at the
fastest rate and fires each Stream when its interval elapses.

Streams:
  FeedStream         — adapter-contributed (social feed scan)
  ReflectionStream   — proactive intention generation
  MaintenanceStream  — memory prune + dream consolidation

Adding a new stimulus source means registering one Stream. Zero changes
to the core loop.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from oasyce_sdk.agent.stream import Stream

from .streams import MaintenanceStream, ReflectionStream

if TYPE_CHECKING:
    from .server import Samantha

logger = logging.getLogger(__name__)


def cognitive_loop(samantha: "Samantha", base_interval: int = 300) -> None:
    """Poll all registered Streams on their own schedules. Blocking.

    The loop assembles Streams from:
      1. The surface adapter (FeedStream for social surfaces)
      2. ReflectionStream (always, if chat is available)
      3. MaintenanceStream (always)

    Each returned Stimulus goes through ``samantha.process()``.
    """
    streams: list[Stream] = []

    adapter_streams = samantha.surface_adapter.contribute_streams(samantha)
    streams.extend(adapter_streams)

    streams.append(ReflectionStream(samantha, interval=base_interval * 3))
    streams.append(MaintenanceStream(samantha, interval=base_interval * 10))

    last_poll: dict[int, float] = {id(s): 0.0 for s in streams}
    tick = max(1, min(s.interval for s in streams) // 2) if streams else base_interval

    logger.info(
        "Cognitive loop started: %d streams, tick=%ds",
        len(streams), tick,
    )
    for s in streams:
        logger.info("  %s interval=%ds mode=%s", type(s).__name__, s.interval, s.default_mode.value)

    while True:
        now = time.time()
        for stream in streams:
            sid = id(stream)
            if now - last_poll[sid] < stream.interval:
                continue
            last_poll[sid] = now
            try:
                stimuli = stream.poll()
                for stimulus in stimuli:
                    try:
                        samantha.process(stimulus)
                    except Exception:
                        logger.debug(
                            "Process failed for %s stimulus",
                            type(stream).__name__, exc_info=True,
                        )
            except Exception:
                logger.debug(
                    "%s poll failed", type(stream).__name__, exc_info=True,
                )

        time.sleep(tick)


# ── Backward compat alias ────────────────────────────────────────

def proactive_loop(samantha: "Samantha", interval: int = 300) -> None:
    """Legacy entry point — delegates to cognitive_loop."""
    cognitive_loop(samantha, base_interval=interval)
