"""Command-line entry point."""

import argparse
import asyncio
import json
from .config import Settings
from .engine import Engine


async def _ask_approval(action):
    print("Approval required:", json.dumps(action, ensure_ascii=False, indent=2))
    response = await asyncio.to_thread(input, "Approve? [y/N] ")
    return response.strip().lower() in {"y", "yes"}


async def _chat(prompt: str, session_id: str):
    engine = Engine(Settings.from_env())
    async for event in engine.run(session_id, prompt, _ask_approval):
        if event["type"] == "delta":
            print(event["data"]["text"], end="", flush=True)
        elif event["type"] in {"error", "done"}:
            print()
            print(json.dumps(event, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(prog="nano-code", description="Independent local coding agent")
    sub = parser.add_subparsers(dest="command")
    chat = sub.add_parser("chat", help="Run one prompt")
    chat.add_argument("prompt")
    chat.add_argument("--session", default="")
    web = sub.add_parser("web", help="Serve the local web UI")
    web.add_argument("--host", choices=["127.0.0.1", "::1", "localhost"], default="127.0.0.1")
    web.add_argument("--port", type=int, default=8865)
    skills = sub.add_parser("skills", help="Review pending skill candidates")
    skills.add_argument("--activate", metavar="CANDIDATE_ID")
    args = parser.parse_args()
    if args.command == "skills":
        from .knowledge import KnowledgeStore
        store = KnowledgeStore(Settings.from_env().data_dir)
        try:
            candidates = store.list_skill_candidates()
            if args.activate:
                candidate = next((c for c in candidates if c["id"] == args.activate), None)
                if candidate is None:
                    parser.error("Candidate not found")
                print(json.dumps(candidate, ensure_ascii=False, indent=2))
                if input("Activate this skill? [y/N] ").strip().lower() in {"y", "yes"}:
                    print(json.dumps(store.activate_skill_candidate(args.activate), ensure_ascii=False))
            else:
                print(json.dumps(candidates, ensure_ascii=False, indent=2))
        finally:
            store.close()
    elif args.command == "chat":
        asyncio.run(_chat(args.prompt, args.session))
    elif args.command == "web":
        import uvicorn
        from .web import create_app
        uvicorn.run(create_app(Settings.from_env()), host=args.host, port=args.port)
    else:
        parser.print_help()
