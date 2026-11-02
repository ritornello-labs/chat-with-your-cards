"""Exercise installed runtimes against a synthetic loopback model and MCP.

No real model service, credentials, Anki profile or user configuration is used.
Raw protocol captures belong in the supplied output directory outside Git.
This is a compatibility probe, not an assessment of model/tutor quality.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from chat_with_your_cards.mcp_server import McpServer  # noqa: E402


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def env_for(root: Path) -> dict[str, str]:
    for name in ("home", "tmp", "codex", "claude", "pi"):
        (root / name).mkdir(parents=True, exist_ok=True)
    # Actual per-child home/config isolation, not aliases for user directories.
    node_dir = str(
        Path(os.environ.get("CWYC_EVAL_NODE_BIN", shutil.which("node") or "node")).parent
    )
    return {
        "PATH": node_dir + os.pathsep + os.environ["PATH"],
        "HOME": str(root / "home"),
        "TMPDIR": str(root / "tmp"),
        "LANG": "en_US.UTF-8",
        "CODEX_HOME": str(root / "codex"),
        "CLAUDE_CONFIG_DIR": str(root / "claude"),
        "PI_CODING_AGENT_DIR": str(root / "pi"),
        "PI_OFFLINE": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }


class Process:
    def __init__(self, command: list[str], root: Path, env: dict[str, str], label: str):
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.seen: list[dict[str, Any]] = []
        self.root, self.label = root, label
        self.stderr = (root / f"{label}-stderr.log").open("w")
        self.stdout = (root / f"{label}-stdout.log").open("w")
        self.proc = subprocess.Popen(
            command,
            cwd=root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        assert self.proc.stdout
        for line in self.proc.stdout:
            self.stdout.write(line)
            self.stdout.flush()
            try:
                event = json.loads(line)
            except ValueError:
                continue
            self.seen.append(event)
            self.events.put(event)

    def send(self, event: dict[str, Any]):
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(event) + "\n")
        self.proc.stdin.flush()

    def wait(self, predicate, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                event = self.events.get(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"{self.label}: process exited before expected event")
                continue
            if predicate(event):
                return event
        raise TimeoutError(f"{self.label}: protocol event timeout")

    def rpc(self, method, params=None):
        rid = uuid.uuid4().hex
        self.send({"id": rid, "method": method, "params": params or {}})
        result = self.wait(lambda e: e.get("id") == rid)
        if "error" in result:
            raise RuntimeError(f"{self.label}: RPC {method} failed")
        return result.get("result", {})

    def pi(self, command, **params):
        rid = uuid.uuid4().hex
        self.send({"id": rid, "type": command, **params})
        result = self.wait(lambda e: e.get("id") == rid)
        if not result.get("success"):
            raise RuntimeError(f"{self.label}: RPC {command} failed")
        return result.get("data", {})

    def close(self):
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=3)
        self.reader.join(timeout=1)
        write_json(self.root / f"{self.label}-protocol.json", self.seen)
        self.stderr.close()
        self.stdout.close()


class ModelServer:
    def __init__(self):
        self.requests: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"data":[]}')

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                owner.requests.append({"path": self.path, "body": body})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    if "messages" in self.path:
                        self.anthropic(body)
                    else:
                        self.emit_responses(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def event(self, kind, data):
                payload = {"type": kind, **data}
                self.wfile.write((f"event: {kind}\ndata: {json.dumps(payload)}\n\n").encode())
                self.wfile.flush()

            def pick_tool(self, body):
                history = body.get("messages", body.get("input", []))

                def has_result(value):
                    if isinstance(value, list):
                        return any(has_result(v) for v in value)
                    if isinstance(value, dict):
                        return value.get("type") in ("function_call_output", "tool_result") or any(
                            has_result(v) for v in value.values()
                        )
                    return False

                if "CWYC_TOOL_PROBE" not in json.dumps(history) or has_result(history):
                    return None
                for tool in body.get("tools", []):
                    name = tool.get("name", "")
                    if name.endswith("search_notes"):
                        return {"name": name}
                    if tool.get("type") == "namespace":
                        for nested in tool.get("tools", []):
                            if nested.get("name") == "search_notes":
                                return {"name": "search_notes", "namespace": name}
                return None

            def emit_responses(self, body):
                rid = "resp_" + uuid.uuid4().hex[:12]
                base = {
                    "id": rid,
                    "object": "response",
                    "created_at": int(time.time()),
                    "model": body.get("model", "eval"),
                    "status": "in_progress",
                    "output": [],
                }
                self.event("response.created", {"response": base})
                tool = self.pick_tool(body)
                if tool:
                    item = {
                        "id": "fc_" + rid,
                        "call_id": "call_" + rid,
                        "type": "function_call",
                        **tool,
                        "arguments": "",
                        "status": "in_progress",
                    }
                    self.event("response.output_item.added", {"output_index": 0, "item": item})
                    self.event(
                        "response.function_call_arguments.delta",
                        {"output_index": 0, "item_id": item["id"], "delta": "{}"},
                    )
                    item = {**item, "arguments": "{}", "status": "completed"}
                else:
                    item = {
                        "id": "msg_" + rid,
                        "type": "message",
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [],
                    }
                    self.event("response.output_item.added", {"output_index": 0, "item": item})
                    self.event(
                        "response.content_part.added",
                        {
                            "output_index": 0,
                            "item_id": item["id"],
                            "content_index": 0,
                            "part": {"type": "output_text", "text": "", "annotations": []},
                        },
                    )
                    chunks = ["Synthetic ", "explanation."]
                    if "CWYC_WAIT" in json.dumps(body):
                        chunks = ["waiting "] * 300
                    for chunk in chunks:
                        self.event(
                            "response.output_text.delta",
                            {
                                "output_index": 0,
                                "item_id": item["id"],
                                "content_index": 0,
                                "delta": chunk,
                            },
                        )
                        time.sleep(0.04)
                    item = {
                        **item,
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": "".join(chunks), "annotations": []}
                        ],
                    }
                self.event("response.output_item.done", {"output_index": 0, "item": item})
                self.event(
                    "response.completed",
                    {
                        "response": {
                            **base,
                            "status": "completed",
                            "output": [item],
                            "usage": {
                                "input_tokens": 20,
                                "output_tokens": 5,
                                "total_tokens": 25,
                                "input_tokens_details": {"cached_tokens": 0},
                                "output_tokens_details": {"reasoning_tokens": 0},
                            },
                        }
                    },
                )

            def anthropic(self, body):
                mid = "msg_" + uuid.uuid4().hex[:12]
                self.event(
                    "message_start",
                    {
                        "message": {
                            "id": mid,
                            "type": "message",
                            "role": "assistant",
                            "model": body.get("model"),
                            "content": [],
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 20, "output_tokens": 0},
                        }
                    },
                )
                tool = self.pick_tool(body)
                if tool:
                    self.event(
                        "content_block_start",
                        {
                            "index": 0,
                            "content_block": {
                                "type": "tool_use",
                                "id": "tool_" + mid,
                                "name": tool["name"],
                                "input": {},
                            },
                        },
                    )
                    self.event(
                        "content_block_delta",
                        {"index": 0, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
                    )
                else:
                    self.event(
                        "content_block_start",
                        {"index": 0, "content_block": {"type": "text", "text": ""}},
                    )
                    chunks = ["Synthetic ", "explanation."]
                    if "CWYC_WAIT" in json.dumps(body):
                        chunks = ["waiting "] * 300
                    for chunk in chunks:
                        self.event(
                            "content_block_delta",
                            {"index": 0, "delta": {"type": "text_delta", "text": chunk}},
                        )
                        time.sleep(0.04)
                self.event("content_block_stop", {"index": 0})
                self.event(
                    "message_delta",
                    {
                        "delta": {
                            "stop_reason": "tool_use" if tool else "end_turn",
                            "stop_sequence": None,
                        },
                        "usage": {"output_tokens": 5},
                    },
                )
                self.event("message_stop", {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def pi_probe(binary: str, root: Path, model: ModelServer, mcp: McpServer) -> dict[str, Any]:
    root.mkdir(parents=True)
    env = env_for(root)
    write_json(root / "pi" / "settings.json", {"enableSkillCommands": True})
    write_json(
        root / "pi" / "models.json",
        {
            "providers": {
                "cwyc-eval": {
                    "baseUrl": model.url + "/v1",
                    "api": "openai-responses",
                    "apiKey": "synthetic-only",
                    "models": [
                        {
                            "id": "eval",
                            "name": "Synthetic",
                            "input": ["text", "image"],
                            "contextWindow": 32000,
                            "maxTokens": 500,
                            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                        }
                    ],
                }
            }
        },
    )
    write_json(
        root / "pi" / "mcp.json",
        {
            "mcpServers": {
                "anki": {
                    "type": "http",
                    "url": mcp.url,
                    "headers": {"Authorization": f"Bearer {mcp.token}"},
                    "exposure": "direct",
                }
            }
        },
    )
    skill = root / "pi" / "skills" / "synthetic-study" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: synthetic-study\ndescription: Synthetic card explanation exercise.\n---\nUse a concise explanation.\n"
    )
    cmd = [
        binary,
        "--mode",
        "rpc",
        "--offline",
        "--no-context-files",
        "--no-extensions",
        "-e",
        "builtin:mcp",
        "--no-builtin-tools",
        "--provider",
        "cwyc-eval",
        "--model",
        "eval",
        "--session-dir",
        str(root / "sessions"),
    ]
    p = Process(cmd, root, env, "pi")
    result: dict[str, Any] = {}
    try:
        state = p.pi("get_state")
        result["startup"] = True
        commands = p.pi("get_commands").get("commands", [])
        result["skill_discovery"] = any(c.get("name") == "skill:synthetic-study" for c in commands)
        p.pi("prompt", message="/skill:synthetic-study CWYC_EXPLAIN")
        p.wait(lambda e: e.get("type") == "agent_end")
        state = p.pi("get_state")
        result["streaming"] = any(e.get("type") == "message_update" for e in p.seen)
        result["native_history"] = state.get("messageCount", 0) >= 2
        original_session = state["sessionFile"]
        before = state["sessionId"]
        p.pi("clone")
        forked = p.pi("get_state")
        result["head_fork"] = (
            before != forked["sessionId"] and forked["messageCount"] == state["messageCount"]
        )
        eligible = p.pi("get_fork_messages")["messages"]
        old_id = forked["sessionId"]
        p.pi("fork", entryId=eligible[0]["entryId"])
        rewound = p.pi("get_state")
        retained = p.pi("get_messages")["messages"]
        result["historical_fork"] = rewound["sessionId"] != old_id and not any(
            message.get("role") in ("user", "assistant") for message in retained
        )
        p.pi("new_session")
        p.pi("prompt", message="CWYC_TOOL_PROBE")
        p.wait(lambda e: e.get("type") == "agent_end")
        result["mcp_tool_execution"] = any(e.get("type") == "tool_execution_end" for e in p.seen)
        p.pi("new_session")
        p.pi("prompt", message="CWYC_WAIT")
        p.wait(lambda e: e.get("type") == "message_update")
        p.pi("abort")
        result["interrupt"] = not p.pi("get_state").get("isStreaming")
        p.pi("new_session")
        result["new_session_clears_history"] = p.pi("get_state").get("messageCount") == 0
        skill.write_text(
            skill.read_text().replace(
                "Synthetic card explanation exercise.", "Revised synthetic guidance."
            )
        )
        p.pi("new_session")
        result["new_session_refreshes_skill_metadata"] = (
            "Revised synthetic guidance." in json.dumps(p.pi("get_commands"))
        )
    finally:
        p.close()
    resumed = Process([*cmd, "--session", original_session], root, env, "pi-resumed")
    try:
        restored = resumed.pi("get_state")
        result["restart_resume"] = restored["sessionId"] == before and restored["messageCount"] >= 2
        result["restart_refreshes_skills"] = "Revised synthetic guidance." in json.dumps(
            resumed.pi("get_commands")
        )
    finally:
        resumed.close()
    return result


def codex_probe(binary: str, root: Path, model: ModelServer, mcp: McpServer) -> dict[str, Any]:
    root.mkdir(parents=True)
    env = env_for(root)
    config = f'''model = "eval"
model_provider = "cwyc_eval"
[model_providers.cwyc_eval]
name = "Synthetic loopback"
base_url = "{model.url}/v1"
wire_api = "responses"
requires_openai_auth = false
[mcp_servers.anki]
url = "{mcp.url}"
http_headers = {{ Authorization = "Bearer {mcp.token}" }}
'''
    (root / "codex" / "config.toml").write_text(config)
    skill = root / ".agents" / "skills" / "synthetic-study" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: synthetic-study\ndescription: Synthetic card explanation exercise.\n---\nUse a concise explanation.\n"
    )
    p = Process([binary, "app-server", "--listen", "stdio://"], root, env, "codex")
    result: dict[str, Any] = {}
    try:
        p.rpc(
            "initialize",
            {
                "clientInfo": {
                    "name": "cwyc_eval",
                    "title": "Synthetic CWYC evaluation",
                    "version": "0.0.0",
                }
            },
        )
        p.send({"method": "initialized", "params": {}})
        result["startup"] = True
        skills = p.rpc("skills/list", {"cwds": [str(root)], "forceReload": True})
        result["skill_discovery"] = "synthetic-study" in json.dumps(skills)
        thread = p.rpc(
            "thread/start", {"cwd": str(root), "approvalPolicy": "never", "sandbox": "read-only"}
        )["thread"]["id"]
        turn = p.rpc(
            "turn/start", {"threadId": thread, "input": [{"type": "text", "text": "CWYC_EXPLAIN"}]}
        )
        completed = p.wait(lambda e: e.get("method") == "turn/completed")
        result["completed_turn"] = completed["params"]["turn"]["status"] == "completed"
        result["streaming"] = any(e.get("method") == "item/agentMessage/delta" for e in p.seen)
        result["head_fork"] = (
            p.rpc("thread/fork", {"threadId": thread, "excludeTurns": True})["thread"]["id"]
            != thread
        )
        result["resume"] = (
            p.rpc("thread/resume", {"threadId": thread, "excludeTurns": True})["thread"]["id"]
            == thread
        )
        p.rpc(
            "turn/start",
            {"threadId": thread, "input": [{"type": "text", "text": "CWYC_LATER_TURN"}]},
        )
        p.wait(lambda e: e.get("method") == "turn/completed")
        historical = p.rpc(
            "thread/fork",
            {"threadId": thread, "lastTurnId": turn["turn"]["id"], "excludeTurns": True},
        )
        fork_history = p.rpc(
            "thread/read", {"threadId": historical["thread"]["id"], "includeTurns": True}
        )["thread"]["turns"]
        original_history = p.rpc("thread/read", {"threadId": thread, "includeTurns": True})[
            "thread"
        ]["turns"]
        result["historical_fork"] = (
            historical["thread"]["id"] != thread
            and len(fork_history) == 1
            and len(original_history) == 2
            and "CWYC_EXPLAIN" in json.dumps(fork_history)
            and "CWYC_LATER_TURN" not in json.dumps(fork_history)
            and "CWYC_LATER_TURN" in json.dumps(original_history)
        )
        tooling = p.rpc(
            "thread/start", {"cwd": str(root), "approvalPolicy": "never", "sandbox": "read-only"}
        )["thread"]["id"]
        p.rpc(
            "turn/start",
            {"threadId": tooling, "input": [{"type": "text", "text": "CWYC_TOOL_PROBE"}]},
        )
        p.wait(lambda e: e.get("method") == "turn/completed")
        result["mcp_tool_execution"] = any(
            e.get("method") == "item/completed"
            and e.get("params", {}).get("item", {}).get("type") == "mcpToolCall"
            for e in p.seen
        )
        skill.write_text(
            skill.read_text().replace(
                "Synthetic card explanation exercise.", "Revised synthetic guidance."
            )
        )
        result["force_reload_skills"] = "Revised synthetic guidance." in json.dumps(
            p.rpc("skills/list", {"cwds": [str(root)], "forceReload": True})
        )
        # Test interruption in a clean branch, avoiding the prior marker.
        branch = p.rpc(
            "thread/start", {"cwd": str(root), "approvalPolicy": "never", "sandbox": "read-only"}
        )["thread"]["id"]
        active = p.rpc(
            "turn/start", {"threadId": branch, "input": [{"type": "text", "text": "CWYC_WAIT"}]}
        )
        p.wait(lambda e: e.get("method") == "item/agentMessage/delta")
        p.rpc("turn/interrupt", {"threadId": branch, "turnId": active["turn"]["id"]})
        stopped = p.wait(lambda e: e.get("method") == "turn/completed")
        result["interrupt"] = stopped["params"]["turn"]["status"] == "interrupted"
    finally:
        p.close()
    return result


def claude_probe(binary: str, root: Path, model: ModelServer, mcp: McpServer) -> dict[str, Any]:
    root.mkdir(parents=True)
    env = env_for(root)
    env.update(
        ANTHROPIC_API_KEY="synthetic-only", ANTHROPIC_BASE_URL=model.url, ENABLE_TOOL_SEARCH="false"
    )
    skill = root / ".claude" / "skills" / "synthetic-study" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: synthetic-study\ndescription: Synthetic card explanation exercise.\n---\nUse a concise explanation.\n"
    )
    mcp_config = root / "mcp.json"
    write_json(
        mcp_config,
        {
            "mcpServers": {
                "anki": {
                    "type": "http",
                    "url": mcp.url,
                    "headers": {"Authorization": f"Bearer {mcp.token}"},
                }
            }
        },
    )
    command = [
        binary,
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--model",
        "claude-sonnet-4-6",
        "--setting-sources",
        "user,project,local",
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp_config),
        "--allowedTools",
        "mcp__anki,Read,Skill",
        "--disallowedTools",
        "Bash,Edit,Write,NotebookEdit",
    ]

    def prompt(process, text):
        process.send(
            {
                "type": "user",
                "message": {"role": "user", "content": text},
                "parent_tool_use_id": None,
            }
        )
        return process.wait(lambda e: e.get("type") == "result", timeout=25)

    p = Process(command, root, env, "claude")
    result: dict[str, Any] = {}
    try:
        completed = prompt(p, "CWYC_EXPLAIN")
        result["completed_turn"] = not completed.get("is_error", True)
        session_id = completed["session_id"]
        initialization = next(
            e for e in p.seen if e.get("type") == "system" and e.get("subtype") == "init"
        )
        result["skill_discovery"] = "synthetic-study" in json.dumps(initialization)
        result["streaming"] = any(e.get("type") == "stream_event" for e in p.seen)
        tool_completed = prompt(p, "CWYC_TOOL_PROBE")
        result["mcp_tool_execution"] = not tool_completed.get("is_error", True) and any(
            e.get("type") == "assistant"
            and any(c.get("type") == "tool_use" for c in e.get("message", {}).get("content", []))
            for e in p.seen
        )
        p.send(
            {
                "type": "user",
                "message": {"role": "user", "content": "CWYC_WAIT"},
                "parent_tool_use_id": None,
            }
        )
        p.wait(
            lambda e: (
                e.get("type") == "stream_event"
                and e.get("event", {}).get("type") == "content_block_delta"
            )
        )
        p.send(
            {
                "type": "control_request",
                "request_id": "synthetic-interrupt",
                "request": {"subtype": "interrupt"},
            }
        )
        p.wait(lambda e: e.get("type") == "control_response")
        result["interrupt_acknowledged"] = True
    finally:
        p.close()
    forked = Process([*command, "--resume", session_id, "--fork-session"], root, env, "claude-fork")
    try:
        completed = prompt(forked, "CWYC_FORK")
        result["head_fork"] = completed["session_id"] != session_id and not completed.get(
            "is_error", True
        )
    finally:
        forked.close()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime", choices=["pi", "codex", "claude"], required=True)
    parser.add_argument("--binary", required=True)
    args = parser.parse_args()
    binary = str(Path(shutil.which(args.binary) or args.binary).resolve())
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    calls: list[str] = []
    model = ModelServer()
    mcp = McpServer(
        tool_specs=[
            {
                "name": "search_notes",
                "description": "Search the synthetic collection.",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": True},
            }
        ],
        execute_tool=lambda name, _args: (
            calls.append(name) or {"notes": [{"id": 1, "front": "Synthetic prerequisite"}]}
        ),
    )
    mcp.start()
    try:
        probe = {"pi": pi_probe, "codex": codex_probe, "claude": claude_probe}[args.runtime]
        result = probe(binary, args.output / "run", model, mcp)
        write_json(
            args.output / "result.json",
            {
                "runtime": args.runtime,
                "checks": result,
                "synthetic_model_requests": len(model.requests),
                "mcp_calls": calls,
            },
        )
        print(
            json.dumps(
                {
                    "runtime": args.runtime,
                    "checks": result,
                    "synthetic_model_requests": len(model.requests),
                    "mcp_calls": calls,
                }
            )
        )
        if not all(result.values()):
            raise SystemExit(1)
    finally:
        write_json(args.output / "model-requests.json", model.requests)
        mcp.stop()
        model.close()


if __name__ == "__main__":
    main()
