"""Advisory risk review for proposed tool calls.

Copyright (c) 2026 leonbuilds-ai. Licensed under the MIT License.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Awaitable


_SCHEMA: dict[str, dict[str, type]] = {
    "read_file": {"path": str},
    "list_files": {},
    "search_text": {"query": str},
    "write_file": {"path": str, "content": str},
    "run_command": {"argv": list},
    "recall_output": {"ref": str},
    "remember": {"text": str},
    "search_memory": {"query": str},
    "route_skills": {"query": str},
    "delegate": {"mode": str},
}
_OPTIONAL: dict[str, dict[str, type]] = {
    "list_files": {"path": str},
    "search_text": {"path": str},
    "recall_output": {"offset": int, "limit": int},
    "remember": {"kind": str},
    "delegate": {"task": str, "tasks": list},
}
_INJECTION = re.compile(
    r"(?i)(ignore (all |the )?(previous|prior|system|developer) instructions|"
    r"you are now (the )?(system|developer)|system prompt:|"
    r"override (the )?(safety|policy|instructions)|忽略(之前|以上|系统)指令|"
    r"覆盖(系统|开发者)指令|泄露(密钥|密码))"
)
_SENSITIVE = re.compile(r"(?i)(\.env\b|id_rsa\b|private[_-]?key|credential|password|密钥|密码)")
_RANK = {"low": 0, "medium": 1, "high": 2}


class RiskReview:
    """Classify a call; enforcement and user approval belong to the tool layer."""

    async def assess(
        self,
        tool: str,
        args: dict,
        context: str = "",
        classifier: Callable[[dict], Awaitable[str | dict]] | None = None,
    ) -> dict:
        if tool not in _SCHEMA or not isinstance(args, dict) or not isinstance(context, str):
            return {"level": "high", "reason": "Unknown tool or invalid argument object", "source": "validation"}
        fields = _SCHEMA[tool]
        allowed = fields | _OPTIONAL.get(tool, {})
        if (
            any(key not in args or type(args[key]) is not value_type for key, value_type in fields.items())
            or any(key not in allowed or type(value) is not allowed[key] for key, value in args.items())
        ):
            return {"level": "high", "reason": "Missing or mistyped required argument", "source": "validation"}
        if any(not isinstance(item, str) for item in args.get("argv", [])) or (tool == "run_command" and not args["argv"]):
            return {"level": "high", "reason": "Command must be a nonempty string argv array", "source": "validation"}
        if tool == "delegate" and args["mode"] not in {"fork", "team", "worktree"}:
            return {"level": "high", "reason": "Invalid delegation mode", "source": "validation"}
        if tool == "delegate" and "tasks" in args and (
            len(args["tasks"]) > 3 or any(not isinstance(item, str) for item in args["tasks"])
        ):
            return {"level": "high", "reason": "Invalid delegation tasks", "source": "validation"}
        if tool == "remember" and args.get("kind", "episodic") not in {"profile", "episodic", "procedural"}:
            return {"level": "high", "reason": "Invalid memory kind", "source": "validation"}

        payload = json.dumps(args, ensure_ascii=False)
        if _INJECTION.search(context) or _INJECTION.search(payload):
            base = {"level": "high", "reason": "Instruction override pattern in untrusted text", "source": "rules"}
        elif tool == "run_command":
            base = {"level": "high", "reason": "Command execution may change local state", "source": "rules"}
        elif tool in {"write_file", "delegate", "remember"}:
            base = {"level": "medium", "reason": "Tool changes state or delegates work", "source": "rules"}
        elif _SENSITIVE.search(payload):
            base = {"level": "medium", "reason": "Tool arguments mention sensitive material", "source": "rules"}
        else:
            base = {"level": "low", "reason": "Read-only tool with validated arguments", "source": "rules"}

        if classifier is None or base["level"] == "high":
            return base
        bounded = {"tool": tool, "args": json.loads(payload[:4000]) if len(payload) <= 4000 else {"truncated": payload[:4000]}, "context": context[:4000]}
        try:
            response = await classifier(bounded)
            decision = json.loads(response) if isinstance(response, str) else response
            if (
                not isinstance(decision, dict)
                or set(decision) != {"level", "reason"}
                or decision["level"] not in _RANK
                or not isinstance(decision["reason"], str)
                or not decision["reason"].strip()
                or len(decision["reason"]) > 240
            ):
                raise ValueError("invalid classifier response")
        except Exception:
            return {"level": "high", "reason": "Risk classifier failed or returned invalid JSON", "source": "validation"}
        if _RANK[decision["level"]] > _RANK[base["level"]]:
            return {"level": decision["level"], "reason": decision["reason"], "source": "classifier"}
        return base
