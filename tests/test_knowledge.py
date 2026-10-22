"""Behavioral tests for the independent knowledge layer."""

import json
import threading

import pytest

from nanocode.knowledge import KnowledgeStore


def test_sessions_and_outputs_survive_reopen(tmp_path):
    store = KnowledgeStore(tmp_path)
    session = store.create_session("First")
    store.append_message(session["id"], {"role": "user", "content": "hello"})
    store.rename_session(session["id"], "Renamed")
    output = store.externalize("a" * 7000)
    assert output == store.externalize("a" * 7000)
    store.close()

    reopened = KnowledgeStore(tmp_path)
    assert reopened.get_session(session["id"])["messages"] == [{"role": "user", "content": "hello"}]
    assert reopened.list_sessions()[0]["title"] == "Renamed"
    assert reopened.list_sessions()[0]["message_count"] == 1
    assert reopened.recall_output(output["ref"], 5990, 30) == "a" * 30
    assert reopened.read_external(output["ref"]) == "a" * 7000
    with pytest.raises(ValueError):
        reopened.recall_output("../knowledge.sqlite3")
    with pytest.raises(KeyError):
        reopened.get_session("missing")


def test_concurrent_appends_have_unique_order(tmp_path):
    store = KnowledgeStore(tmp_path)
    session_id = store.create_session()["id"]
    threads = [
        threading.Thread(target=lambda n=i: store.append_message(session_id, {"role": "user", "content": str(n)}))
        for i in range(32)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert {message["content"] for message in store.get_session(session_id)["messages"]} == {
        str(i) for i in range(32)
    }


def test_compact_keeps_recent_tool_pair_and_hard_budget(tmp_path):
    store = KnowledgeStore(tmp_path)
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "first request " + "x" * 800},
        {"role": "assistant", "content": "old response " + "y" * 800},
        {"role": "user", "content": "check file"},
        {"role": "assistant", "tool_calls": [{"id": "call1", "name": "read_file"}]},
        {"role": "tool", "tool_call_id": "call1", "content": "file contents"},
        {"role": "assistant", "content": "done"},
    ]
    packed = store.compact(messages, budget_chars=550)
    assert len(json.dumps(packed, ensure_ascii=False, separators=(",", ":"), sort_keys=True)) <= 550
    assert packed[0] == messages[0]
    assert messages[4:7] == packed[-3:]
    assert any("context_summary_ref" in item.get("content", "") for item in packed)
    summary = next(item for item in packed if "context_summary_ref" in item.get("content", ""))
    reference = json.loads(summary["content"])["context_summary_ref"]
    assert "first request" in store.read_external(reference)
    assert not any(item.get("role") == "tool" and item.get("tool_call_id") == "call1" for item in packed[:-2])


def test_compact_preserves_huge_current_request_by_reference(tmp_path):
    store = KnowledgeStore(tmp_path)
    full = "Please inspect this: " + "z" * 5000
    packed = store.compact([{"role": "user", "content": full}], budget_chars=320)
    assert len(json.dumps(packed, ensure_ascii=False, separators=(",", ":"), sort_keys=True)) <= 320
    assert packed[-1]["role"] == "user"
    reference = packed[-1]["content"].split("ref=")[1][:64]
    assert store.read_external(reference) == full


def test_compact_large_tool_args_and_invalid_protocol(tmp_path):
    store = KnowledgeStore(tmp_path)
    huge_args = json.dumps({"query": "x" * 5000})
    messages = [
        {"role": "user", "content": "Find the config"},
        {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "search_text", "arguments": huge_args}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "Found config"},
        {"role": "assistant", "content": "Done"},
        {"role": "tool", "tool_call_id": "orphan", "content": "Ignore me"},
        {"role": "assistant", "tool_calls": [{"id": "incomplete"}]},
    ]
    original = json.loads(json.dumps(messages))
    packed = store.compact(messages, budget_chars=520)
    assert messages == original
    assert len(json.dumps(packed, ensure_ascii=False, separators=(",", ":"), sort_keys=True)) <= 520
    assert packed[0]["role"] == "user"
    assert [item["role"] for item in packed if item["role"] != "system"] == ["user", "assistant", "tool", "assistant"]
    ref = json.loads(packed[1]["tool_calls"][0]["function"]["arguments"])["externalized_arguments_ref"]
    assert store.read_external(ref) == huge_args


def test_memory_reflection_search_and_secret_guard(tmp_path):
    store = KnowledgeStore(tmp_path)
    session_id = store.create_session()["id"]
    saved = store.reflect(session_id, "I prefer concise Python examples", "Understood", [])
    assert len(saved) == 1 and saved[0]["kind"] == "profile"
    assert store.search_memory("Python examples")[0]["kind"] == "profile"
    fixed = store.reflect(session_id, "Run checks", "Passed", [
        {"tool": "pytest", "args": {"path": "bad"}, "result": "Tool error: file missing"},
        {"tool": "pytest", "args": {"path": "good"}, "result": "5 passed"},
    ])
    assert {item["kind"] for item in fixed} == {"procedural", "episodic"}
    assert store.search_memory("pytest")[0]["kind"] in {"procedural", "episodic"}
    assert store.reflect(session_id, "What is the port?", "The port is 8080", [])[0]["kind"] == "episodic"
    assert store.reflect(session_id, "api_key=sk-123456789abcdef", "ok", []) == []
    with pytest.raises(ValueError):
        store.remember("profile", "password: abc123")
    store.close()
    reopened = KnowledgeStore(tmp_path)
    other_session = reopened.create_session("Another task")
    assert other_session["id"] != session_id
    assert any(item["kind"] == "profile" for item in reopened.search_memory("Python examples"))
    assert reopened.profile_memories(1)[0]["kind"] == "profile"


def test_reflect_requires_success_and_changed_arguments(tmp_path):
    store = KnowledgeStore(tmp_path)
    session_id = store.create_session()["id"]
    records = store.reflect(session_id, "Run checks", "Done", [
        {"tool": "run_command", "args": {"argv": ["pytest"]}, "result": "exit=1"},
        {"tool": "run_command", "args": {"argv": ["pytest"]}, "result": "exit=0"},
    ])
    assert all(record["kind"] != "procedural" for record in records)
    records = store.reflect(session_id, "Run checks", "Done", [
        {"tool": "run_command", "args": {"argv": ["pytest", "bad"]}, "result": "exit=1"},
        {"tool": "run_command", "args": {"argv": ["pytest", "good"]}, "result": "exit=0"},
    ])
    assert any(record["kind"] == "procedural" for record in records)


def test_skill_routing_boundaries_and_activation(tmp_path):
    store = KnowledgeStore(tmp_path)
    assert store.route_skills("Please run tests for this module")[0]["name"] == "test-runner"
    assert all(item["name"] != "test-runner" for item in store.route_skills("skip tests"))
    candidate = store.propose_skill_candidate(
        "check-schema", "Check database schema", "Inspect tables and compare expected columns.",
        "Successful correction in session 1",
    )
    assert candidate["status"] == "pending"
    assert store.list_skill_candidates()[0]["id"] == candidate["id"]
    assert all(item["name"] != "check-schema" for item in store.route_skills("check schema"))
    store.activate_skill_candidate(candidate["id"])
    assert store.route_skills("check schema")[0]["name"] == "check-schema"
    with pytest.raises(ValueError):
        store.activate_skill_candidate(candidate["id"])
    dangerous = store.propose_skill_candidate("bad-command", "bad", "rm -rf /", "example")
    with pytest.raises(ValueError):
        store.activate_skill_candidate(dangerous["id"])
