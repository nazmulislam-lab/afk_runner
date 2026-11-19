"""How ``no_sandbox()`` runs a command on Windows (issue #940).

Port of ``windowsCommand.ts`` and ``posixSimpleCommand.ts``. Sandcastle
builds exec commands as POSIX shell strings; Windows has no ``sh`` and
cmd.exe doesn't understand POSIX quoting. So a *simple* command is split
back into its argv and the program spawned directly (``.exe``), or through
cmd.exe with batch-file escaping (``.cmd``/``.bat``); anything else goes to
cmd.exe unchanged.
"""

from __future__ import annotations

import ntpath
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, Union

ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
UNQUOTED_SPECIAL = "|&<>()$`*?[]{}~#!%^\n\r"
SEPARATORS = " \t;"
DOUBLE_QUOTE_ESCAPABLE = '"\\$`'
CMD_MAX_COMMAND_LINE = 8191
_CMD_META = re.compile(r'([()\][%!^"`<>&|;, *?])')


@dataclass(frozen=True)
class SimpleCommand:
    env: Dict[str, str]
    argv: List[str]


@dataclass(frozen=True)
class SpawnPlan:
    file: str
    args: List[str]
    verbatim: bool
    env: Dict[str, str] = field(default_factory=dict)
    #: For a verbatim plan: the line cmd.exe runs (handed to the shell as is).
    shell_line: str = ""

    def command_line(self) -> Union[List[str], str]:
        """An argv list for a direct spawn, or the cmd.exe line (a str) for
        a verbatim plan, which the runner hands to the shell unchanged."""
        if self.verbatim:
            return self.shell_line
        return [self.file, *self.args]


def _read_single(s: str, i: int) -> Optional[Tuple[str, int]]:
    end = s.find("'", i + 1)
    return None if end == -1 else (s[i + 1 : end], end + 1)


def _read_double(s: str, i: int) -> Optional[Tuple[str, int]]:
    text, j = "", i + 1
    while j < len(s):
        c = s[j]
        nxt = s[j + 1] if j + 1 < len(s) else ""
        if c == '"':
            return text, j + 1
        if c in "$`" or (c == "\\" and nxt == "\n"):
            return None
        if c == "\\" and nxt and nxt in DOUBLE_QUOTE_ESCAPABLE:
            text += nxt
            j += 2
        else:
            text += c
            j += 1
    return None


def _read_bare(s: str, i: int) -> Tuple[str, int]:
    j = i
    while j < len(s) and s[j] not in SEPARATORS and s[j] not in UNQUOTED_SPECIAL and s[j] not in "'\"\\":
        j += 1
    return s[i:j], j


def _read_piece(s: str, i: int) -> Optional[Tuple[str, int]]:
    c = s[i]
    if c == "'":
        return _read_single(s, i)
    if c == '"':
        return _read_double(s, i)
    if c == "\\":
        return ("'", i + 2) if i + 1 < len(s) and s[i + 1] == "'" else None
    if c in UNQUOTED_SPECIAL:
        return None
    return _read_bare(s, i)


def _split_statements(s: str) -> Optional[List[List[str]]]:
    statements: List[List[str]] = [[]]
    word: Optional[str] = None
    i = 0
    while i < len(s):
        c = s[i]
        piece = None if c in SEPARATORS else _read_piece(s, i)
        if piece is not None:
            word = (word or "") + piece[0]
            i = piece[1]
        elif c in SEPARATORS:
            if word is not None:
                statements[-1].append(word)
            word = None
            if c == ";":
                statements.append([])
            i += 1
        else:
            return None
    if word is not None:
        statements[-1].append(word)
    return statements


def parse_simple_command(command: str) -> Optional[SimpleCommand]:
    statements = _split_statements(command)
    if statements is None:
        return None
    argv = statements.pop()
    if not argv or argv[0] == "" or "=" in argv[0]:
        return None
    env: Dict[str, str] = {}
    for st in statements:
        if not st or st[0] != "export" or len(st) < 2:
            return None
        for a in st[1:]:
            eq = a.find("=")
            name = a[:eq]
            if eq == -1 or not ENV_NAME.match(name):
                return None
            env[name] = a[eq + 1 :]
    return SimpleCommand(env, argv)


def _get_env(env: Mapping[str, str], name: str) -> Optional[str]:
    keys = sorted(k for k in env if k.upper() == name)
    return env[keys[0]] if keys else None


def _candidates(program: str, env: Mapping[str, str], cwd: str) -> List[str]:
    exts = [e.lower() for e in (_get_env(env, "PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(";") if e]
    has_ext = ntpath.splitext(program)[1].lower() in exts

    def with_exts(base: str) -> List[str]:
        return [base] if has_ext else [base + e for e in exts]

    if re.search(r"[\\/]", program) or ntpath.isabs(program):
        return with_exts(ntpath.normpath(ntpath.join(cwd, program)))
    dirs = [d.replace('"', "").strip() for d in (_get_env(env, "PATH") or "").split(";")]
    return [c for d in dirs if d for c in with_exts(ntpath.join(d, program))]


def resolve_windows_program(program: str, env: Mapping[str, str], cwd: str) -> Optional[str]:
    for c in _candidates(program, env, cwd):
        if os.path.isfile(c):
            return c
    return None


def _escape_cmd_meta(s: str) -> str:
    return _CMD_META.sub(r"^\1", s)


def _escape_batch_argument(arg: str) -> str:
    quoted = re.sub(r'(\\*)"', lambda m: m.group(1) * 2 + '\\"', arg)
    quoted = re.sub(r"(\\*)$", lambda m: m.group(1) * 2, quoted)
    return _escape_cmd_meta(_escape_cmd_meta(f'"{quoted}"'))


def plan_windows_spawn(argv: Sequence[str], env: Mapping[str, str], cwd: str) -> Optional[SpawnPlan]:
    if not argv:
        return None
    program, args = argv[0], list(argv[1:])
    file = resolve_windows_program(program, env, cwd)
    if file is None:
        return None
    ext = ntpath.splitext(file)[1].lower()
    if ext in (".cmd", ".bat"):
        name = ntpath.basename(file)
        if any(re.search(r"[\r\n]", a) for a in args):
            raise ValueError(
                f"{program} is a batch file ({name}), which runs under cmd.exe, and cmd.exe cannot pass an argument "
                "containing a newline. Pass that text on stdin instead."
            )
        line = " ".join([_escape_cmd_meta(file), *(_escape_batch_argument(a) for a in args)])
        if len(line) > CMD_MAX_COMMAND_LINE:
            raise ValueError(
                f"The command line for {program} ({name}) is too long for cmd.exe: {len(line)} characters once escaped, "
                f"over the {CMD_MAX_COMMAND_LINE}-character limit. Pass long text on stdin instead."
            )
        comspec = _get_env(env, "COMSPEC") or "cmd.exe"
        return SpawnPlan(comspec, ["/d", "/s", "/c", f'"{line}"'], True, dict(env), shell_line=line)
    return SpawnPlan(file, args, False, dict(env))


def plan_windows_exec(command: str, env: Mapping[str, str], cwd: str) -> SpawnPlan:
    simple = parse_simple_command(command)
    if simple is not None:
        cmd_env = {**env, **simple.env}
        plan = plan_windows_spawn(simple.argv, cmd_env, cwd)
        if plan is not None:
            return plan
    return SpawnPlan("cmd.exe", ["/d", "/s", "/c", command], True, dict(env), shell_line=command)
