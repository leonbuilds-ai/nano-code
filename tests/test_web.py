import time

from fastapi.testclient import TestClient

from nanocode.config import Settings
from nanocode.knowledge import KnowledgeStore
import nanocode.web as web


class ScriptedEngine:
    def __init__(self, settings):
        self.store = KnowledgeStore(settings.data_dir)

    async def run(self, session_id, prompt, approve):
        self.store.append_message(session_id, {"role": "user", "content": prompt})
        if prompt == "overflow":
            for _ in range(305):
                yield {"type": "delta", "data": {"text": "x"}}
            yield {"type": "done", "data": {"session_id": session_id}}
            return
        yield {"type": "delta", "data": {"text": "working"}}
        if prompt == "approve":
            accepted = await approve({"tool": "write_file", "arguments": {"path": "a.txt"}})
            yield {"type": "tool_result", "data": {"result": "accepted" if accepted else "denied"}}
        self.store.append_message(session_id, {"role": "assistant", "content": "done"})
        yield {"type": "done", "data": {"session_id": session_id}}


def make_client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "Engine", ScriptedEngine)
    settings = Settings(workspace=tmp_path, data_dir=tmp_path / "data")
    return TestClient(web.create_app(settings), base_url="http://localhost:8865")


def wait_until(predicate):
    for _ in range(100):
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out")


def test_sessions_run_and_sse_replay(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        session = client.post("/api/sessions", json={"title": "First"}).json()
        sid = session["id"]
        assert client.get("/api/sessions").json()[0]["title"] == "First"
        assert client.patch(f"/api/sessions/{sid}", json={"title": "Renamed"}).json()["title"] == "Renamed"
        run_id = client.post(f"/api/sessions/{sid}/runs", json={"prompt": "hello"}).json()["id"]
        wait_until(lambda: client.app.state.runs[run_id].done)
        events = client.get(f"/api/runs/{run_id}/events").text
        assert "event: delta" in events and "event: done" in events
        assert "event: delta" not in client.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": "1"}).text
        assert client.get(f"/api/sessions/{sid}").json()["messages"][-1]["content"] == "done"


def test_pending_approval_once_and_cancel(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        run_id = client.post(f"/api/sessions/{sid}/runs", json={"prompt": "approve"}).json()["id"]
        run = client.app.state.runs[run_id]
        wait_until(lambda: bool(run.pending))
        approval_id = next(iter(run.pending))
        assert client.post(f"/api/sessions/{sid}/runs", json={"prompt": "again"}).status_code == 409
        endpoint = f"/api/runs/{run_id}/approvals/{approval_id}"
        assert client.post(endpoint, json={"approved": True}).status_code == 200
        assert client.post(endpoint, json={"approved": True}).status_code == 409
        wait_until(lambda: run.done)
        assert "accepted" in client.get(f"/api/runs/{run_id}/events").text
        next_id = client.post(f"/api/sessions/{sid}/runs", json={"prompt": "approve"}).json()["id"]
        next_run = client.app.state.runs[next_id]
        wait_until(lambda: bool(next_run.pending))
        assert client.post(f"/api/runs/{next_id}/cancel").status_code == 200
        wait_until(lambda: next_run.done)
        assert client.post(f"/api/runs/{next_id}/cancel").status_code == 409


def test_rejects_nonlocal_host_and_origin(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        assert client.get("/api/status", headers={"host": "example.com"}).status_code == 403
        assert client.post("/api/sessions", json={}, headers={"origin": "https://evil.example"}).status_code == 403
        assert client.post("/api/sessions", json={}, headers={"origin": "http://localhost:9999"}).status_code == 403
        assert client.get("/api/status").json()["ok"] is True


def test_event_retention_signals_replay_gap(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        run_id = client.post(f"/api/sessions/{sid}/runs", json={"prompt": "overflow"}).json()["id"]
        wait_until(lambda: client.app.state.runs[run_id].done)
        response = client.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": "1"}).text
        assert "event: reset" in response
        assert f'"session_id": "{sid}"' in response
