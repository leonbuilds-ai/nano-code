import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import pytest

from nanocode import Engine, Settings
from nanocode.security import SecurityError, safe_path
from nanocode.tools import ToolRegistry


def test_safe_path_blocks_private_and_symlink_escape(tmp_path):
    (tmp_path / "outside").mkdir()
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / ".env").write_text("private")
    for name in ("../outside", ".env", ".git/config", "secret.pem"):
        try:
            safe_path(workspace, name)
        except SecurityError:
            pass
        else:
            raise AssertionError(f"Path should be blocked: {name}")
    link = workspace / "link"
    if os.name == "nt":
        # Junctions exercise Windows reparse-point resolution without requiring
        # the optional symbolic-link privilege.
        import _winapi
        _winapi.CreateJunction(str(tmp_path / "outside"), str(link))
    else:
        link.symlink_to(tmp_path / "outside", target_is_directory=True)
    try:
        safe_path(workspace, "link/file")
    except SecurityError:
        pass
    else:
        raise AssertionError("Symlink escape should be blocked")


def test_write_requires_approval(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    class Store:
        pass
    tools = ToolRegistry(workspace, tmp_path / "data", Store())
    reviewed = []

    async def deny(action):
        reviewed.append(action)
        return False

    async def scenario():
        try:
            await tools.execute("write_file", {"path": "hello.txt", "content": "ok"}, deny)
        except SecurityError:
            pass
        else:
            raise AssertionError("Denied write succeeded")

    asyncio.run(scenario())
    assert len(reviewed) == 1
    assert reviewed[0]["tool"] == "write_file"
    assert reviewed[0]["arguments"] == {"path": "hello.txt", "content": "ok"}
    assert reviewed[0]["review"]["level"] in {"low", "medium", "high"}
    assert not (workspace / "hello.txt").exists()


def test_streaming_engine_tool_roundtrip(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "note.txt").write_text("hello from file", encoding="utf-8")
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(request)
            if len(received) == 1:
                chunks = [
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"note'}}]}}]},
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '.txt"}'}}]}}]},
                ]
            else:
                chunks = [{"choices": [{"delta": {"content": "The note says hello."}}]}]
            body = "".join("data: " + json.dumps(item) + "\n\n" for item in chunks) + "data: [DONE]\n\n"
            encoded = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = Settings(workspace, tmp_path / "data", base_url=f"http://127.0.0.1:{server.server_port}/v1")

    async def approve(action):
        raise AssertionError("Read tool should not request approval")

    async def scenario():
        return [event async for event in Engine(settings).run("", "Read the note", approve)]

    try:
        events = asyncio.run(scenario())
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert [event["type"] for event in events] == ["status", "tool_start", "tool_result", "delta", "done"]
    assert events[2]["data"]["result"] == "hello from file"
    assert received[1]["messages"][-1]["role"] == "tool"
    assert "untrusted_tool_result" in received[1]["messages"][-1]["content"]


def test_cancel_during_approval_keeps_history_valid(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    engine = Engine(Settings(workspace, tmp_path / "data"))

    class ScriptedProvider:
        async def stream(self, messages, tools):
            yield {"type": "tool", "call": {"index": 0, "id": "write-1", "function": {
                "name": "write_file", "arguments": '{"path":"new.txt","content":"ok"}'}}}

    engine.provider = ScriptedProvider()

    async def approve(action):
        raise AssertionError("Approval should not be reached after cancellation")

    async def scenario():
        stream = engine.run("", "Write a file", approve)
        events = []
        async for event in stream:
            events.append(event)
            if event["type"] == "approval":
                await stream.aclose()
                break
        return events

    events = asyncio.run(scenario())
    session_id = events[0]["data"]["session_id"]
    assert [m["role"] for m in engine.store.get_session(session_id)["messages"]] == ["user"]
    assert not (workspace / "new.txt").exists()


def test_read_only_delegate_rejects_write_even_if_model_requests_it(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            messages = request["messages"]
            if len(messages) == 3:  # child: system, context, user
                delta = {"tool_calls": [{"index": 0, "id": "bad-write", "function": {
                    "name": "write_file", "arguments": '{"path":"bad.txt","content":"bad"}'}}]}
            else:
                delta = {"content": "Done."}
            encoded = ("data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\ndata: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = Settings(workspace, tmp_path / "data", base_url=f"http://127.0.0.1:{server.server_port}/v1")
    engine = Engine(settings)

    async def approve(action):
        raise AssertionError("Read-only delegate must reject before approval")

    async def scenario():
        return await engine._delegate({"mode": "fork", "task": "Try writing a file"}, approve, [0])

    try:
        result = asyncio.run(scenario())
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert "Done." in result
    assert not (workspace / "bad.txt").exists()
    assert any("Read-only delegate" in m.get("content", "") for req in requests for m in req["messages"] if m["role"] == "tool")


def test_worktree_delegate_uses_clean_branch_and_preserves_dirty_source(tmp_path):
    git = shutil.which("git")
    assert git, "Git is required for worktree integration tests"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run([git, "init", "-q", str(workspace)], check=True)
    subprocess.run([git, "-C", str(workspace), "config", "user.name", "Test"], check=True)
    subprocess.run([git, "-C", str(workspace), "config", "user.email", "test@example.invalid"], check=True)
    (workspace / "file.txt").write_text("committed", encoding="utf-8")
    subprocess.run([git, "-C", str(workspace), "add", "file.txt"], check=True)
    subprocess.run([git, "-C", str(workspace), "commit", "-qm", "initial"], check=True)
    (workspace / "file.txt").write_text("dirty", encoding="utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            encoded = b'data: {"choices":[{"delta":{"content":"Inspected."}}]}\n\ndata: [DONE]\n\n'
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    engine = Engine(Settings(workspace, tmp_path / "data", base_url=f"http://127.0.0.1:{server.server_port}/v1"))
    approvals = []

    async def approve(action):
        approvals.append(action)
        return True

    async def scenario():
        return await engine._delegate({"mode": "worktree", "task": "Inspect source"}, approve, [0])

    try:
        result = json.loads(asyncio.run(scenario()))
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert approvals[0]["tool"] == "delegate_worktree"
    assert approvals[0]["review"]["level"] == "high"
    assert Path(result["worktree"]).joinpath("file.txt").read_text(encoding="utf-8") == "committed"
    assert (workspace / "file.txt").read_text(encoding="utf-8") == "dirty"
    assert result["branch"].startswith("codex/delegate-")
    assert result["merged"] is False


def test_team_runs_children_in_parallel_with_bounded_count(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    barrier = threading.Barrier(2, timeout=3)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            task = request["messages"][-1]["content"]
            barrier.wait()
            encoded = ("data: " + json.dumps({"choices": [{"delta": {"content": task}}]}) + "\n\ndata: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    engine = Engine(Settings(workspace, tmp_path / "data", base_url=f"http://127.0.0.1:{server.server_port}/v1"))
    count = [0]

    async def approve(action):
        raise AssertionError("Read-only team should not need approval")

    async def scenario():
        output = await engine._delegate({"mode": "team", "tasks": ["Alpha", "Beta"]}, approve, count)
        assert count == [2]
        with pytest.raises(ValueError, match="limit"):
            await engine._delegate({"mode": "team", "tasks": ["Gamma", "Delta"]}, approve, count)
        return output

    try:
        result = asyncio.run(scenario())
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert "Alpha" in result and "Beta" in result
