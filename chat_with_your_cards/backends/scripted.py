"""ScriptedBackend: replays canned scripts as streamed chat events.

Exists so the chat UI can be designed, demoed, and smoke-tested with no
real AI behind it. Emits the exact same event union a real backend will.

No aqt imports: the scheduler is injected. Production passes a QTimer
adapter; unit tests pass an immediate executor.
"""

from __future__ import annotations

import functools
import random
from typing import Any, Callable

from .base import (
    ChatEvent,
    Done,
    EventCallback,
    ProposalRequest,
    TextDelta,
    ThinkingDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from .fixtures import Step, select_script

# schedule(delay_ms, callback) -> None
Scheduler = Callable[[int, Callable[[], None]], None]

_DELTA_MIN_WORDS = 2
_DELTA_MAX_WORDS = 5
_DELTA_MIN_MS = 25
_DELTA_MAX_MS = 60
# Thinking beats land slower than text deltas - they are meant to be read as
# "still working", not raced through - so the rotating indicator's ~2.5s
# phrase cadence (ReasoningBlock.tsx) has time to actually rotate
# during manual preview iteration.
_THINK_MIN_MS = 200
_THINK_MAX_MS = 450


def compile_script(steps: list[Step], rng: random.Random) -> list[tuple[int, ChatEvent]]:
    """Flatten fixture steps into (delay_ms, event) pairs ending with Done."""
    timeline: list[tuple[int, ChatEvent]] = []
    call_counter = 0
    for step in steps:
        if step["kind"] == "text":
            for delta in _chop_markdown(step["markdown"], rng):
                timeline.append((rng.randint(_DELTA_MIN_MS, _DELTA_MAX_MS), TextDelta(delta)))
        elif step["kind"] == "thinking":
            # A quiet thinking phase: empty-text ThinkingDelta beats with a
            # growing token estimate, mirroring what the real Claude CLI
            # sends today (text redacted upstream at every effort level -
            # see claude_cli.py's parser and DESIGN.md section 9). Exercises
            # the rotating "Thinking..." indicator in both UIs before any
            # visible text or tool call appears.
            for tokens in step["estimated_tokens"]:
                timeline.append(
                    (
                        rng.randint(_THINK_MIN_MS, _THINK_MAX_MS),
                        ThinkingDelta("", int(tokens)),
                    )
                )
        elif step["kind"] == "tool":
            call_counter += 1
            call_id = f"call-{call_counter}"
            timeline.append(
                (
                    rng.randint(_DELTA_MIN_MS, _DELTA_MAX_MS),
                    ToolCallStarted(call_id, step["tool"], step["summary"]),
                )
            )
            timeline.append(
                (
                    int(step["duration_ms"]),
                    ToolCallFinished(call_id, bool(step["ok"]), step["result"]),
                )
            )
        elif step["kind"] == "propose":
            timeline.append(
                (
                    rng.randint(_DELTA_MIN_MS, _DELTA_MAX_MS),
                    ProposalRequest(step["proposal_kind"], dict(step["payload"])),
                )
            )
        else:
            raise ValueError(f"unknown fixture step kind: {step['kind']!r}")
    timeline.append((_DELTA_MAX_MS, Done()))
    return timeline


def _chop_markdown(markdown: str, rng: random.Random) -> list[str]:
    """Split markdown into small word-group deltas that reassemble verbatim."""
    deltas: list[str] = []
    words = markdown.split(" ")
    index = 0
    while index < len(words):
        take = rng.randint(_DELTA_MIN_WORDS, _DELTA_MAX_WORDS)
        chunk = " ".join(words[index : index + take])
        if index + take < len(words):
            chunk += " "
        deltas.append(chunk)
        index += take
    return deltas


class ScriptedSession:
    def __init__(self, schedule: Scheduler, rng: random.Random) -> None:
        self._schedule = schedule
        self._rng = rng
        # Bumping the generation invalidates all pending timer callbacks.
        self._generation = 0
        self._streaming = False

    @property
    def streaming(self) -> bool:
        return self._streaming

    def send(
        self,
        text: str,
        on_event: EventCallback,
        extra_blocks: list[dict] | None = None,
    ) -> None:
        # extra_blocks (#15b) are accepted for interface parity and ignored:
        # the scripted backend replays fixed timelines.
        if self._streaming:
            raise RuntimeError("send() while a response is still streaming")
        timeline = compile_script(select_script(text), self._rng)
        self._streaming = True
        generation = self._generation
        elapsed = 0

        def deliver(event: ChatEvent, gen: int) -> None:
            if gen != self._generation:
                return
            if isinstance(event, Done):
                self._streaming = False
            on_event(event)

        for delay, event in timeline:
            elapsed += delay
            self._schedule(elapsed, functools.partial(deliver, event, generation))

    def cancel(self) -> None:
        self._generation += 1
        self._streaming = False

    def interrupt(self) -> bool:
        """No real CLI to acknowledge a control_request against, so this is
        cancel() under another name: stop emission, no Done, no respawn to
        model (there is no process). Always reports "not acknowledged" so
        callers take the same fallback UI path (push a cancelled event) as
        an old-CLI/timeout ClaudeCliSession would."""
        self.cancel()
        return False

    def close(self) -> None:
        self.cancel()


class ScriptedBackend:
    def __init__(self, schedule: Scheduler, seed: int | None = None) -> None:
        self._schedule = schedule
        self._seed = seed

    def start_session(self, context: dict[str, Any]) -> ScriptedSession:
        rng = random.Random(self._seed)
        return ScriptedSession(self._schedule, rng)
