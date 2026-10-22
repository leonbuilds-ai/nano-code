"""Local, single-user HTTP interface for NanoCode."""

from __future__ import annotations

import asyncio
import json
import secrets
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .config import Settings
from .engine import Engine


class NewSession(BaseModel):
    title: str = Field(default="New session", max_length=120)


class RenameSession(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class NewRun(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)


class ApprovalDecision(BaseModel):
    approved: bool


@dataclass
class Run:
    id: str
    session_id: str
    events: deque = field(default_factory=lambda: deque(maxlen=300))
    next_id: int = 1
    done: bool = False
    task: asyncio.Task | None = None
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def emit(self, kind: str, data: dict) -> None:
        self.events.append((self.next_id, kind, data))
        self.next_id += 1
        self.changed.set()


def _local_host(value: str) -> bool:
    try:
        parsed = urlsplit("//" + value)
        host = parsed.hostname
    except ValueError:
        return False
    return host in {"localhost", "127.0.0.1", "::1"} and not (parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = Engine(settings)
    runs: dict[str, Run] = {}
    active: dict[str, str] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            tasks = [run.task for run in runs.values() if run.task and not run.task.done()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            engine.store.close()

    app = FastAPI(title="NanoCode", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.engine = engine
    app.state.runs = runs

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if not _local_host(request.headers.get("host", "")):
            return JSONResponse({"detail": "Local host required"}, status_code=403)
        origin = request.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or not _local_host(parsed.netloc):
                return JSONResponse({"detail": "Local origin required"}, status_code=403)
            if parsed.netloc.lower() != request.headers["host"].lower():
                return JSONResponse({"detail": "Origin mismatch"}, status_code=403)
        return await call_next(request)

    def session_or_404(session_id: str):
        try:
            session = engine.store.get_session(session_id)
        except (KeyError, ValueError):
            session = None
        if session is None:
            raise HTTPException(404, "Session not found")
        return session

    def run_or_404(run_id: str) -> Run:
        run = runs.get(run_id)
        if run is None:
            raise HTTPException(404, "Run not found")
        return run

    @app.get("/api/status")
    def status():
        return {"ok": True, "model": settings.model, "workspace": settings.workspace.name}

    @app.get("/api/sessions")
    def list_sessions():
        return engine.store.list_sessions()

    @app.post("/api/sessions", status_code=201)
    def create_session(payload: NewSession):
        return engine.store.create_session(payload.title.strip() or "New session")

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str):
        return session_or_404(session_id)

    @app.patch("/api/sessions/{session_id}")
    def rename_session(session_id: str, payload: RenameSession):
        session_or_404(session_id)
        title = payload.title.strip()
        if not title:
            raise HTTPException(422, "Title cannot be empty")
        engine.store.rename_session(session_id, title)
        return engine.store.get_session(session_id)

    async def execute(run: Run, prompt: str):
        async def approve(request: dict) -> bool:
            approval_id = secrets.token_urlsafe(12)
            future = asyncio.get_running_loop().create_future()
            run.pending[approval_id] = future
            run.emit("approval", {"approval_id": approval_id, "request": request})
            try:
                return await asyncio.wait_for(future, timeout=300)
            except asyncio.TimeoutError:
                return False
            finally:
                run.pending.pop(approval_id, None)

        try:
            async for event in engine.run(run.session_id, prompt, approve):
                if not isinstance(event, dict):
                    continue
                kind = str(event.get("type", "status"))
                data = event.get("data", {})
                if kind == "approval":
                    continue  # The approval callback emits the actionable, bound ID.
                if kind == "error":
                    data = {"message": "运行失败，请查看本地服务日志。"}
                if kind == "done":
                    data = {"session_id": run.session_id}
                run.emit(kind, data if isinstance(data, dict) else {"value": data})
            if not any(kind == "done" for _, kind, _ in run.events):
                run.emit("done", {"session_id": run.session_id})
        except asyncio.CancelledError:
            run.emit("error", {"message": "运行已停止"})
        except Exception:
            run.emit("error", {"message": "运行失败，请查看本地服务日志。"})
        finally:
            for future in run.pending.values():
                if not future.done():
                    future.cancel()
            run.pending.clear()
            run.done = True
            active.pop(run.session_id, None)
            run.changed.set()
            completed = [key for key, item in runs.items() if item.done]
            for key in completed[:-100]:
                runs.pop(key, None)

    @app.post("/api/sessions/{session_id}/runs", status_code=201)
    async def start_run(session_id: str, payload: NewRun):
        session_or_404(session_id)
        if session_id in active:
            raise HTTPException(409, "Session already has an active run")
        if len(active) >= 20:
            raise HTTPException(503, "Too many active runs")
        run = Run(id=secrets.token_urlsafe(18), session_id=session_id)
        runs[run.id] = run
        active[session_id] = run.id
        run.task = asyncio.create_task(execute(run, payload.prompt))
        return {"id": run.id, "session_id": session_id}

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request):
        run = run_or_404(run_id)
        cursor = request.headers.get("last-event-id") or request.query_params.get("after", "0")
        try:
            after = max(0, int(cursor))
        except ValueError:
            raise HTTPException(400, "Invalid event cursor")

        async def stream():
            nonlocal after
            while True:
                if await request.is_disconnected():
                    break
                run.changed.clear()
                if run.events and after < run.events[0][0] - 1:
                    after = run.events[0][0] - 1
                    yield f"event: reset\ndata: {json.dumps({'session_id': run.session_id})}\n\n"
                available = [(n, kind, data) for n, kind, data in run.events if n > after]
                for n, kind, data in available:
                    after = n
                    yield f"id: {n}\nevent: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                if run.done:
                    break
                try:
                    await asyncio.wait_for(run.changed.wait(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/runs/{run_id}/approvals/{approval_id}")
    async def decide(run_id: str, approval_id: str, payload: ApprovalDecision):
        run = run_or_404(run_id)
        future = run.pending.get(approval_id)
        if future is None or future.done():
            raise HTTPException(409, "Approval is no longer pending")
        future.set_result(payload.approved)
        return {"accepted": True}

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel(run_id: str):
        run = run_or_404(run_id)
        if run.done or run.task is None:
            raise HTTPException(409, "Run is not active")
        run.task.cancel()
        return {"cancelled": True}

    checkout_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    packaged_dist = Path(__file__).resolve().parent / "static"
    dist = checkout_dist if checkout_dist.is_dir() else packaged_dist

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        target = (dist / path).resolve()
        if dist.exists() and target.is_relative_to(dist.resolve()) and target.is_file():
            return FileResponse(target)
        index = dist / "index.html"
        if index.is_file():
            return FileResponse(index)
        raise HTTPException(404, "Frontend is not built")

    return app
