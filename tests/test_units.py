"""Pure-function units: session store, prompts, output, usage limits,
sync-out path checks, patches, recovery message, git hardening, mounts,
Windows command parsing, display buffers."""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from sandcastle.context_window import ContextWindow, format_context_window
from sandcastle.display import BoundedTail, FileDisplay, SilentDisplay, TextDeltaBuffer
from sandcastle.errors import PromptError, SyncOutPathError
from sandcastle.host_git import hardening_overrides, parse_config_list, trusted_path, with_config_overrides
from sandcastle.mounts import (
    SANDBOX_REPO_DIR,
    MountEntry,
    default_image_name,
    format_volume_mount,
    normalize_mounts,
    parse_gitdir_path,
    process_file_mount_parents,
    resolve_sandbox_path,
)
from sandcastle.output import ExtractionContext, Output, StructuredOutputError, extract_structured_output, find_last_tag_content
from sandcastle.prompts import (
    SHELL_BLOCK_MARKER,
    find_missing_prompt_arg_keys,
    preprocess_prompt,
    resolve_prompt,
    substitute_prompt_args,
    validate_no_args_with_inline_prompt,
    validate_no_built_in_arg_override,
)
from sandcastle.run import build_log_filename, sanitize_branch_for_filename
from sandcastle.sandbox_provider import ExecResult, Mount
from sandcastle.sandboxes._engine import resolve_engine_exit, shell_args_reporting_exit
from sandcastle.sandboxes._windows import parse_simple_command
from sandcastle.session_store import (
    encode_pi_session_dir,
    encode_project_path,
    transfer_claude_session,
    transfer_copilot_session,
    transfer_pi_session,
)
from sandcastle.sync import (
    build_recovery_message,
    check_patch_name,
    check_patch_symlinks,
    check_relative_path,
    is_git_segment,
    parse_patch,
    resolve_host_destination,
)
from sandcastle.usage_limit import is_usage_limit_message, parse_reset_time
from sandcastle.util import merge_provider_env, parse_env_text, validate_timeout_options
from sandcastle.worktree_manager import generate_temp_branch_name, sanitize_name

# ---------------------------------------------------------------------------
# Session store (vectors from tests/encodeProjectPath.test.ts)
# ---------------------------------------------------------------------------


def test_encode_project_path_matches_claude_code():
    assert encode_project_path("/home/me/repo/.sandcastle/worktrees/sandcastle_2026.x") == "-home-me-repo--sandcastle-worktrees-sandcastle-2026-x"
    assert encode_project_path("/tmp/sp ace/ü~@+=,dir") == "-tmp-sp-ace-------dir"
    long = f"/tmp/enc-5jbL/{'a' * 120}/{'b' * 120}"
    assert encode_project_path(long) == f"-tmp-enc-5jbL-{'a' * 120}-{'b' * 65}-oy0ktj"
    assert encode_project_path("C:\\Users\\me\\my.app") == "C--Users-me-my-app"
    assert encode_project_path("/x/y/") == "-x-y"


def test_transfer_sessions():
    src = '{"cwd":"/a","x":1}\n{"type":"session_meta","payload":{"cwd":"/a"}}\ntorn\n{"cwd":"/b"}\n'
    out = transfer_claude_session(src, "/a", "/z").split("\n")
    assert json.loads(out[0])["cwd"] == "/z"
    assert json.loads(out[1])["payload"]["cwd"] == "/z"
    assert out[2] == "torn" and json.loads(out[3])["cwd"] == "/b" and out[4] == ""
    pi_out = transfer_pi_session('{"type":"session","cwd":"/a"}\n{"type":"message","cwd":"/a"}', "/a", "/z").split("\n")
    assert json.loads(pi_out[0])["cwd"] == "/z" and json.loads(pi_out[1])["cwd"] == "/a"
    cp = transfer_copilot_session('{"data":{"context":{"cwd":"/a","gitRoot":"/a"}}}\n{"data":{}}', "/a", "/z").split("\n")
    assert json.loads(cp[0])["data"]["context"] == {"cwd": "/z", "gitRoot": "/z"}
    assert cp[1] == '{"data":{}}'
    assert encode_pi_session_dir("/home/me/proj") == "--home-me-proj--"


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def test_resolve_prompt(tmp_path):
    with pytest.raises(PromptError):
        resolve_prompt("a", "b")
    with pytest.raises(PromptError):
        resolve_prompt()
    assert resolve_prompt("x").source == "inline"
    f = tmp_path / "p.md"
    f.write_text("hello")
    r = resolve_prompt(prompt_file=str(f))
    assert (r.text, r.source) == ("hello", "template")
    with pytest.raises(PromptError):
        resolve_prompt(prompt_file=str(tmp_path / "missing.md"))


def test_substitute_prompt_args():
    d = SilentDisplay()
    out = substitute_prompt_args("Issue {{ N }} ok={{FLAG}}", {"N": 42, "FLAG": True, "UNUSED": "x"}, d)
    assert out == "Issue 42 ok=true"
    assert any("UNUSED" in e.message for e in d.entries)
    with pytest.raises(PromptError):
        substitute_prompt_args("{{MISSING}}", {}, d)
    assert find_missing_prompt_arg_keys("{{A}} {{SOURCE_BRANCH}} {{B}} {{A}}", {"B": 1}) == ["A"]
    with pytest.raises(PromptError):
        validate_no_built_in_arg_override({"SOURCE_BRANCH": "x"})
    with pytest.raises(PromptError):
        validate_no_args_with_inline_prompt({"A": 1})
    validate_no_args_with_inline_prompt({})


def test_shell_blocks_are_marked_but_not_from_args_or_comments():
    out = substitute_prompt_args("run !`echo {{X}}` <!-- !`rm -rf` -->", {"X": "!`evil`"}, SilentDisplay())
    assert f"!{SHELL_BLOCK_MARKER}`echo !`evil``" in out
    assert "<!-- !`rm -rf` -->" in out


class FakeSandbox:
    def __init__(self, results):
        self.results = results
        self.calls = []

    async def exec(self, command, **kw):
        self.calls.append(command)
        return self.results.get(command, ExecResult("", "boom", 1))


async def test_preprocess_prompt():
    marked = substitute_prompt_args("A !`echo hi` B !`git log`", {}, SilentDisplay())
    sb = FakeSandbox({"echo hi": ExecResult("hi\n", "", 0), "git log": ExecResult("abc\n\n", "", 0)})
    d = SilentDisplay()
    assert await preprocess_prompt(marked, sb, "/w", d) == "A hi B abc"
    assert d.entries[-1].tag == "taskLog"
    with pytest.raises(PromptError) as e:
        await preprocess_prompt(substitute_prompt_args("!`nope`", {}, d), sb, "/w", d)
    assert e.value.exit_code == 1
    assert await preprocess_prompt("no blocks", sb, "/w", d) == "no blocks"


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------


async def test_structured_output():
    from pydantic import BaseModel

    class M(BaseModel):
        answer: int

    assert find_last_tag_content("<r>1</r> <r>2</r>", "r") == "2"
    ctx = ExtractionContext(branch="b", session_id="s")
    out = await extract_structured_output('x <r>\n```json\n{"answer": 3}\n```\n</r>', Output.object("r", M), ctx)
    assert out.answer == 3
    assert await extract_structured_output("<s>  hi </s>", Output.string("s"), ctx) == "hi"
    with pytest.raises(StructuredOutputError) as e:
        await extract_structured_output("nothing", Output.object("r", M), ctx)
    assert e.value.raw_matched is None and e.value.session_id == "s"
    with pytest.raises(StructuredOutputError) as e:
        await extract_structured_output("<r>{bad</r>", Output.object("r", M), ctx)
    assert e.value.raw_matched == "{bad"
    with pytest.raises(StructuredOutputError) as e:
        await extract_structured_output('<r>{"answer": "x"}</r>', Output.object("r", M), ctx)
    assert "schema validation" in e.value.message

    def positive(v):
        if v["n"] <= 0:
            raise ValueError("must be positive")
        return v

    assert (await extract_structured_output('<r>{"n": 2}</r>', Output.object("r", positive), ctx)) == {"n": 2}


# ---------------------------------------------------------------------------
# Usage limit
# ---------------------------------------------------------------------------


def test_usage_limit_detection_and_reset_parsing():
    now = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
    assert is_usage_limit_message("Claude AI usage limit reached|1900000000")
    assert parse_reset_time("Claude AI usage limit reached|1900000000", now) == datetime.fromtimestamp(1900000000, tz=timezone.utc)
    assert is_usage_limit_message("5-hour limit reached ∙ resets 3pm")
    assert is_usage_limit_message("You've hit your limit · resets 11:30pm (Asia/Dhaka)")
    dhaka = parse_reset_time("You've hit your limit · resets 11:30pm (Asia/Dhaka)", now)
    assert dhaka == datetime(2026, 11, 18, 17, 30, tzinfo=timezone.utc)  # 23:30 +06:00
    assert parse_reset_time("try again in 2h 15m", now) == now + timedelta(hours=2, minutes=15)
    assert parse_reset_time("resets in 37 minutes", now) == now + timedelta(minutes=37)
    earlier = parse_reset_time("limit reached, resets 9am (UTC)", now)
    assert earlier == datetime(2026, 11, 19, 9, 0, tzinfo=timezone.utc)  # already past today
    assert parse_reset_time("resets Nov 20, 3pm (UTC)", now) == datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
    assert parse_reset_time("no time here", now) is None
    assert not is_usage_limit_message("I implemented the rate limiter and checked usage")
    assert not is_usage_limit_message("")


# ---------------------------------------------------------------------------
# Sync-out paths and patches
# ---------------------------------------------------------------------------


def test_relative_path_checks():
    assert check_relative_path("a/./b") == ["a", "b"]
    for bad in ("/etc/passwd", "../x", "a/../../x", ".git/config", "a/.GIT. /x", "C:x", "", "a\0b", "\\\\server\\x"):
        with pytest.raises(SyncOutPathError):
            check_relative_path(bad)
    assert is_git_segment(".git ") and is_git_segment(".Git.")
    check_patch_name("0001-x.patch")
    for bad in ("a/b", "..", ""):
        with pytest.raises(SyncOutPathError):
            check_patch_name(bad)


def test_resolve_host_destination_refuses_escaping_symlinks(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    (root / "ok").mkdir()
    os.symlink(str(tmp_path), root / "out")
    os.symlink(str(root / "nowhere"), root / "dangling")
    real = os.path.realpath(root)
    assert resolve_host_destination(real, "ok/new.txt") == os.path.join(real, "ok", "new.txt")
    with pytest.raises(SyncOutPathError):
        resolve_host_destination(real, "out/x.txt")
    with pytest.raises(SyncOutPathError):
        resolve_host_destination(real, "dangling/x")


def test_patch_parsing_and_symlink_checks():
    patch = (
        "diff --git a/l b/l\nnew file mode 120000\n--- /dev/null\n+++ b/l\n@@ -0,0 +1 @@\n+../outside\n"
        "\\ No newline at end of file\n"
    )
    ch = parse_patch(patch)[0]
    assert ch.symlink and ch.path == "l" and ch.new_lines == ["../outside"]
    with pytest.raises(SyncOutPathError):
        check_patch_symlinks([patch], lambda p: None)
    inside = patch.replace("../outside", "sub/target")
    check_patch_symlinks([inside], lambda p: None)
    to_git = patch.replace("../outside", ".git/hooks")
    with pytest.raises(SyncOutPathError):
        check_patch_symlinks([to_git], lambda p: None)
    renamed = 'diff --git a/x b/y\nrename from x\nrename to "y\\303\\251"\n'
    r = parse_patch(renamed)[0]
    assert (r.from_, r.path) == ("x", "yé")
    # A pure rename of a host symlink that points outside is refused.
    with pytest.raises(SyncOutPathError):
        check_patch_symlinks(['diff --git a/x b/y\nrename from x\nrename to y\n'], lambda p: "/etc" if p == "x" else None)


def test_recovery_message():
    m = build_recovery_message(patch_dir=".sandcastle/patches/1", failed_step="commits", has_commits=True, has_diff=True, has_untracked=True)
    assert m.startswith("Patch application failed at step 1 (committed changes).")
    assert "git am --continue" in m
    assert "  git apply .sandcastle/patches/1/changes.patch && \\\n  cp -r .sandcastle/patches/1/untracked/* ." in m
    m2 = build_recovery_message(patch_dir="p", failed_step="diff", has_commits=True, has_diff=True, has_untracked=False, branch="feat")
    assert "git worktree add .sandcastle/worktree feat" in m2 and "git apply ../../p/changes.patch" in m2
    with pytest.raises(ValueError):
        build_recovery_message(patch_dir="p", failed_step="untracked", has_commits=True, has_diff=False, has_untracked=False)


# ---------------------------------------------------------------------------
# Host git hardening
# ---------------------------------------------------------------------------


def test_hardening_overrides():
    listing = "global\0core.pager\nless\0local\0core.fsmonitor\0local\0filter.lfs.clean\ngit-lfs clean\0local\0credential.https://x.helper\nevil\0global\0credential.helper\nstore\0local\0core.gitproxy\nevil\0"
    entries = parse_config_list(listing)
    assert entries[1].value is None
    ov = dict()
    for k, v in hardening_overrides(entries):
        ov.setdefault(k, []).append(v)
    assert ov["core.pager"] == ["less"]  # global value kept
    assert ov["core.fsmonitor"] == ["false"]  # repo value neutralised
    assert ov["filter.lfs.clean"] == [""]
    assert ov["protocol.git.allow"] == ["never"]
    assert ov["credential.helper"] == ["", "store"]  # cleared, trusted helper re-added
    assert ov["credential.https://x.helper"] == [""]
    env = with_config_overrides({"GIT_CONFIG_COUNT": "2"}, [("a", "1")])
    assert env["GIT_CONFIG_COUNT"] == "3" and env["GIT_CONFIG_KEY_2"] == "a"


def test_trusted_path_drops_relative_and_repo_entries(tmp_path):
    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    path = os.pathsep.join(["relative/bin", "", str(repo / "bin"), "/usr/bin"])
    assert trusted_path(path, [str(repo)]) == "/usr/bin"


# ---------------------------------------------------------------------------
# Mounts
# ---------------------------------------------------------------------------


def test_mount_helpers(tmp_path):
    assert default_image_name("/home/me/My Repo/") == "sandcastle:my-repo"
    assert default_image_name("C:\\code\\App") == "sandcastle:app"
    assert format_volume_mount(Mount("/a", "/b", True), "z") == "/a:/b:ro,z"
    assert format_volume_mount(Mount("/a", "/b"), False) == "/a:/b"
    assert resolve_sandbox_path("data") == f"{SANDBOX_REPO_DIR}/data"
    assert resolve_sandbox_path("~/.npm", "/home/agent") == "/home/agent/.npm"
    with pytest.raises(ValueError):
        resolve_sandbox_path("~/x")
    assert parse_gitdir_path("C:\\r\\.git\\worktrees\\wt") == ("C:/r/.git", "wt")
    f = tmp_path / "file"
    f.write_text("x")
    assert process_file_mount_parents([Mount(str(f), "/home/agent/.config/a/file")], "/home/agent") == ["/home/agent/.config/a"]
    with pytest.raises(ValueError):
        process_file_mount_parents([Mount(str(f), "/etc/x/file")], "/home/agent")
    win = normalize_mounts([MountEntry("C:\\wt\\.git", "C:\\wt\\.git")], "C:\\wt", SANDBOX_REPO_DIR, platform="win32")
    assert (win[0].host_path, win[0].sandbox_path) == ("C:/wt/.git", f"{SANDBOX_REPO_DIR}/.git")


# ---------------------------------------------------------------------------
# Engine exit status / Windows commands
# ---------------------------------------------------------------------------


def test_engine_exit_status():
    assert resolve_engine_exit("err\n__SANDCASTLE_EXIT_STATUS=3\n", 0) == ("err", 3)
    assert resolve_engine_exit("killed", 0) == ("killed", 1)
    assert resolve_engine_exit("x", 125) == ("x", 125)
    assert shell_args_reporting_exit("ls")[-1] == "ls"


def test_parse_simple_command():
    s = parse_simple_command("export A='1 2'; claude --print 'it'\\''s' \"q\\\"x\"")
    assert s.env == {"A": "1 2"} and s.argv == ["claude", "--print", "it's", 'q"x']
    assert parse_simple_command("echo $HOME") is None
    assert parse_simple_command("a | b") is None


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def test_misc_helpers():
    assert format_context_window(ContextWindow(46001)) == "47k"
    assert format_context_window(ContextWindow(32000, 258999)) == "32k / 258k"
    assert sanitize_name("Fix Bug 😀") == "fix-bug---"
    assert generate_temp_branch_name("My Run").startswith("sandcastle/my-run/")
    assert sanitize_branch_for_filename("a/b:c") == "a-b-c"
    assert build_log_filename("sandcastle/x", "main", "W 1") == "main-sandcastle-x-w-1.log"
    assert parse_env_text('A=1\n# c\nB="x\\ny"\nC=\'q\'\nbad\n') == {"A": "1", "B": "x\ny", "C": "q"}
    assert merge_provider_env({"A": "1"}, {"B": "2"}, {"C": "3"}) == {"A": "1", "B": "2", "C": "3"}
    with pytest.raises(ValueError):
        merge_provider_env({}, {"K": "1"}, {"K": "2"})
    with pytest.raises(ValueError):
        validate_timeout_options(idle_timeout_seconds=0)
    validate_timeout_options(idle_timeout_seconds=float("inf"))


def test_bounded_tail():
    t = BoundedTail(10, "\n")
    for line in ("aaaa", "bbbb", "cccc"):
        t.push(line)
    assert str(t) == "bbbb\ncccc"
    big = BoundedTail(5)
    big.push("0123456789")
    assert str(big) == "56789"


async def test_text_delta_buffer():
    out = []
    b = TextDeltaBuffer(out.append)
    b.write("Hello")
    b.write(" world. ")
    assert out == ["Hello world. "]
    b.write("tail")
    await asyncio.sleep(0.1)
    assert out[-1] == "tail"


def test_file_display(tmp_path):
    p = tmp_path / "logs" / "run.log"
    d = FileDisplay(str(p))
    d.status("[name] Started", "info")
    d.text_chunk("partial")
    d.tool_call("Bash", "ls")
    text = p.read_text()
    assert "--- Run started:" in text and "\nStarted\n" in text and "partial\nBash(ls)\n" in text
