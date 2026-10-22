"""Workspace tools. Approval is an authorization step, not a command sandbox."""

import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
from .security import PRIVATE_NAMES, SecurityError, audit, safe_path
from .risk import RiskReview


def _spec(name: str, description: str, properties: dict, required: list[str]):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required,
                           "additionalProperties": False}}}


S = {"type": "string"}
SCHEMAS = [
    _spec("read_file", "Read a workspace text file", {"path": S}, ["path"]),
    _spec("list_files", "List workspace files", {"path": S}, ["path"]),
    _spec("search_text", "Search text in workspace files", {"query": S, "path": S}, ["query"]),
    _spec("write_file", "Write a workspace text file", {"path": S, "content": S}, ["path", "content"]),
    _spec("run_command", "Run an approved command using an argv array; no shell", {"argv": {"type": "array", "items": S}}, ["argv"]),
    _spec("recall_output", "Recall a slice of a prior large tool output", {"ref": S, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, ["ref"]),
    _spec("remember", "Save a memory note", {"text": S, "kind": {"type": "string", "enum": ["profile", "episodic", "procedural"]}}, ["text"]),
    _spec("search_memory", "Search saved memory notes", {"query": S}, ["query"]),
    _spec("route_skills", "Find local skill instructions relevant to a task", {"query": S}, ["query"]),
    _spec("delegate", "Delegate bounded tasks", {"task": S, "tasks": {"type": "array", "items": S, "maxItems": 3}, "mode": {"type": "string", "enum": ["fork", "team", "worktree"]}}, ["mode"]),
]


class ToolRegistry:
    def __init__(self, workspace: Path, data_dir: Path, store, *, read_only: bool = False):
        self.workspace = workspace.resolve(strict=True)
        self.data_dir = data_dir
        self.store = store
        self.read_only = read_only

    def schemas(self):
        if self.read_only:
            return [s for s in SCHEMAS if s["function"]["name"] not in {"write_file", "run_command", "remember", "delegate"}]
        return SCHEMAS

    def _path(self, name, write=False):
        path = safe_path(self.workspace, name, for_write=write)
        data_dir = self.data_dir.resolve(strict=False)
        if data_dir.is_relative_to(self.workspace) and path.is_relative_to(data_dir):
            raise SecurityError("Agent data directory is blocked")
        return path

    async def execute(self, name: str, args: dict, approve, *, review=None, classifier=None):
        if not isinstance(args, dict):
            raise ValueError("Tool arguments must be an object")
        if self.read_only and name in {"write_file", "run_command", "remember", "delegate"}:
            raise SecurityError("Read-only delegate cannot use this tool")
        action = {"tool": name, "arguments": args}
        try:
            if name == "read_file":
                path = self._path(args["path"])
                result = path.read_text(encoding="utf-8")[:100_000]
            elif name == "list_files":
                path = self._path(args.get("path", "."))
                if not path.is_dir():
                    raise ValueError("Directory expected")
                files = []
                for child in path.iterdir():
                    try:
                        self._path(str(child))
                    except SecurityError:
                        continue
                    files.append({"name": child.name, "directory": child.is_dir()})
                    if len(files) >= 500:
                        break
                result = json.dumps(files, ensure_ascii=False)
            elif name == "search_text":
                root = self._path(args.get("path", "."))
                query = args["query"]
                if not isinstance(query, str) or not query:
                    raise ValueError("Nonempty query required")
                matches = []
                if root.is_file():
                    paths = [root]
                else:
                    paths = []
                    for folder, dirs, files in os.walk(root, followlinks=False):
                        allowed_dirs = []
                        for name in dirs:
                            candidate = Path(folder) / name
                            if name.lower() in PRIVATE_NAMES or candidate.is_symlink():
                                continue
                            try:
                                self._path(str(candidate))
                            except SecurityError:
                                continue
                            allowed_dirs.append(name)
                        dirs[:] = allowed_dirs
                        for name in files:
                            paths.append(Path(folder) / name)
                            if len(paths) >= 1000:
                                break
                        if len(paths) >= 1000:
                            break
                for path in paths:
                    try:
                        self._path(str(path))
                        if not path.is_file() or path.stat().st_size > 1_000_000:
                            continue
                        lines = path.read_text(encoding="utf-8").splitlines()
                    except (SecurityError, UnicodeError, OSError):
                        continue
                    for number, line in enumerate(lines, 1):
                        if query.lower() in line.lower():
                            matches.append(f"{path.relative_to(self.workspace)}:{number}:{line[:300]}")
                            if len(matches) >= 100:
                                break
                    if len(matches) >= 100:
                        break
                result = "\n".join(matches)
            elif name == "write_file":
                path = self._path(args["path"], write=True)
                content = args["content"]
                if not isinstance(content, str) or len(content) > 2_000_000:
                    raise ValueError("Content must be text under 2 MB")
                # Recheck the resolved parent after the user has reviewed exact arguments.
                target_before = path
                await self._approve(action, approve, review, classifier)
                path = self._path(args["path"], write=True)
                if path != target_before:
                    raise SecurityError("Write target changed after approval")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                result = f"Wrote {len(content)} characters to {path.relative_to(self.workspace)}"
            elif name == "run_command":
                argv = args["argv"]
                if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x for x in argv):
                    raise ValueError("argv must be a nonempty string array")
                await self._approve(action, approve, review, classifier)
                kwargs = {"cwd": self.workspace, "stdout": asyncio.subprocess.PIPE,
                          "stderr": asyncio.subprocess.STDOUT, "shell": False}
                if os.name == "nt":
                    kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    kwargs["start_new_session"] = True
                process = await asyncio.create_subprocess_exec(*argv, **kwargs)
                try:
                    async def drain():
                        chunks = []
                        size = 0
                        while True:
                            part = await process.stdout.read(8192)
                            if not part:
                                break
                            if size < 100_000:
                                chunks.append(part[:100_000 - size])
                                size += len(chunks[-1])
                        await process.wait()
                        return b"".join(chunks)
                    output = await asyncio.wait_for(drain(), timeout=30)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    await self._stop_process(process)
                    raise
                result = f"exit={process.returncode}\n" + output.decode("utf-8", errors="replace")
            elif name == "recall_output":
                offset = int(args.get("offset", 0))
                limit = int(args.get("limit", 6000))
                if offset < 0 or not 1 <= limit <= 6000:
                    raise ValueError("offset must be nonnegative and limit 1..6000")
                result = self.store.read_external(args["ref"])[offset:offset + limit]
            elif name == "remember":
                result = self.store.remember(args.get("kind", "episodic"), args["text"])
            elif name == "search_memory":
                result = self.store.search_memory(args["query"])
            elif name == "route_skills":
                result = self.store.route_skills(args["query"])
            else:
                raise ValueError(f"Unknown tool: {name}")
            audit(self.data_dir, {**action, "ok": True, "result_chars": len(str(result))})
            return result
        except Exception as error:
            audit(self.data_dir, {**action, "ok": False, "error": str(error)})
            raise

    async def review(self, name: str, args: dict, classifier=None):
        try:
            return await RiskReview().assess(name, args, classifier=classifier)
        except Exception:
            return {"level": "high", "reason": "Risk review failed", "source": "error"}

    async def _approve(self, action, approve, review, classifier):
        action["review"] = review if review is not None else await self.review(action["tool"], action["arguments"], classifier)
        if not await approve(action):
            raise SecurityError("Approval denied")

    async def _stop_process(self, process):
        if process.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
