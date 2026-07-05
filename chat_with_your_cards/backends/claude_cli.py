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
    ToolCallFinished,
    ToolCallStarted,
    UsageUpdate,
)

SUMMARY_CHARS = 90
RESULT_CHARS = 120
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
                state.streamed_chars += len(delta["text"])
                return [TextDelta(delta["text"])]
        return []

    if kind == "assistant":
        events: list[ChatEvent] = []
        message = obj.get("message") or {}
        unstreamed_text: list[str] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
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
            events.insert(0, TextDelta(joined))
        state.streamed_chars = 0
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
) -> list[str]:
    # The agent lives in collection-land: its own file/shell tools stay off.
    # Skill is allowed so the user's system-wide skills and our card-authoring
    # template work; WebSearch/WebFetch are on by default (config web_access).
    allowed = ["mcp__anki", "Skill"]
    disallowed = ["Bash", "Edit", "Write", "NotebookEdit"]
    if web_access:
        allowed += ["WebSearch", "WebFetch"]
    else:
        disallowed += ["WebSearch", "WebFetch"]
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
        "--strict-mcp-config",
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


def write_mcp_config(directory: Path, url: str, token: str) -> Path:
    config = {
        "mcpServers": {
            "anki": {
                "type": "http",
                "url": url,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }
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
    ) -> None:
        self._cli_path = cli_path
        self._system_prompt = system_prompt
        self._model = model
        self._effort = effort
        self._web_access = web_access
        self._extra_env = extra_env or {}
        self._workdir = workdir
        self._workdir.mkdir(parents=True, exist_ok=True)
        self._tmpdir = Path(tempfile.mkdtemp(prefix="cwyc-claude-"))
        self._mcp_config = write_mcp_config(self._tmpdir, mcp_url, mcp_token)
        self._run_on_ui = run_on_ui
        self._log = log or BackendLog(None)
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._state = ParserState()
        self._generation = 0
        self._streaming = False
        self._on_event: EventCallback | None = None
        # Model/effort the live process was spawned with; a mismatch means
        # the user switched mid-chat and the next send must respawn.
        self._spawned_model = model
        self._spawned_effort = effort

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

    def cancel(self) -> None:
        self._generation += 1
        self._streaming = False
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
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
        )
