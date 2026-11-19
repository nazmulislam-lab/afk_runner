"""Agent providers: print/interactive commands and stream parsing.
Expectations mirror src/AgentProvider.test.ts in the original."""

import json

import pytest

from sandcastle.agent_provider import (
    AgentCommandOptions,
    ContextWindowEvent,
    ErrorEvent,
    ResultEvent,
    SessionIdEvent,
    TextEvent,
    ToolCallEvent,
    UsageEvent,
    claude_code,
    codex,
    copilot,
    cursor,
    opencode,
    parse_claude_session_usage,
    parse_codex_session_context_window,
    pi,
    shell_escape,
)
from sandcastle.errors import ForkNotSupportedError


def opts(prompt="do something", skip=True, resume=None, fork=False):
    return AgentCommandOptions(prompt=prompt, dangerously_skip_permissions=skip, resume_session=resume, fork_session=fork)


def test_shell_escape():
    assert shell_escape("it's") == "'it'\\''s'"
    assert shell_escape("plain") == "'plain'"


class TestClaudeCode:
    def test_basics(self):
        p = claude_code("claude-sonnet-4-6")
        assert p.name == "claude-code"
        pc = p.build_print_command(opts())
        assert "claude-sonnet-4-6" in pc.command
        assert "--output-format stream-json" in pc.command
        assert "--print" in pc.command
        assert "'do something'" not in pc.command
        assert pc.stdin == "do something"
        assert "--model 'claude-sonnet-4-6'" in pc.command
        assert "--dangerously-skip-permissions" in pc.command

    def test_effort(self):
        assert "--effort high" in claude_code("m", effort="high").build_print_command(opts()).command
        assert "--effort" not in claude_code("m").build_print_command(opts()).command

    def test_resume_and_fork(self):
        c = claude_code("m").build_print_command(opts(resume="abc-123")).command
        assert "--resume 'abc-123'" in c and "--fork-session" not in c
        c = claude_code("m").build_print_command(opts(resume="abc-123", fork=True)).command
        assert "--resume 'abc-123'" in c and "--fork-session" in c
        c = claude_code("m").build_print_command(opts(fork=True)).command
        assert "--fork-session" not in c and "--resume" not in c

    def test_permission_mode_replaces_skip(self):
        c = claude_code("m", permission_mode="auto").build_print_command(opts()).command
        assert "--permission-mode auto" in c
        assert "--dangerously-skip-permissions" not in c
        assert "--permission-mode" not in claude_code("m").build_print_command(opts()).command
        assert "--dangerously-skip-permissions" not in claude_code("m").build_print_command(opts(skip=False)).command

    def test_interactive_args(self):
        args = claude_code("m", effort="low").build_interactive_args(opts(prompt="hi"))
        assert args == ["claude", "--dangerously-skip-permissions", "--model", "m", "--effort", "low", "hi"]
        args = claude_code("m", permission_mode="plan").build_interactive_args(opts())
        assert "--permission-mode" in args and "plan" in args and "--dangerously-skip-permissions" not in args

    def test_parse_stream(self):
        p = claude_code("m")
        init = json.dumps({"type": "system", "subtype": "init", "session_id": "s1"})
        assert p.parse_stream_line(init) == [SessionIdEvent("s1")]
        asst = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "a"},
                        {"type": "text", "text": "b"},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "x"}},
                        {"type": "text", "text": "c"},
                    ],
                    "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 100, "output_tokens": 3},
                },
            }
        )
        evs = p.parse_stream_line(asst)
        assert evs[:3] == [TextEvent("ab"), ToolCallEvent("Bash", "ls"), TextEvent("c")]
        assert evs[3] == ContextWindowEvent(evs[3].context_window)
        assert evs[3].context_window.tokens == 115
        sub = json.loads(asst)
        sub["parent_tool_use_id"] = "t"
        assert not any(isinstance(e, ContextWindowEvent) for e in p.parse_stream_line(json.dumps(sub)))
        assert p.parse_stream_line(json.dumps({"type": "result", "result": "done"})) == [ResultEvent("done")]
        assert p.parse_stream_line(json.dumps({"type": "result", "is_error": True, "result": "boom"})) == [ErrorEvent("boom")]
        assert p.parse_stream_line("not json") == []
        assert p.parse_stream_line("{broken") == []

    def test_session_usage(self):
        content = "\n".join(
            [
                json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 1, "cache_creation_input_tokens": 2, "cache_read_input_tokens": 3, "output_tokens": 4}}}),
                json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 5, "cache_creation_input_tokens": 6, "cache_read_input_tokens": 7, "output_tokens": 8}}}),
                "torn",
            ]
        )
        u = parse_claude_session_usage(content)
        assert (u.input_tokens, u.cache_creation_input_tokens, u.cache_read_input_tokens, u.output_tokens) == (5, 6, 7, 8)


class TestPi:
    def test_commands(self):
        p = pi("claude-sonnet-4-6", thinking="high")
        assert p.name == "pi"
        c = p.build_print_command(opts(prompt="continue", resume="abc-123"))
        assert "--session 'abc-123'" in c.command and "--mode json" in c.command
        assert "--model 'claude-sonnet-4-6'" in c.command and "--thinking high" in c.command
        assert c.stdin == "continue"
        assert "--no-session" not in c.command
        f = p.build_print_command(opts(resume="abc-123", fork=True)).command
        assert "--fork 'abc-123'" in f and "--session" not in f
        assert "--thinking" not in pi("m").build_print_command(opts()).command

    def test_parse(self):
        p = pi("m")
        assert p.parse_stream_line(json.dumps({"type": "session", "id": "u1"})) == [SessionIdEvent("u1")]
        assert p.parse_stream_line(json.dumps({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "hi"}})) == [TextEvent("hi")]
        assert p.parse_stream_line(json.dumps({"type": "tool_execution_start", "toolName": "bash", "args": {"command": "ls"}})) == [ToolCallEvent("Bash", "ls")]
        evs = p.parse_stream_line(json.dumps({"type": "message_end", "message": {"role": "assistant", "usage": {"input": 10, "output": 2, "cacheRead": 5, "cacheWrite": 1}}}))
        assert evs[0].context_window.tokens == 16
        assert isinstance(evs[1], UsageEvent) and evs[1].usage.cache_creation_input_tokens == 1
        end = {"type": "agent_end", "messages": [{"role": "assistant", "content": [{"type": "text", "text": "x"}, {"type": "text", "text": "y"}]}]}
        assert p.parse_stream_line(json.dumps(end)) == [ResultEvent("xy")]
        err = {"type": "agent_end", "messages": [{"role": "assistant", "content": [], "stopReason": "error", "errorMessage": "400"}]}
        assert p.parse_stream_line(json.dumps(err)) == [ErrorEvent("400")]
        assert p.parse_stream_line(json.dumps({"type": "error", "error": {"message": "rate"}})) == [ErrorEvent("rate")]


class TestCodex:
    def test_commands(self):
        p = codex("gpt-5.4-mini")
        c = p.build_print_command(opts(prompt="it's a test"))
        assert "codex exec --json" in c.command and "-m 'gpt-5.4-mini'" in c.command
        assert "it's a test" not in c.command and c.stdin == "it's a test"
        r = p.build_print_command(opts(prompt="continue", resume="abc-123")).command
        assert "codex exec resume 'abc-123'" in r and r.endswith(" -")
        f = p.build_print_command(opts(resume="abc-123", fork=True)).command
        assert "codex exec fork 'abc-123'" in f and "codex exec resume" not in f
        assert "model_reasoning_effort" not in c.command
        assert "model_reasoning_effort=\"high\"" in codex("m", effort="high").build_print_command(opts()).command
        a = codex("m", approvals_reviewer="auto_review").build_print_command(opts()).command
        assert "-a on-request" in a and "-s danger-full-access" in a and "--dangerously-bypass-approvals-and-sandbox" not in a
        assert "--dangerously-bypass-approvals-and-sandbox" in c.command and "approvals_reviewer" not in c.command

    def test_parse(self):
        p = codex("m")
        assert p.parse_stream_line(json.dumps({"type": "thread.started", "thread_id": "t1"})) == [SessionIdEvent("t1")]
        assert p.parse_stream_line(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}})) == [TextEvent("hi"), ResultEvent("hi")]
        assert p.parse_stream_line(json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "ls"}})) == [ToolCallEvent("Bash", "ls")]
        u = p.parse_stream_line(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 5}}))[0].usage
        assert (u.input_tokens, u.cache_read_input_tokens) == (60, 40)
        assert p.parse_stream_line(json.dumps({"type": "turn.failed", "error": {"message": "x"}})) == [ErrorEvent("x")]
        assert p.parse_stream_line(json.dumps({"type": "error", "message": "auth"})) == [ResultEvent("auth")]

    def test_session_context_window(self):
        lines = [
            json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1000}, "model_context_window": 258000}}}),
            json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 2000}, "model_context_window": 258000}}}),
        ]
        cw = parse_codex_session_context_window("\n".join(lines))
        assert (cw.tokens, cw.limit) == (2000, 258000)


class TestCursor:
    def test_commands(self):
        p = cursor("claude-sonnet-4-6")
        c = p.build_print_command(opts()).command
        assert "--print" in c and "--output-format stream-json" in c and "--force" in c and "--model 'claude-sonnet-4-6'" in c
        assert " -p " not in c
        assert "--force" not in p.build_print_command(opts(skip=False)).command
        assert "--resume='abc-123' 'test'" in p.build_print_command(opts(prompt="test", resume="abc-123")).command
        with pytest.raises(ForkNotSupportedError):
            p.build_print_command(opts(resume="a", fork=True))
        with pytest.raises(ValueError):
            p.build_print_command(opts(prompt="x" * (121 * 1024)))
        assert p.session_storage.can_fork is False

    def test_parse(self):
        p = cursor("m")
        tc = {"type": "tool_call", "subtype": "started", "tool_call": {"shellToolCall": {"args": {"command": "ls"}}}}
        assert p.parse_stream_line(json.dumps(tc)) == [ToolCallEvent("Bash", "ls")]
        fn = {"type": "tool_call", "subtype": "started", "tool_call": {"function": {"name": "x", "arguments": '{"command":"pwd"}'}}}
        assert p.parse_stream_line(json.dumps(fn)) == [ToolCallEvent("Bash", "pwd")]
        assert p.parse_stream_line(json.dumps({"type": "result", "result": "ok"})) == [ResultEvent("ok")]


class TestOpenCode:
    def test_commands(self):
        p = opencode("opencode/big-pickle", variant="high", agent="build")
        c = p.build_print_command(opts()).command
        assert "opencode run" in c and "--format json" in c and "'do something'" in c
        assert "--model 'opencode/big-pickle'" in c and "--variant 'high'" in c and "--agent 'build'" in c
        assert "--dangerously-skip-permissions" in c
        assert "--session 'ses_abc123' --fork" in p.build_print_command(opts(resume="ses_abc123", fork=True)).command
        args = opencode("m").build_interactive_args(opts(prompt="p"))
        assert args == ["opencode", "--model", "m", "--prompt", "p"]

    def test_parse(self):
        p = opencode("m")
        assert p.parse_stream_line(json.dumps({"type": "step_start", "sessionID": "s"})) == [SessionIdEvent("s")]
        assert p.parse_stream_line(json.dumps({"type": "text", "part": {"type": "text", "text": "hi"}})) == [TextEvent("hi"), ResultEvent("hi")]
        done = {"type": "tool_use", "part": {"type": "tool", "tool": "bash", "state": {"status": "completed", "input": {"command": "ls"}}}}
        assert p.parse_stream_line(json.dumps(done)) == [ToolCallEvent("bash", "ls")]
        other = {"type": "tool_use", "part": {"type": "tool", "tool": "read", "state": {"status": "completed", "input": {"path": "a"}}}}
        assert p.parse_stream_line(json.dumps(other)) == [ToolCallEvent("read", '{"path":"a"}')]
        pending = {"type": "tool_use", "part": {"type": "tool", "tool": "bash", "state": {"status": "running", "input": {}}}}
        assert p.parse_stream_line(json.dumps(pending)) == []
        cw = p.parse_stream_line(json.dumps({"type": "step_finish", "part": {"tokens": {"input": 3, "cache": {"read": 4, "write": 5}}}}))
        assert cw[0].context_window.tokens == 12


class TestCopilot:
    def test_commands(self):
        p = copilot("claude-sonnet-4.5", effort="high")
        c = p.build_print_command(opts()).command
        assert "copilot -p" in c and "'do something'" in c and "--model 'claude-sonnet-4.5'" in c
        assert "--output-format json" in c and "--allow-all-tools" in c and "--effort high" in c
        assert "--resume='abc-123'" in p.build_print_command(opts(resume="abc-123")).command
        with pytest.raises(ForkNotSupportedError):
            p.build_print_command(opts(resume="a", fork=True))
        assert p.build_interactive_args(opts(prompt="go"))[0] == "copilot"
        assert p.build_interactive_args(opts(prompt="go"))[-2:] == ["-i", "go"]

    def test_parse(self):
        p = copilot("m")
        assert p.parse_stream_line(json.dumps({"type": "assistant.message_delta", "data": {"deltaContent": "a"}})) == [TextEvent("a")]
        assert p.parse_stream_line(json.dumps({"type": "tool.execution_start", "data": {"toolName": "bash", "arguments": {"command": "ls"}}})) == [ToolCallEvent("Bash", "ls")]
        assert p.parse_stream_line(json.dumps({"type": "result", "sessionId": "x"})) == [SessionIdEvent("x")]
        assert p.parse_stream_line(json.dumps({"type": "session.usage_checkpoint", "data": {"accountingSnapshot": {"lastCallInputTokens": 9}}}))[0].context_window.tokens == 9
