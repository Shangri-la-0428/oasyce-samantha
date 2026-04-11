"""AppChannel — reference Channel implementation for the Oasyce App backend.

The generic Channel Protocol / Agent delivery seam tests stay in
``oasyce-sdk/tests/test_channel.py`` — those cover the transport-agnostic
contract. This file keeps only the tests that depend on
``oasyce_samantha.channel.AppChannel`` (the reference implementation) and
``oasyce_samantha.app_client.AppClient``.

Invariants guarded:

  - ``AppChannel`` satisfies ``oasyce_sdk.agent.channel.Channel``
    structurally — no inheritance, no registration, just ``deliver``.
  - Chat stimuli with non-empty responses are delivered via
    ``AppClient.send_message(session_id, response)``.
  - Non-chat kinds (``comment`` / ``mention`` / ``feed_post``) are no-ops
    — their replies go through tool calls in the generator phase, not
    through the Channel. Double-delivering would double-post.
  - Empty responses are no-ops — Plan.intent=='observe' leaves response
    empty and the pipeline still calls ``deliver``.
  - Network failures are swallowed (logged), never raised. The pipeline
    has no try/except around the deliver hop by design, so a raising
    Channel would kill the worker thread and skip Reflect.
"""

from __future__ import annotations

from oasyce_sdk.agent.channel import Channel
from oasyce_sdk.agent.stimulus import Stimulus


# ── Protocol structural check ───────────────────────────────────

class TestAppChannelProtocol:
    def test_app_channel_satisfies_protocol(self):
        """The reference implementation must pass the structural check.

        If this test fails, ``AppChannel`` has drifted from the Protocol
        and Samantha cannot construct an Agent — the whole extraction
        is broken.
        """
        from oasyce_samantha.app_client import AppClient
        from oasyce_samantha.channel import AppChannel

        app = AppClient("http://127.0.0.1:0")  # never called in this test
        assert isinstance(AppChannel(app), Channel)


# ── AppChannel behaviour ────────────────────────────────────────

class _FakeAppClient:
    """Records send_message calls without doing any network I/O.

    ``requests.Session`` is expensive to stub mid-flight, and the real
    ``AppClient`` would raise on a non-existent base URL. A hand-rolled
    fake keeps the test tight to the Channel contract.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[int, str]] = []
        self.fail = fail

    def send_message(self, session_id: int, content: str) -> dict:
        if self.fail:
            raise RuntimeError("simulated network failure")
        self.calls.append((session_id, content))
        return {"ok": True}


class TestAppChannel:
    """AppChannel is chat-only, empty-tolerant, and exception-safe.

    These three invariants are stated in the Channel Protocol docstring
    and the AppChannel class docstring — but stating them is not
    enough. The pipeline body calls ``self.channel.deliver`` with
    *whatever* the plan produced, including edge cases the LLM
    invented on its own. If AppChannel raises, the Agent base class
    has no try/except around the deliver hop (by design — the contract
    says Channels MUST NOT raise on network errors), and a bad network
    condition would crash the worker thread.
    """

    def test_chat_kind_is_delivered(self):
        """The happy path: a chat stimulus with a real response is sent
        via ``AppClient.send_message`` with the session id as the first
        arg and the response as the second.
        """
        from oasyce_samantha.channel import AppChannel

        app = _FakeAppClient()
        channel = AppChannel(app)
        channel.deliver(
            Stimulus(kind="chat", content="hi", session_id=42, sender_id=7),
            "hello there",
        )
        assert app.calls == [(42, "hello there")]

    def test_non_chat_kinds_are_not_delivered(self):
        """Comments, mentions, and feed posts express their reply through
        *tool calls* in the generator phase — not through the Channel.

        This asymmetry is load-bearing: the Channel Protocol is "where
        replies go", and for a post comment the reply IS the tool
        invocation. If AppChannel ever started delivering these kinds,
        Samantha would double-post every comment reply.
        """
        from oasyce_samantha.channel import AppChannel

        app = _FakeAppClient()
        channel = AppChannel(app)

        for kind in ("comment", "mention", "feed_post"):
            channel.deliver(
                Stimulus(kind=kind, content="payload", session_id=99),
                "should not be sent",
            )

        assert app.calls == []

    def test_empty_response_is_a_noop(self):
        """Plan.intent=='observe' leaves response empty. The pipeline
        still calls deliver — Channel implementations must treat empty
        strings as a no-op rather than posting empty messages.
        """
        from oasyce_samantha.channel import AppChannel

        app = _FakeAppClient()
        channel = AppChannel(app)
        channel.deliver(
            Stimulus(kind="chat", content="hi", session_id=1),
            "",
        )
        assert app.calls == []

    def test_network_failure_is_swallowed_not_raised(self):
        """Channel contract: deliver MUST NOT raise on network errors.

        The pipeline has no try/except around the deliver hop. If
        AppClient.send_message raises and AppChannel doesn't catch it,
        the exception propagates into ``Agent._deliver`` and then into
        the executor, potentially killing the worker thread. Worse,
        Reflect never runs, so Thronglets loses the turn.

        This test guards the invariant directly: even when the App
        backend explodes, deliver returns normally.
        """
        from oasyce_samantha.channel import AppChannel

        app = _FakeAppClient(fail=True)
        channel = AppChannel(app)
        # Must not raise
        channel.deliver(
            Stimulus(kind="chat", content="hi", session_id=1),
            "will fail",
        )
