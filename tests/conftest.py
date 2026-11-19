import json
import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A fresh git repo on `main` with one commit, plus an isolated HOME."""
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "test@example.com")
    git(r, "config", "user.name", "Tester")
    (r / "README.md").write_text("hello\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "init")
    return r


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """Put the scriptable fake `claude` first on PATH. Returns a helper with
    .scenario(list), .calls() and .home."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text(f"#!/bin/sh\nexec {sys.executable} {os.path.join(HERE, 'fake_claude.py')} \"$@\"\n")
    exe.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    gitconfig = home / ".gitconfig"
    gitconfig.write_text("[user]\n\temail = test@example.com\n\tname = Tester\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FAKE_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.jsonl"))

    class Helper:
        def __init__(self):
            self.home = home

        def scenario(self, steps):
            monkeypatch.setenv("FAKE_SCENARIO", json.dumps(steps))

        def calls(self):
            p = tmp_path / "calls.jsonl"
            return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    h = Helper()
    h.scenario([{}])
    return h
