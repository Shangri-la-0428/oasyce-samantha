"""CompanionWorld — Samantha's mode-aware delivery.

Routes pipeline output through the correct delivery path based on
cognitive mode:

  REACTIVE   → channel.deliver (chat response to user)
  PROACTIVE  → deliver_proactive with SILENCE filtering
  OBSERVING  → no delivery (observation already stored)
  REFLECTING → no delivery (maintenance handled by dream)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from oasyce_sdk.agent.cognitive import CognitiveMode
from oasyce_sdk.agent.world import Outcome

if TYPE_CHECKING:
    from .server import Samantha
    from oasyce_sdk.agent.planner import Plan
    from oasyce_sdk.agent.stimulus import Stimulus

logger = logging.getLogger(__name__)


class CompanionWorld:
    """Unified delivery for all four cognitive modes."""

    def __init__(self, runtime: "Samantha") -> None:
        self._runtime = runtime

    def act(
        self,
        mode: CognitiveMode,
        stimulus: "Stimulus",
        response: str | None,
        plan: "Plan",
    ) -> Outcome:
        if mode == CognitiveMode.REACTIVE:
            if response:
                self._runtime.channel.deliver(stimulus, response)
                return Outcome(success=True, detail="delivered")
            return Outcome(success=True, detail="no response")

        if mode == CognitiveMode.PROACTIVE:
            if not response or response.strip().upper() == "SILENCE":
                return Outcome(success=True, detail="silence")
            urgency = 0.3 if not stimulus.metadata.get("time_context") else 0.5
            self._runtime.deliver_proactive(
                stimulus.sender_id,
                response,
                urgency=urgency,
                context=stimulus.metadata,
            )
            return Outcome(success=True, detail="proactive")

        if mode == CognitiveMode.OBSERVING:
            return Outcome(success=True, detail="observed")

        if mode == CognitiveMode.REFLECTING:
            return Outcome(success=True, detail="reflected")

        return Outcome(success=True)
