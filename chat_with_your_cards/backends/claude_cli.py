"""Claude Code CLI backend: persistent headless stream-json subprocess.

The CLI supplies the agent loop, context management, and MCP tool use;
this module only spawns it, feeds user messages, and maps its stream-json
output onto the backend-neutral ChatEvent union (DESIGN.md section 2).

No aqt imports: UI-thread marshaling is an injected callable, so the
whole backend can be exercised headlessly (see dev/cli_live_check.py).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .base import (
    ChatEvent,
    Done,
    ErrorEvent,
    EventCallback,
    TextDelta,
    ThinkingDelta,
    ToolCallFinished,
    ToolCallStarted,
    UsageUpdate,
)

# Inline chips ellipsize these anyway; the larger cap feeds the expandable
# tool-detail view (the chip's collapsed row still shows only a short hint).
SUMMARY_CHARS = 500
RESULT_CHARS = 500
LOG_ROTATE_BYTES = 512 * 1024

RunOnUi = Callable[[Callable[[], None]], None]


class BackendLog:
    """Append-only debug log (user_files/logs/backend.log), thread-safe.

    Exists so 'the chat hung' is diagnosable after the fact: spawn/exit,
    send, result subtypes, and the CLI's stderr all land here.
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._lock = threading.Lock()
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists() and path.stat().st_size > LOG_ROTATE_BYTES:
                    path.replace(path.with_suffix(".log.1"))
            except OSError:
                self._path = None

    def write(self, message: str) -> None:
        if self._path is None:
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self._lock, self._path.open("a", encoding="utf-8") as handle:
                handle.write(f"{stamp} {message}\n")
        except OSError:
            pass


def find_claude_cli(configured: str = "") -> str | None:
    """Locate the claude binary; GUI apps do not inherit the shell PATH."""
    if configured:
        return configured if Path(configured).exists() else None
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    candidates = [
        home / ".claude" / "local" / "claude",
        home / ".local" / "bin" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _compact(value: Any, limit: int) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    text = " ".join(text.split())
    return text[: limit - 1] + "…" if len(text) > limit else text


@dataclass
class ParserState:
    session_id: str | None = None
    started_calls: set[str] = field(default_factory=set)
    # Chars streamed via partial deltas since the last full assistant
    # message. Synthetic CLI messages (usage-limit notices, some errors)
    # skip the partial-delta path entirely; when a full assistant message
    # arrives with text nobody streamed, surface it as one TextDelta.
    streamed_chars: int = 0
    surfaced_texts: set[str] = field(default_factory=set)
    # Paragraph separation across text boundaries within one turn. Consecutive
    # text blocks (a new content block, or a second assistant message with no
    # intervening tool call) were concatenated with no whitespace, gluing
    # "…done.Next…" together. turn_text_chars tracks whether any text has been
    # emitted this turn (reset at `result`); last_block_index and pending_break
    # mark block/message boundaries so a "\n\n" is inserted before the next
    # text. Leading "\n\n" in a fresh UI bubble (after a tool chip) is trimmed
    # by the markdown renderer, so it is harmless there.
    turn_text_chars: int = 0
    last_block_index: int | None = None
    pending_break: bool = False


def _text_separator(state: ParserState, index: Any) -> str:
    """The whitespace to prefix onto the next streamed text delta, so text
    across a block/message boundary reads as a new paragraph, not glued."""
    boundary = state.pending_break or (
        index is not None
        and state.last_block_index is not None
        and index != state.last_block_index
    )
    sep = "\n\n" if boundary and state.turn_text_chars > 0 else ""
    state.pending_break = False
    if index is not None:
        state.last_block_index = index
    return sep


def parse_stream_line(obj: dict[str, Any], state: ParserState) -> list[ChatEvent]:
    """Map one stream-json object to zero or more ChatEvents.

    Text comes from partial-message deltas; tool calls from the full
    assistant/user messages (their inputs/results are complete there).
    Unknown shapes are ignored - the CLI adds event types over time.
    """
    kind = obj.get("type")

    if kind == "system":
        if obj.get("subtype") == "init":
            state.session_id = obj.get("session_id") or state.session_id
        return []

    if kind == "stream_event":
        event = obj.get("event") or {}
        if event.get("type") == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                text = str(delta["text"])
                sep = _text_separator(state, event.get("index"))
                state.streamed_chars += len(text)
                state.turn_text_chars += len(sep) + len(text)
                return [TextDelta(sep + text)]
            if delta.get("type") == "thinking_delta" and delta.get("thinking"):
                # Extended-thinking text. Deliberately bypasses
                # _text_separator/turn_text_chars/streamed_chars: those track
                # only the visible answer, and a thinking block's content-block
                # index must never perturb the paragraph-break bookkeeping for
                # text that streams before/after it. signature_delta (the
                # cryptographic continuation token for the thinking block) and
                # content_block_start/stop are intentionally not handled here -
                # nothing else about the CLI's stream carries visible text.
                return [ThinkingDelta(str(delta["thinking"]))]
        return []

    if kind == "assistant":
        events: list[ChatEvent] = []
        message = obj.get("message") or {}
        unstreamed_text: list[str] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            # "thinking" blocks are intentionally skipped here: their content
            # was already streamed (or, if the account/CLI redacts thinking
            # text, was empty throughout) via content_block_delta above, so
            # re-surfacing the full block would double-emit or leak it into
            # the visible answer.
            if block.get("type") == "text" and block.get("text"):
                unstreamed_text.append(block["text"])
            elif (
                block.get("type") == "tool_use"
                and block.get("id") not in state.started_calls
            ):
                state.started_calls.add(block["id"])
                events.append(
                    ToolCallStarted(
                        call_id=block["id"],
                        tool=str(block.get("name", "?")),
                        summary=_compact(block.get("input", {}), SUMMARY_CHARS),
                    )
                )
        joined = "\n\n".join(unstreamed_text)
        if joined and state.streamed_chars == 0 and joined not in state.surfaced_texts:
            state.surfaced_texts.add(joined)
            sep = "\n\n" if state.turn_text_chars > 0 else ""
            state.turn_text_chars += len(sep) + len(joined)
            events.insert(0, TextDelta(sep + joined))
        # Message boundary: the next text (a follow-up message, or text after a
        # tool call) starts a fresh paragraph. Block indices restart per message.
        state.streamed_chars = 0
        state.last_block_index = None
        state.pending_break = True
        return events

    if kind == "user":
        events = []
        message = obj.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    events.append(
                        ToolCallFinished(
                            call_id=str(block.get("tool_use_id", "")),
                            ok=not bool(block.get("is_error")),
                            summary=_compact(block.get("content", ""), RESULT_CHARS),
                        )
                    )
        return events

    if kind == "result":
        state.session_id = obj.get("session_id") or state.session_id
        # Turn over: reset paragraph-separation state so the next turn's first
        # text is not prefixed with a spurious break.
        state.turn_text_chars = 0
        state.last_block_index = None
        state.pending_break = False
        result_events: list[ChatEvent] = []
        usage = obj.get("usage") or {}
        cost = obj.get("total_cost_usd")
        if cost is not None or usage:
            result_events.append(
                UsageUpdate(
                    cost_usd=float(cost) if cost is not None else None,
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                )
            )
        if obj.get("subtype") == "success" or not obj.get("is_error"):
            return [*result_events, Done()]
        return [
            *result_events,
            ErrorEvent(str(obj.get("result") or obj.get("subtype"))),
            Done(),
        ]

    return []


VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def build_cli_args(
    *,
    cli_path: str,
    system_prompt: str,
    mcp_config_path: str,
    resume_session_id: str | None = None,
    model: str = "",
    effort: str = "",
    web_access: bool = True,
    mcp_inherit_user: bool = False,
    mcp_disabled: list[str] | None = None,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    # The agent lives in collection-land: its own shell/write tools stay off.
    # Skill is allowed so the user's system-wide skills and our card-authoring
    # template work; Read is allowed so local card sources (PDFs) open;
    # WebSearch/WebFetch are on by default (config web_access).
    allowed = ["mcp__anki", "Skill", "Read"]
    disallowed = ["Bash", "Edit", "Write", "NotebookEdit"]
    if web_access:
        allowed += ["WebSearch", "WebFetch"]
    else:
        disallowed += ["WebSearch", "WebFetch"]
    # MCP widening, config-file tier (DESIGN.md section 5, shipped 2026-07-10):
    # mcp_disabled names (ours or an inherited user server) become
    # mcp__<name> disallowedTools entries. Guard: "anki" can never be
    # disabled this way - that would silently break every propose_*/
    # collection tool - so it is dropped and logged instead of honored.
    for name in mcp_disabled or []:
        name = str(name).strip()
        if not name:
            continue
        if name == "anki":
            if log is not None:
                log(
                    "mcp_disabled config: ignoring 'anki' - disabling the "
                    "built-in server would silently break proposals"
                )
            continue
        disallowed.append(f"mcp__{name}")
    args = [
        cli_path,
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--append-system-prompt",
        system_prompt,
        "--mcp-config",
        mcp_config_path,
    ]
    if not mcp_inherit_user:
        # Restrictive by default: the agent sees only our --mcp-config
        # servers, never the user's own Claude Code MCP servers. Dropped
        # only when the user opts in to mcp_inherit_user - card/field
        # content is untrusted input, so silently wiring every server the
        # user configured for coding into a context that also ingests
        # untrusted card text is an exfiltration surface (DESIGN.md section
        # 5's "MCP scoping" decision).
        args.append("--strict-mcp-config")
    args += [
        "--allowedTools",
        ",".join(allowed),
        "--disallowedTools",
        ",".join(disallowed),
    ]
    if model.strip():
        args += ["--model", model.strip()]
    if effort.strip() in VALID_EFFORTS:
        args += ["--effort", effort.strip()]
    if resume_session_id:
        args += ["--resume", resume_session_id]
    return args


def write_mcp_config(
    directory: Path,
    url: str,
    token: str,
    *,
    extra_servers: dict[str, Any] | None = None,
    log: Callable[[str], None] | None = None,
) -> Path:
    """Write the CLI's --mcp-config JSON: our built-in ``anki`` server plus
    any dock-specific servers from the ``mcp_servers`` config key (DESIGN.md
    section 5's config-file MCP-widening tier, shipped 2026-07-10), merged
    verbatim in Claude-Code server-spec format.

    Guard: a user-supplied server named ``anki`` is dropped (logged, not
    silently merged) rather than allowed to shadow the built-in one - every
    propose_*/collection tool depends on that exact server. The built-in
    entry is also assigned last, after the merge, as defense in depth.
    """
    servers: dict[str, Any] = {}
    for name, spec in (extra_servers or {}).items():
        name = str(name)
        if name == "anki":
            if log is not None:
                log(
                    "mcp_servers config: ignoring a user-supplied server "
                    "named 'anki' - it cannot override the built-in one"
                )
            continue
        servers[name] = spec
    servers["anki"] = {
        "type": "http",
        "url": url,
        "headers": {"Authorization": f"Bearer {token}"},
    }
    config = {"mcpServers": servers}
    path = directory / "mcp-config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


class ClaudeCliSession:
    def __init__(
        self,
        *,
        cli_path: str,
        system_prompt: str,
        mcp_url: str,
        mcp_token: str,
        run_on_ui: RunOnUi,
        workdir: Path,
        log: BackendLog | None = None,
        model: str = "",
        effort: str = "",
        web_access: bool = True,
        extra_env: dict[str, str] | None = None,
        resume_session_id: str | None = None,
        mcp_servers: dict[str, Any] | None = None,
        mcp_inherit_user: bool = False,
        mcp_disabled: list[str] | None = None,
    ) -> None:
        self._cli_path = cli_path
        self._system_prompt = system_prompt
        self._model = model
        self._effort = effort
        self._web_access = web_access
        self._extra_env = extra_env or {}
        self._mcp_inherit_user = mcp_inherit_user
        self._mcp_disabled = list(mcp_disabled or [])
        self._workdir = workdir
        self._workdir.mkdir(parents=True, exist_ok=True)
        self._tmpdir = Path(tempfile.mkdtemp(prefix="cwyc-claude-"))
        self._run_on_ui = run_on_ui
        self._log = log or BackendLog(None)
        self._mcp_config = write_mcp_config(
            self._tmpdir,
            mcp_url,
            mcp_token,
            extra_servers=mcp_servers,
            log=self._log.write,
        )
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._state = ParserState()
        if resume_session_id:
            # History resume: the first spawn continues the old conversation
            # via --resume instead of starting fresh.
            self._state.session_id = resume_session_id
        self._generation = 0
        self._streaming = False
        self._on_event: EventCallback | None = None
        # Model/effort the live process was spawned with; a mismatch means
        # the user switched mid-chat and the next send must respawn.
        self._spawned_model = model
        self._spawned_effort = effort
        # Control-request/control-response correlation for interrupt(). Wire
        # framing ported (field names, not implementation) from the MIT-
        # licensed Claude Agent SDK's Query._send_control_request /
        # _read_messages: https://raw.githubusercontent.com/anthropics/
        # claude-agent-sdk-python/main/src/claude_agent_sdk/_internal/query.py
        # (MIT License, Copyright (c) 2025 Anthropic, PBC).
        self._control_request_counter = 0
        self._pending_control: dict[str, threading.Event] = {}
        self._pending_control_result: dict[str, dict[str, Any]] = {}

    @property
    def streaming(self) -> bool:
        return self._streaming

    @property
    def session_id(self) -> str | None:
        return self._state.session_id

    def prewarm(self) -> None:
        """Spawn the CLI ahead of the first send (hides startup latency)."""
        self._ensure_process()

    def set_model_effort(self, model: str, effort: str) -> None:
        """Switch model/effort mid-conversation. The change takes effect on
        the next send: the process respawns with --resume so the same
        conversation continues under the new model (matching the CLI apps).
        Applied lazily so an in-flight response is never interrupted."""
        self._model = model
        self._effort = effort

    def _ensure_process(self) -> None:
        if self._process is not None and self._process.poll() is None:
            if (self._spawned_model, self._spawned_effort) == (
                self._model,
                self._effort,
            ):
                return
            # Model/effort changed since this process spawned: tear it down
            # and respawn below with --resume to keep the conversation.
            # Bump the generation first so the dying process's reader thread
            # (which will see SIGTERM as a nonzero exit) is fenced off and
            # does not emit a spurious error/Done into the new turn.
            self._log.write(
                f"model switch {self._spawned_model or '-'}/{self._spawned_effort or '-'}"
                f" -> {self._model or '-'}/{self._effort or '-'} pid={self._process.pid}"
            )
            self._generation += 1
            self._process.terminate()
            self._process = None
        args = build_cli_args(
            cli_path=self._cli_path,
            system_prompt=self._system_prompt,
            mcp_config_path=str(self._mcp_config),
            resume_session_id=self._state.session_id,
            model=self._model,
            effort=self._effort,
            web_access=self._web_access,
            mcp_inherit_user=self._mcp_inherit_user,
            mcp_disabled=self._mcp_disabled,
            log=self._log.write,
        )
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.update(self._extra_env)  # e.g. ANTHROPIC_API_KEY for BYOK
        self._process = subprocess.Popen(
            args,
            cwd=self._workdir,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._spawned_model = self._model
        self._spawned_effort = self._effort
        self._log.write(
            f"spawn pid={self._process.pid} resume={self._state.session_id or '-'} "
            f"model={self._model or '-'} effort={self._effort or '-'} cli={self._cli_path}"
        )
        generation = self._generation
        self._reader = threading.Thread(
            target=self._read_loop,
            args=(self._process, generation),
            name="cwyc-claude-reader",
            daemon=True,
        )
        self._reader.start()
        threading.Thread(
            target=self._stderr_loop,
            args=(self._process,),
            name="cwyc-claude-stderr",
            daemon=True,
        ).start()

    def _stderr_loop(self, process: subprocess.Popen[str]) -> None:
        # Drain continuously: an undrained 16KB pipe would eventually block
        # the CLI mid-response. Everything goes to the backend log.
        if process.stderr is None:
            return
        for line in process.stderr:
            line = line.rstrip()
            if line:
                self._stderr_tail.append(line)
                self._log.write(f"[stderr pid={process.pid}] {line}")

    def send(self, text: str, on_event: EventCallback) -> None:
        if self._streaming:
            raise RuntimeError("send() while a response is still streaming")
        self._ensure_process()
        assert self._process is not None and self._process.stdin is not None
        self._on_event = on_event
        self._streaming = True
        payload = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
        try:
            self._process.stdin.write(json.dumps(payload) + "\n")
            self._process.stdin.flush()
            self._log.write(f"send chars={len(text)} pid={self._process.pid}")
        except (BrokenPipeError, OSError) as exc:
            self._streaming = False
            self._log.write(f"send failed: {exc}")
            self._deliver_now(ErrorEvent(f"could not talk to claude CLI: {exc}"))
            self._deliver_now(Done())

    def _read_loop(self, process: subprocess.Popen[str], generation: int) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                self._log.write(f"unparseable stdout line: {line[:200]}")
                continue
            if obj.get("type") == "control_response":
                # Reply to a control_request we sent (currently only
                # interrupt()). Routed here, never dispatched as a ChatEvent -
                # the CLI still emits the aborted turn's own Done/ErrorEvent
                # through the normal path below.
                self._handle_control_response(obj)
                continue
            if obj.get("type") == "result":
                self._log.write(
                    "result subtype={} is_error={} text={}".format(
                        obj.get("subtype"),
                        obj.get("is_error"),
                        _compact(obj.get("result") or "", 200),
                    )
                )
            events = parse_stream_line(obj, self._state)
            for event in events:
                self._dispatch(event, generation)
        if process.poll() not in (0, None) and generation == self._generation:
            stderr_tail = " | ".join(list(self._stderr_tail)[-5:])[-400:]
            self._log.write(f"cli exited code={process.returncode}")
            self._dispatch(
                ErrorEvent(
                    f"claude CLI exited with code {process.returncode}: {stderr_tail}"
                ),
                generation,
            )
            self._dispatch(Done(), generation)

    def _handle_control_response(self, obj: dict[str, Any]) -> None:
        response = obj.get("response") or {}
        request_id = response.get("request_id")
        if not isinstance(request_id, str):
            return
        event = self._pending_control.get(request_id)
        if event is None:
            return
        self._pending_control_result[request_id] = response
        event.set()

    def _dispatch(self, event: ChatEvent, generation: int) -> None:
        def deliver() -> None:
            if generation != self._generation:
                return
            if isinstance(event, Done):
                self._streaming = False
            if self._on_event is not None:
                self._on_event(event)

        self._run_on_ui(deliver)

    def _deliver_now(self, event: ChatEvent) -> None:
        if self._on_event is not None:
            self._on_event(event)

    def interrupt(self, timeout: float = 5.0) -> bool:
        """Ask the running turn to stop via a control_request, keeping the
        process (and conversation) alive - unlike cancel(), which tears the
        process down and forces the next send() to respawn with --resume.

        Wire framing (the control_request/control_response envelope and the
        "req_<counter>_<8 hex chars>" request_id format) is ported - field
        names only, not the implementation - from the MIT-licensed Claude
        Agent SDK's Query._send_control_request/interrupt:
        https://raw.githubusercontent.com/anthropics/claude-agent-sdk-python/
        main/src/claude_agent_sdk/_internal/query.py
        (MIT License, Copyright (c) 2025 Anthropic, PBC).

        On any failure - no live process, a write error, a timeout waiting
        for control_response (e.g. an older CLI that does not understand
        control requests), or an explicit error subtype - falls back to
        cancel() so the stop button can never wedge. Returns True only when
        the CLI acknowledged the interrupt; the caller should then stay
        silent and let the CLI's own aborted-turn Done/ErrorEvent close out
        the UI turn through the normal read loop (no generation bump, no
        double-emit).
        """
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            self.cancel()
            return False
        self._control_request_counter += 1
        request_id = f"req_{self._control_request_counter}_{os.urandom(4).hex()}"
        event = threading.Event()
        self._pending_control[request_id] = event
        control_request = {
            "type": "control_request",
            "request_id": request_id,
            "request": {"subtype": "interrupt"},
        }
        try:
            process.stdin.write(json.dumps(control_request) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self._pending_control.pop(request_id, None)
            self._log.write(f"interrupt write failed: {exc}")
            self.cancel()
            return False
        acknowledged = event.wait(timeout)
        response = self._pending_control_result.pop(request_id, None)
        self._pending_control.pop(request_id, None)
        if not acknowledged:
            self._log.write(f"interrupt timed out after {timeout}s pid={process.pid}")
            self.cancel()
            return False
        if response is not None and response.get("subtype") == "error":
            self._log.write(f"interrupt rejected: {response.get('error')}")
            self.cancel()
            return False
        self._log.write(f"interrupt acknowledged pid={process.pid}")
        return True

    def cancel(self) -> None:
        self._generation += 1
        self._streaming = False
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        self._process = None  # next send respawns with --resume

    def close(self) -> None:
        self.cancel()


class ClaudeCliBackend:
    def __init__(
        self,
        *,
        cli_path: str,
        system_prompt_builder: Callable[[], str],
        mcp_url: str,
        mcp_token: str,
        run_on_ui: RunOnUi,
        workdir: Path,
        log_path: Path | None = None,
        model_effort: Callable[[], tuple[str, str]] | None = None,
        web_access: bool = True,
        extra_env: dict[str, str] | None = None,
        mcp_servers: dict[str, Any] | None = None,
        mcp_inherit_user: bool = False,
        mcp_disabled: list[str] | None = None,
    ) -> None:
        self._cli_path = cli_path
        self._system_prompt_builder = system_prompt_builder
        self._mcp_url = mcp_url
        self._mcp_token = mcp_token
        self._run_on_ui = run_on_ui
        self._workdir = workdir
        self._log = BackendLog(log_path)
        self._model_effort = model_effort or (lambda: ("", ""))
        self._web_access = web_access
        self._extra_env = extra_env or {}
        self._mcp_servers = mcp_servers or {}
        self._mcp_inherit_user = mcp_inherit_user
        self._mcp_disabled = mcp_disabled or []

    def start_session(self, context: dict[str, Any]) -> ClaudeCliSession:
        model, effort = self._model_effort()
        return ClaudeCliSession(
            cli_path=self._cli_path,
            system_prompt=self._system_prompt_builder(),
            mcp_url=self._mcp_url,
            mcp_token=self._mcp_token,
            run_on_ui=self._run_on_ui,
            workdir=self._workdir,
            log=self._log,
            model=model,
            effort=effort,
            web_access=self._web_access,
            extra_env=self._extra_env,
            resume_session_id=(context or {}).get("resume_session_id"),
            mcp_servers=self._mcp_servers,
            mcp_inherit_user=self._mcp_inherit_user,
            mcp_disabled=self._mcp_disabled,
        )
