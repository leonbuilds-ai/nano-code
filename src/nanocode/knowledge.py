"""Persistent, explainable knowledge for the independent NanoCode engine.

Copyright (c) 2026 leonbuilds-ai. Licensed under the MIT License.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path


_WORDS = re.compile(r"[a-z0-9_]+|[\u3400-\u9fff]+", re.I)
_SECRET = re.compile(
    r"(?i)(api[_ -]?key|password|passwd|secret|token|private key|密码|密钥|令牌)"
    r"\s*[:=：是]\s*\S+|sk-[a-z0-9_-]{12,}|-----BEGIN [A-Z ]+PRIVATE KEY-----"
)
_REF = re.compile(r"^[0-9a-f]{64}$")


def _tokens(text: str) -> set[str]:
    result: set[str] = set()
    for word in _WORDS.findall(text.lower()):
        if len(word) > 1:
            result.add(word)
        if "\u3400" <= word[0] <= "\u9fff":
            result.update(word[i : i + 2] for i in range(len(word) - 1))
    return result


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _safe_text(text: str) -> bool:
    return bool(text.strip()) and not _SECRET.search(text)


class KnowledgeStore:
    def __init__(self, data_dir: Path, skills_dir: Path | None = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir = Path(skills_dir) if skills_dir else Path(__file__).resolve().parent / "resources" / "skills"
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.data_dir / "knowledge.sqlite3", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        with self._db:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(session_id, position)
                );
                CREATE TABLE IF NOT EXISTS outputs (
                    ref TEXT PRIMARY KEY, body TEXT NOT NULL, chars INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS summaries (
                    digest TEXT PRIMARY KEY, body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    body TEXT NOT NULL, source TEXT NOT NULL,
                    created_at REAL NOT NULL, UNIQUE(kind, body, source)
                );
                CREATE TABLE IF NOT EXISTS memory_terms (
                    term TEXT NOT NULL, memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                    PRIMARY KEY(term, memory_id)
                );
                CREATE INDEX IF NOT EXISTS memory_terms_by_memory ON memory_terms(memory_id);
                CREATE TABLE IF NOT EXISTS skill_candidates (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                    instructions TEXT NOT NULL, evidence TEXT NOT NULL,
                    status TEXT NOT NULL, created_at REAL NOT NULL
                );
            """)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def create_session(self, title: str = "New session") -> dict:
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must be nonempty")
        now = time.time()
        session_id = uuid.uuid4().hex
        with self._lock, self._db:
            self._db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (session_id, title.strip(), now, now))
        return {"id": session_id, "title": title.strip(), "created_at": now, "messages": []}

    def list_sessions(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("""
                SELECT s.*, COUNT(m.position) AS message_count FROM sessions s
                LEFT JOIN messages m ON m.session_id=s.id
                GROUP BY s.id ORDER BY s.updated_at DESC, s.id
            """).fetchall()
        return [dict(row) for row in rows]

    def get_session(self, session_id: str) -> dict:
        with self._lock:
            row = self._db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise KeyError(session_id)
            messages = self._db.execute(
                "SELECT body FROM messages WHERE session_id=? ORDER BY position", (session_id,)
            ).fetchall()
        return {**dict(row), "messages": [json.loads(item[0]) for item in messages]}

    def append_message(self, session_id: str, message: dict) -> None:
        if not isinstance(message, dict) or not isinstance(message.get("role"), str):
            raise ValueError("message must have a role")
        body = _dump(message)
        with self._lock, self._db:
            if self._db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is None:
                raise KeyError(session_id)
            position = self._db.execute(
                "SELECT COALESCE(MAX(position)+1,0) FROM messages WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            self._db.execute("INSERT INTO messages VALUES (?,?,?)", (session_id, position, body))
            self._db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), session_id))

    def rename_session(self, session_id: str, title: str) -> None:
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must be nonempty")
        with self._lock, self._db:
            result = self._db.execute(
                "UPDATE sessions SET title=?,updated_at=? WHERE id=?",
                (title.strip(), time.time(), session_id),
            )
            if result.rowcount == 0:
                raise KeyError(session_id)

    def externalize(self, text: str) -> dict:
        if not isinstance(text, str):
            raise TypeError("output must be text")
        ref = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO outputs VALUES (?,?,?)", (ref, text, len(text))
            )
        return {"ref": ref, "preview": text[:240], "chars": len(text)}

    def recall_output(self, ref: str, offset: int = 0, limit: int = 6000) -> str:
        if not isinstance(ref, str) or not _REF.fullmatch(ref):
            raise ValueError("invalid output reference")
        if not isinstance(offset, int) or not isinstance(limit, int) or offset < 0 or not 0 <= limit <= 6000:
            raise ValueError("invalid output slice")
        with self._lock:
            row = self._db.execute("SELECT body FROM outputs WHERE ref=?", (ref,)).fetchone()
        if row is None:
            raise KeyError(ref)
        if hashlib.sha256(row[0].encode("utf-8")).hexdigest() != ref:
            raise ValueError("output checksum mismatch")
        return row[0][offset : offset + limit]

    def read_external(self, ref: str) -> str:
        """Read a complete stored output after validating its reference and checksum."""
        if not isinstance(ref, str) or not _REF.fullmatch(ref):
            raise ValueError("invalid output reference")
        with self._lock:
            row = self._db.execute("SELECT body FROM outputs WHERE ref=?", (ref,)).fetchone()
        if row is None:
            raise KeyError(ref)
        if hashlib.sha256(row[0].encode("utf-8")).hexdigest() != ref:
            raise ValueError("output checksum mismatch")
        return row[0]

    @staticmethod
    def _groups(messages: list[dict]) -> list[list[dict]]:
        groups: list[list[dict]] = []
        for message in messages:
            role = message.get("role")
            if role == "tool":
                if groups and groups[-1][0].get("role") == "assistant":
                    calls = groups[-1][0].get("tool_calls") or []
                    expected = {call.get("id") for call in calls if isinstance(call, dict)}
                    if expected and message.get("tool_call_id") in expected:
                        groups[-1].append(message)
                continue
            groups.append([message])
        return [
            group for group in groups
            if not group[0].get("tool_calls") or
            {call.get("id") for call in group[0]["tool_calls"] if isinstance(call, dict)}
            == {item.get("tool_call_id") for item in group[1:]}
        ]

    def _bounded_group(self, group: list[dict], target: int) -> list[dict]:
        copy = json.loads(_dump(group))
        if len(_dump(copy)) <= target:
            return copy
        for message in copy:
            content = message.get("content")
            if isinstance(content, str) and content:
                ref = self.externalize(content)["ref"]
                message["content"] = f"[ref={ref}] {content[:96]}"
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                arguments = function.get("arguments")
                if isinstance(arguments, str) and arguments:
                    ref = self.externalize(arguments)["ref"]
                    function["arguments"] = _dump({"externalized_arguments_ref": ref})
        while len(_dump(copy)) > target:
            changed = False
            for message in copy:
                content = message.get("content")
                if isinstance(content, str) and content.startswith("[ref=") and len(content) > 71:
                    message["content"] = content[: max(71, len(content) - 32)]
                    changed = True
            if not changed:
                break
        return copy

    def compact(self, messages: list[dict], budget_chars: int = 24000) -> list[dict]:
        if budget_chars < 2:
            raise ValueError("budget too small")
        prefix: list[dict] = []
        position = 0
        while position < len(messages) and messages[position].get("role") == "system":
            prefix.append(messages[position])
            position += 1
        if len(_dump(prefix)) > budget_chars:
            raise ValueError("system prefix exceeds budget")
        groups = self._groups(messages[position:])
        if len(_dump(prefix + [item for group in groups for item in group])) <= budget_chars:
            return prefix + [item for group in groups for item in group]
        last_user = next((i for i in range(len(groups) - 1, -1, -1) if groups[i][0].get("role") == "user"), None)
        selected: dict[int, list[dict]] = {}
        if last_user is not None:
            reserve = budget_chars - len(_dump(prefix))
            if last_user != len(groups) - 1:
                reserve = max(120, reserve // 2)
            candidate = self._bounded_group(groups[last_user], reserve)
            if len(_dump(prefix + candidate)) <= budget_chars:
                selected[last_user] = candidate
        recent_start = last_user if last_user is not None else max(0, len(groups) - 1)
        for index in range(len(groups) - 1, recent_start - 1, -1):
            if index in selected:
                continue
            current = prefix + [item for key in sorted(selected) for item in selected[key]]
            candidate = self._bounded_group(groups[index], budget_chars - len(_dump(current)))
            trial_groups = selected | {index: candidate}
            trial = prefix + [item for key in sorted(trial_groups) for item in trial_groups[key]]
            if len(_dump(trial)) <= budget_chars:
                selected[index] = candidate
        kept = [selected[index] for index in sorted(selected)]
        older = [group for index, group in enumerate(groups) if index not in selected]
        if older:
            digest = hashlib.sha256(_dump(older).encode("utf-8")).hexdigest()
            notes = []
            for group in older:
                first = group[0]
                if first.get("role") == "user" and isinstance(first.get("content"), str):
                    content = first["content"].strip()
                    if _safe_text(content):
                        notes.append(content[:160])
            summary = {"role": "system", "content": _dump({
                "context_summary_ref": digest,
                "older_groups": len(older),
                "recent_user_requests": notes[-3:],
            })}
            with self._lock, self._db:
                self._db.execute("INSERT OR IGNORE INTO summaries VALUES (?,?)", (digest, _dump(older)))
            self.externalize(_dump(older))
            result = prefix + [summary] + [m for group in kept for m in group]
            while len(_dump(result)) > budget_chars and notes:
                notes.pop(0)
                summary["content"] = _dump({"context_summary_ref": digest, "older_groups": len(older), "recent_user_requests": notes})
            if len(_dump(result)) > budget_chars:
                result.remove(summary)
            return result
        return prefix + [m for group in kept for m in group]

    def remember(self, kind: str, text: str, source: str = "user") -> dict:
        if kind not in {"profile", "episodic", "procedural"}:
            raise ValueError("invalid memory kind")
        if not isinstance(text, str) or not _safe_text(text):
            raise ValueError("empty or sensitive memory")
        memory_id = hashlib.sha256(_dump([kind, text.strip(), source]).encode()).hexdigest()
        now = time.time()
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO memories VALUES (?,?,?,?,?)",
                (memory_id, kind, text.strip(), source, now),
            )
            self._db.executemany(
                "INSERT OR IGNORE INTO memory_terms VALUES (?,?)",
                [(term, memory_id) for term in _tokens(text)],
            )
            row = self._db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return {**dict(row), "text": row["body"]}

    def search_memory(self, query: str, limit: int = 5) -> list[dict]:
        terms = _tokens(query)
        if not terms or limit <= 0:
            return []
        placeholders = ",".join("?" for _ in terms)
        with self._lock:
            rows = self._db.execute(f"""
                SELECT m.*, COUNT(*) AS matches FROM memory_terms t
                JOIN memories m ON m.id=t.memory_id WHERE t.term IN ({placeholders})
                GROUP BY m.id ORDER BY matches DESC, m.created_at DESC LIMIT ?
            """, (*sorted(terms), limit)).fetchall()
        return [{**dict(row), "text": row["body"]} for row in rows]

    def profile_memories(self, limit: int = 3) -> list[dict]:
        if limit <= 0:
            return []
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM memories WHERE kind='profile' ORDER BY created_at DESC, id LIMIT ?",
                (limit,),
            ).fetchall()
        return [{**dict(row), "text": row["body"]} for row in rows]

    def reflect(self, session_id: str, prompt: str, answer: str, trace: object) -> list[dict]:
        self.get_session(session_id)
        created: list[dict] = []
        prompt = prompt.strip()
        answer = answer.strip()
        if _safe_text(prompt) and len(prompt) <= 500 and re.search(
            r"(?i)\b(i prefer|my name is|always use|please always|remember that)\b|我(叫|更喜欢|偏好)|请(始终|记住)",
            prompt,
        ):
            created.append(self.remember("profile", prompt, f"session:{session_id}"))
        events = trace if isinstance(trace, list) else []
        errors: dict[str, dict] = {}
        successful: list[str] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            tool = str(event.get("tool", ""))
            result = str(event.get("result", ""))
            if not _safe_text(tool + result):
                continue
            failed = bool(re.search(
                r"(?i)^(tool error:|invalid tool arguments:)|\b(traceback|failed|error:)\b|\bexit\s*=\s*[1-9][0-9]*\b",
                result,
            ))
            if failed:
                errors[tool] = event
            else:
                successful.append(tool)
                if tool in errors and event.get("args") != errors[tool].get("args"):
                    note = f"{tool} failed, then succeeded after a changed invocation. Failure: {str(errors[tool].get('result', ''))[:120]}. Successful arguments: {_dump(event.get('args', {}))[:160]}"
                    if _safe_text(note):
                        created.append(self.remember("procedural", note, f"session:{session_id}"))
                        self.propose_skill_candidate(
                            f"recover-{re.sub(r'[^a-z0-9_-]', '-', tool.lower())[:40] or 'tool'}",
                            f"Recover a failed {tool} invocation",
                            f"Inspect the {tool} error, correct its arguments, and retry only within the user's authorized task.",
                            note,
                        )
                errors.pop(tool, None)
        if successful and _safe_text(prompt) and len(prompt) <= 160:
            observation = f"Task: {prompt}. Successful tool observations: {', '.join(sorted(set(successful)))}. Model answer was not independently verified."
            created.append(self.remember("episodic", observation, f"session:{session_id}"))
        if (
            _safe_text(answer) and len(answer) <= 300
            and re.search(r"(?i)^(what|who|when|where|which)\b|是什么|是谁|在哪里", prompt)
            and not re.search(r"(?i)\b(maybe|probably|uncertain|guess)\b|可能|不确定", answer)
        ):
            created.append(self.remember("episodic", f"{prompt} => {answer}", f"session:{session_id}"))
        return created

    def _skill_files(self) -> list[Path]:
        paths: dict[str, Path] = {}
        for root in (self.skills_dir, self.data_dir / "skills"):
            if root.exists():
                for path in root.rglob("*.json"):
                    paths[path.stem] = path
        return list(paths.values())

    def route_skills(self, query: str, limit: int = 3) -> list[dict]:
        terms = _tokens(query)
        if not terms or limit <= 0:
            return []
        candidates = []
        for path in self._skill_files():
            try:
                skill = json.loads(path.read_text(encoding="utf-8"))
                if not {"name", "level", "description", "instructions"} <= skill.keys():
                    continue
                if skill["level"] not in {"directory", "tool", "skill"}:
                    continue
                fields = {
                    "intent": _tokens(" ".join(skill.get("intents", []))),
                    "tags": _tokens(" ".join(skill.get("tags", []))),
                    "examples": _tokens(" ".join(skill.get("examples", []))),
                    "description": _tokens(skill["description"]),
                }
                lexical = len(terms & set.union(*fields.values()))
                if lexical == 0:
                    continue
                boundary_hit = any(
                    boundary.lower() in query.lower()
                    for boundary in skill.get("boundaries", [])
                    if isinstance(boundary, str) and boundary.strip()
                )
                score = (
                    5 * len(terms & fields["intent"])
                    + 3 * len(terms & fields["tags"])
                    + 2 * len(terms & fields["examples"])
                    + len(terms & fields["description"])
                )
                if score > 0 and boundary_hit == 0:
                    candidates.append({**skill, "score": score})
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return sorted(candidates, key=lambda item: (-item["score"], item["name"]))[:limit]

    def propose_skill_candidate(self, name: str, description: str, instructions: str, evidence: str) -> dict:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", name):
            raise ValueError("invalid skill name")
        if not all(isinstance(x, str) and _safe_text(x) for x in (description, instructions, evidence)):
            raise ValueError("candidate text is empty or sensitive")
        candidate_id = uuid.uuid4().hex
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO skill_candidates VALUES (?,?,?,?,?,?,?)",
                (candidate_id, name, description, instructions, evidence, "pending", time.time()),
            )
        return {"id": candidate_id, "name": name, "status": "pending"}

    def list_skill_candidates(self) -> list[dict]:
        """List pending and activated candidates for explicit human review."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM skill_candidates ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def activate_skill_candidate(self, candidate_id: str) -> dict:
        with self._lock:
            row = self._db.execute("SELECT * FROM skill_candidates WHERE id=?", (candidate_id,)).fetchone()
            if row is None:
                raise KeyError(candidate_id)
            if row["status"] != "pending":
                raise ValueError("candidate is not pending")
            instructions = row["instructions"]
            if re.search(r"(?i)\b(rm\s+-rf|del\s+/[sq]|curl\s+.*\||invoke-expression|eval\s*\(|exec\s*\()", instructions):
                raise ValueError("candidate contains unsafe executable instruction")
            record = {
                "name": row["name"], "level": "skill", "description": row["description"],
                "instructions": instructions, "intents": [row["name"].replace("-", " ")],
                "tags": [], "examples": [], "boundaries": [],
            }
            target = self.data_dir / "skills" / "skill" / f"{row['name']}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise FileExistsError(target)
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(target)
            with self._db:
                self._db.execute("UPDATE skill_candidates SET status='active' WHERE id=?", (candidate_id,))
        return record
