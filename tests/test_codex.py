"""Codex connector against synthetic rollout files."""

from __future__ import annotations

import json
import os
from pathlib import Path

from jezero.connectors.codex import CodexConnector
from jezero.store import ConnectorState

NOW = 2_000_000_000.0
TS = "2033-05-18T00:00:00Z"


def rollout(path: Path, meta: dict, items: list[dict], mtime: float = NOW - 7200) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "session_meta", "timestamp": TS, "payload": meta}]
    lines += [{"type": "response_item", "timestamp": TS, "payload": p} for p in items]
    lines.append({"type": "event_msg", "timestamp": TS, "payload": {"type": "token_count"}})
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    os.utime(path, (mtime, mtime))


def msg(role: str, *texts: str, kinds: list[str] | None = None) -> dict:
    payload: dict = {
        "type": "message",
        "role": role,
        "content": [{"type": "input_text", "text": t} for t in texts],
    }
    if kinds:
        payload["internal_chat_message_metadata_passthrough"] = {"content_item_kinds": kinds}
    return payload


def make_tree(root: Path) -> Path:
    sessions = root / "sessions"
    day = sessions / "2033" / "05" / "18"
    rollout(
        day / "rollout-a.jsonl",
        {
            "id": "sess-a",
            "cwd": "/w/projects/app-wt/T-1",
            "originator": "codex_exec",
            "source": "exec",
        },
        [
            msg("developer", "You are Codex, follow the sandbox rules."),
            msg(
                "user",
                "<recommended_plugins>plugin list</recommended_plugins>",
                "# AGENTS.md instructions for /w\n<INSTRUCTIONS>contract</INSTRUCTIONS>",
                kinds=["plugins.recommendations", "agents_md.instructions"],
            ),
            msg("user", "<environment_context>cwd</environment_context>"),
            msg("user", "Implement the retry policy for the uploader.", kinds=["user.text"]),
            {"type": "reasoning", "summary": [], "encrypted_content": "gAAAA"},
            {"type": "custom_tool_call", "name": "exec", "input": "cargo test -p uploader"},
            {
                "type": "custom_tool_call",
                "name": "exec",
                "input": 'const r=await tools.exec_command({cmd:"cargo build --locked",'
                "max_output_tokens:1600}); text(r)",
            },
            {
                "type": "custom_tool_call",
                "name": "exec",
                "input": "const r=await tools.exec_command({cmd:\"sed -n '1,80p' a.rs\"}); text(r)",
            },
            {
                "type": "custom_tool_call",
                "name": "exec",
                "input": 'text(await tools.write_stdin({session_id:1,chars:""}))',
            },
            {
                "type": "custom_tool_call",
                "name": "exec",
                "input": "text(await tools.apply_patch("
                '"*** Begin Patch\\n*** Update File: src/up.rs\\n@@"))',
            },
            {"type": "function_call", "name": "wait", "arguments": "{}"},
            {"type": "custom_tool_call_output", "output": "test noise " * 100},
            {"type": "function_call", "name": "apply_patch", "arguments": {"path": "a.rs"}},
            msg("assistant", "Done: retries use exponential backoff, tests pass."),
        ],
    )
    rollout(
        day / "rollout-reviewer.jsonl",
        {"id": "sess-r", "cwd": "/", "source": {"subagent": {"other": "guardian"}}},
        [msg("user", "full transcript copy " * 100, kinds=["user.text"])],
    )
    rollout(
        day / "rollout-bench.jsonl",
        {"id": "sess-b", "cwd": "/w/bench-2033", "source": "cli"},
        [msg("user", "benchmark task " * 100, kinds=["user.text"])],
    )
    rollout(
        day / "rollout-live.jsonl",
        {"id": "sess-l", "cwd": "/w", "source": "cli"},
        [msg("user", "still working " * 100, kinds=["user.text"])],
        mtime=NOW - 60,
    )
    (root / "session_index.jsonl").write_text(
        json.dumps({"id": "sess-a", "thread_name": "Uploader retries", "updated_at": TS}) + "\n"
    )
    return sessions


def test_collect_dialogue_only(tmp_path: Path) -> None:
    sessions = make_tree(tmp_path)
    conn = CodexConnector(
        "agent-codex",
        {
            "sessions_dir": str(sessions),
            "skip_cwd_globs": ["*/bench-*"],
            "min_chars": 0,
            "project_aliases": {"app-wt": "app"},
        },
        max_segment_chars=20_000,
    )
    batch = conn.collect(ConnectorState("agent-codex"), now=NOW)
    assert batch.deferred == 1
    assert [d.original_ref for d in batch.docs] == ["codex-session:sess-a"]
    doc = batch.docs[0]
    text = doc.text
    assert doc.title_hint == "Uploader retries"
    assert doc.project_hint == "app"
    assert "Implement the retry policy for the uploader." in text
    assert "Done: retries use exponential backoff" in text
    assert "tool call: exec cargo test -p uploader" in text
    assert "shell: cargo build --locked" in text
    assert "patch: update src/up.rs" in text
    assert "sed -n" not in text and "write_stdin" not in text and "wait" not in text
    assert 'tool call: apply_patch {"path": "a.rs"}' in text
    for noise in (
        "sandbox rules",
        "plugin list",
        "contract",
        "environment_context",
        "gAAAA",
        "test noise",
        "full transcript copy",
        "benchmark task",
    ):
        assert noise not in text
    again = conn.collect(ConnectorState("agent-codex", cursor=batch.next_cursor), now=NOW)
    assert again.docs == []


def test_sample_counts_only_real_sessions(tmp_path: Path) -> None:
    sessions = make_tree(tmp_path)
    conn = CodexConnector(
        "c", {"sessions_dir": str(sessions), "min_chars": 0}, max_segment_chars=20_000
    )
    batch = conn.collect(ConnectorState("c"), sample=1, now=NOW)
    assert len({d.original_ref for d in batch.docs}) == 1
    assert batch.next_cursor is None


def test_sessions_dir_glob_follows_every_agent_home(tmp_path: Path) -> None:
    for agent, text in (("alpha", "first"), ("beta", "second")):
        rollout(
            tmp_path / "homes" / agent / "sessions" / "2033" / f"rollout-{agent}.jsonl",
            {"id": f"sess-{agent}", "cwd": "/w/projects/app", "source": "exec"},
            [msg("user", f"{text} task " + "x" * 400), msg("assistant", "done " + "y" * 200)],
        )
        (tmp_path / "homes" / agent / "session_index.jsonl").write_text(
            json.dumps({"id": f"sess-{agent}", "thread_name": f"{text} title"}) + "\n"
        )
    pattern = str(tmp_path / "homes" / "*" / "sessions")
    conn = CodexConnector("c", {"sessions_dir": pattern}, max_segment_chars=60_000)
    batch = conn.collect(ConnectorState(name="c"), now=NOW)
    assert sorted(d.title_hint for d in batch.docs) == ["first title", "second title"]
    empty = CodexConnector(
        "c", {"sessions_dir": str(tmp_path / "none" / "*" / "sessions")}, max_segment_chars=60_000
    )
    assert empty.collect(ConnectorState(name="c"), now=NOW).docs == []


def test_user_prompt_markers_drop_repeated_agent_instructions(tmp_path: Path) -> None:
    prompt = "# Agent role\n" + "long instructions " * 50 + "\n## Wake Payload\nFix the login bug"
    rollout(
        tmp_path / "sessions" / "2033" / "rollout-w.jsonl",
        {"id": "sess-w", "cwd": "/w/projects/app", "source": "exec"},
        [msg("user", prompt), msg("assistant", "Fixed it " + "z" * 400)],
    )
    opts = {
        "sessions_dir": str(tmp_path / "sessions"),
        "user_prompt_markers": ["^## Wake Payload"],
        "user_prompt_drop_lines": ["Fix the"],
    }
    conn = CodexConnector("c", opts, max_segment_chars=60_000)
    (doc,) = conn.collect(ConnectorState(name="c"), now=NOW).docs
    assert "long instructions" not in doc.text
    assert "## Wake Payload" in doc.text
    assert "Fix the login bug" not in doc.text
