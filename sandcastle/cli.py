"""The ``sandcastle`` command line. Port of ``cli.ts`` and ``main.ts``.

Commands::

    sandcastle init [--agent --model --sandbox --issue-tracker --template ...]
    sandcastle docker build-image [--image-name N] [--dockerfile PATH]
    sandcastle docker remove-image [--image-name N]
    sandcastle podman build-image [--image-name N] [--containerfile PATH]
    sandcastle podman remove-image [--image-name N]
    sandcastle run  (Python-port extra: an AFK run straight from the shell,
                     with usage-limit auto-resume)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from typing import Callable, List, Optional, Sequence, Tuple

from . import init_service as I
from .display import TerminalDisplay, setup_terminal_cleanup
from .errors import ConfigDirError, InitError, SandcastleError
from .mounts import default_image_name
from .util import format_error_message
from .version import VERSION

CONFIG_DIR = ".sandcastle"
NATIVE_SANDBOX_HINT = "Native sandboxes (bwrap(), seatbelt(), no_sandbox()) run without an image, so you only need one for a container sandbox."


def _tri(value: Optional[str]) -> Optional[bool]:
    return None if value is None else value == "true"


def _non_interactive_message(flags: Sequence[str]) -> str:
    return f"{', '.join(flags)} {'is' if len(flags) == 1 else 'are'} required in non-interactive mode (no TTY detected)."


def _uid_build_args() -> dict:
    args = {}
    if hasattr(os, "getuid"):
        args["AGENT_UID"] = str(os.getuid())
        args["AGENT_GID"] = str(os.getgid())
    return args


# ---------------------------------------------------------------------------
# Terminal prompts (stand-ins for @clack/prompts)
# ---------------------------------------------------------------------------


def _select(message: str, options: List[Tuple[str, str, str]], initial: Optional[str]) -> str:
    """options: (value, label, hint). Returns the chosen value."""
    print(f"◆  {message}")
    default_idx = 0
    for i, (value, label, hint) in enumerate(options, 1):
        mark = "●" if value == initial else "○"
        if value == initial:
            default_idx = i
        print(f"│  {i}. {mark} {label}" + (f"  ({hint})" if hint else ""))
    while True:
        try:
            raw = input(f"└  Choose 1-{len(options)}" + (f" [{default_idx}]" if default_idx else "") + ": ").strip()
        except EOFError:
            raise InitError("Selection cancelled.") from None
        if not raw and default_idx:
            return options[default_idx - 1][0]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        match = [o for o in options if o[0] == raw]
        if match:
            return match[0][0]


def _confirm(message: str, default: bool = True) -> bool:
    try:
        raw = input(f"◆  {message} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    except EOFError:
        raise InitError("Selection cancelled.") from None
    return default if not raw else raw in ("y", "yes")


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    d = TerminalDisplay()
    cwd = os.getcwd()
    interactive = sys.stdin.isatty()

    if args.template and not any(t.name == args.template for t in I.list_templates()):
        raise InitError(f'Unknown template "{args.template}". Available: {", ".join(t.name for t in I.list_templates())}')
    if args.sandbox and I.get_sandbox_provider(args.sandbox) is None:
        raise InitError(f'Unknown sandbox provider "{args.sandbox}". Available: {", ".join(p.name for p in I.list_sandbox_providers())}')
    if args.issue_tracker and I.get_issue_tracker(args.issue_tracker) is None:
        raise InitError(f'Unknown issue tracker "{args.issue_tracker}". Available: {", ".join(t.name for t in I.list_issue_trackers())}')
    if args.agent and I.get_agent(args.agent) is None:
        raise InitError(f'Unknown agent "{args.agent}". Available: {", ".join(a.name for a in I.list_agents())}')

    create_label, build_image, install_deps = _tri(args.create_label), _tri(args.build_image), _tri(args.install_template_deps)
    if not interactive:
        missing = []
        for flag, val in (("--agent", args.agent), ("--sandbox", args.sandbox), ("--issue-tracker", args.issue_tracker), ("--template", args.template)):
            if not val:
                missing.append(flag)
        if args.issue_tracker == "github-issues" and create_label is None:
            missing.append("--create-label")
        if args.template and "pydantic" in I.get_template_dependencies(args.template) and install_deps is None and not I.host_has_python_package("pydantic"):
            missing.append("--install-template-deps")
        sb = I.get_sandbox_provider(args.sandbox) if args.sandbox else None
        if args.issue_tracker != "custom" and (sb is None or sb.image is not None) and build_image is None:
            missing.append("--build-image")
        if missing:
            raise InitError(_non_interactive_message(missing))

    agent = I.get_agent(args.agent) if args.agent else I.get_agent(
        _select("Select an agent:", [(a.name, a.label, f"Default model: {a.default_model}") for a in I.list_agents()], "claude-code")
    )
    assert agent is not None
    model = args.model or agent.default_model
    if args.sandbox:
        provider = I.get_sandbox_provider(args.sandbox)
    else:
        choices = I.list_sandbox_provider_choices()
        rec = next((e.name for e, r in choices if r), None)
        provider = I.get_sandbox_provider(
            _select("Select a sandbox provider:", [(e.name, f"{e.label} (recommended)" if r else e.label, "") for e, r in choices], rec)
        )
    assert provider is not None
    tracker = I.get_issue_tracker(args.issue_tracker) if args.issue_tracker else I.get_issue_tracker(
        _select("Select an issue tracker:", [(t.name, t.label, "") for t in I.list_issue_trackers()], "github-issues")
    )
    assert tracker is not None
    template = args.template or _select("Select a template:", [(t.name, t.name, t.description) for t in I.list_templates()], "blank")

    should_label = False
    if tracker.name == "github-issues":
        should_label = create_label if create_label is not None else _confirm(
            'Create a "Sandcastle" GitHub label? (Templates filter issues by this label)'
        )
        if should_label:
            subprocess.run(
                'gh label create "Sandcastle" --description "Issues for Sandcastle to work on" --color "F9A825"',
                shell=True, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

    async def do_scaffold() -> str:
        return I.scaffold(
            cwd, agent=agent, model=model, template_name=template, create_label=should_label, issue_tracker=tracker,
            sandbox_provider=provider, image_name=args.image_name if provider.image else None,
        )

    asyncio.run(d.spinner("Scaffolding .sandcastle/ config directory...", do_scaffold()))

    warning = I.sandbox_provider_preflight_warning(provider)
    if warning:
        d.status(warning, "warn")
    if provider.image is None:
        w = I.agent_cli_warning(agent, provider.name)
        if w:
            d.status(w, "warn")
    if tracker.name == "beads" and provider.image is not None:
        w = I.beads_version_warning(I.detect_host_bd_version(cwd))
        if w:
            d.status(w, "warn")

    pm = I.detect_package_manager(cwd)
    installed: List[str] = []
    if "pydantic" in I.get_template_dependencies(template):
        if I.host_has_python_package("pydantic"):
            installed.append("pydantic")
        else:
            cmd = I.install_python_package_command("pydantic")
            yes = install_deps if install_deps is not None else _confirm(
                f"The {template} template needs a schema validator. Install pydantic now (`{cmd}`)?"
            )
            if yes:
                ok = subprocess.run(cmd, shell=True, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
                if ok:
                    installed.append("pydantic")
                    d.status("Installed pydantic.", "success")
                else:
                    d.status(f"Couldn't install pydantic automatically. Run `{cmd}` before running the agent.", "warn")

    image_name = args.image_name or default_image_name(cwd)
    if provider.image is None:
        d.status(
            "Init complete! Your custom issue tracker isn't configured yet — see the steps below."
            if tracker.name == "custom"
            else "Init complete!",
            "success",
        )
    elif tracker.name == "custom":
        d.status("Init complete! Your custom issue tracker isn't configured yet — see the steps below before building.", "success")
    else:
        should_build = build_image if build_image is not None else _confirm(f"Build the default {provider.label} image now?")
        if not should_build:
            d.status(f"Init complete! Run `{I.build_image_shell_command(provider.image, args.image_name)}` to build the {provider.label} image later.", "success")
        else:
            asyncio.run(d.spinner(f"Building {provider.label} image '{image_name}'...", _build(provider.name, image_name, cwd, None)))
            d.status("Init complete! Image built successfully.", "success")

    for i, line in enumerate(I.get_next_steps_lines(template, tracker, agent, pm, installed, provider)):
        d.text(line if i == 0 else f"\x1b[2m{line}\x1b[22m" if sys.stdout.isatty() else line)
    return 0


async def _build(engine: str, image_name: str, cwd: str, file: Optional[str]) -> None:
    d = os.path.join(cwd, CONFIG_DIR)
    if engine == "podman":
        from .sandboxes.podman import build_image

        await build_image(image_name, d, containerfile=file)
    else:
        from .sandboxes.docker import build_image as docker_build

        await docker_build(image_name, d, dockerfile=file, build_args=_uid_build_args())


# ---------------------------------------------------------------------------
# docker / podman
# ---------------------------------------------------------------------------


def _require_config_dir(cwd: str) -> None:
    if not os.path.exists(os.path.join(cwd, CONFIG_DIR)):
        raise ConfigDirError("No .sandcastle/ found. Run `sandcastle init` first.")


def cmd_build_image(engine: str, args: argparse.Namespace) -> int:
    d = TerminalDisplay()
    cwd = os.getcwd()
    _require_config_dir(cwd)
    image_name = args.image_name or default_image_name(cwd)
    cfg = os.path.join(cwd, CONFIG_DIR)
    file = getattr(args, "dockerfile", None) if engine == "docker" else getattr(args, "containerfile", None)
    if file is None:
        if engine == "docker":
            if not os.path.exists(os.path.join(cfg, "Dockerfile")):
                if os.path.exists(os.path.join(cfg, "Containerfile")):
                    msg = (
                        f"No Dockerfile found at {CONFIG_DIR}/Dockerfile, but found {CONFIG_DIR}/Containerfile: this project was "
                        f"scaffolded for podman. Run `sandcastle podman build-image`, or pass --dockerfile {CONFIG_DIR}/Containerfile."
                    )
                else:
                    msg = f"No Dockerfile found at {CONFIG_DIR}/Dockerfile. {NATIVE_SANDBOX_HINT} To build one, add {CONFIG_DIR}/Dockerfile or pass --dockerfile <path>."
                raise ConfigDirError(msg)
        elif not any(os.path.exists(os.path.join(cfg, n)) for n in ("Containerfile", "Dockerfile")):
            raise ConfigDirError(
                f"No Containerfile or Dockerfile found in {CONFIG_DIR}/. {NATIVE_SANDBOX_HINT} To build one, add {CONFIG_DIR}/Containerfile or pass --containerfile <path>."
            )
    elif not os.path.exists(os.path.abspath(file)):
        raise ConfigDirError(f"{'Dockerfile' if engine == 'docker' else 'Containerfile'} not found: {file}")
    label = "Docker" if engine == "docker" else "Podman"
    asyncio.run(d.spinner(f"Building {label} image '{image_name}'...", _build(engine, image_name, cwd, file)))
    d.status("Build complete!", "success")
    return 0


def cmd_remove_image(engine: str, args: argparse.Namespace) -> int:
    d = TerminalDisplay()
    image_name = args.image_name or default_image_name(os.getcwd())
    if engine == "podman":
        from .sandboxes.podman import remove_image
    else:
        from .sandboxes.docker import remove_image  # type: ignore[no-redef]
    label = "Docker" if engine == "docker" else "Podman"
    asyncio.run(d.spinner(f"Removing {label} image '{image_name}'...", remove_image(image_name)))
    d.status("Image removed.", "success")
    return 0


# ---------------------------------------------------------------------------
# run (Python-port extra)
# ---------------------------------------------------------------------------


def _make_agent(name: str, model: Optional[str], effort: Optional[str], permission_mode: Optional[str]):
    from . import agent_provider as ap

    entry = I.get_agent(name)
    if entry is None:
        raise ValueError(f'Unknown agent "{name}". Available: {", ".join(a.name for a in I.list_agents())}')
    model = model or entry.default_model
    if name == "claude-code":
        return ap.claude_code(model, effort=effort, permission_mode=permission_mode)
    if name == "codex":
        return ap.codex(model, effort=effort)
    if name == "copilot":
        return ap.copilot(model, effort=effort)
    if name == "pi":
        return ap.pi(model, thinking=effort)
    return getattr(ap, entry.factory_import)(model)


def _make_sandbox(name: str, image_name: Optional[str]):
    if name in ("none", "no-sandbox"):
        from .sandboxes.no_sandbox import no_sandbox

        return no_sandbox()
    if name == "docker":
        from .sandboxes.docker import docker

        return docker(image_name=image_name)
    if name == "podman":
        from .sandboxes.podman import podman

        return podman(image_name=image_name)
    if name == "bwrap":
        from .sandboxes.bwrap import bwrap

        return bwrap()
    if name == "seatbelt":
        from .sandboxes.seatbelt import seatbelt

        return seatbelt()
    raise ValueError(f"Unknown sandbox {name!r}")


def cmd_run(args: argparse.Namespace) -> int:
    from .run import run
    from .sandbox_provider import to_branch_strategy
    from .usage_limit import UsageLimitConfig

    if args.branch and args.merge_to_head:
        raise ValueError("Pass either --branch or --merge-to-head, not both.")
    bs = {"type": "branch", "branch": args.branch} if args.branch else ({"type": "merge-to-head"} if args.merge_to_head else None)
    ul = UsageLimitConfig(
        enabled=not args.no_wait_for_reset,
        buffer_seconds=args.reset_buffer,
        fallback_wait_minutes=args.fallback_wait,
        max_waits=args.max_waits,
    )
    hooks = {"sandbox": {"onSandboxReady": [{"command": c} for c in args.setup]}} if args.setup else None
    result = asyncio.run(
        run(
            agent=_make_agent(args.agent, args.model, args.effort, args.permission_mode),
            sandbox=_make_sandbox(args.sandbox, args.image_name),
            cwd=args.cwd,
            prompt=args.prompt,
            prompt_file=args.prompt_file,
            max_iterations=args.max_iterations,
            name=args.name,
            branch_strategy=to_branch_strategy(bs),
            completion_signal=args.completion_signal,
            idle_timeout_seconds=args.idle_timeout,
            logging={"type": "stdout"} if not args.log_file else {"type": "file", "path": args.log_file},
            hooks=hooks,
            usage_limit=ul,
        )
    )
    print(f"\nCommits: {len(result.commits)}  Branch: {result.branch}  Iterations: {len(result.iterations)}")
    last = result.iterations[-1] if result.iterations else None
    if last and last.session_id:
        print(f"Session: {last.session_id}")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sandcastle", description="Orchestrate AI coding agents in isolated sandboxes.")
    p.add_argument("--version", action="version", version=f"sandcastle {VERSION}")
    sub = p.add_subparsers(dest="command")

    tf = ["true", "false"]
    i = sub.add_parser("init", help="Scaffold the .sandcastle/ config directory")
    i.add_argument("--image-name", help="Image name to build (default sandcastle:<repo-dir-name>)")
    i.add_argument("--template", help="Template to scaffold (e.g. blank, simple-loop, parallel-planner)")
    i.add_argument("--agent", help="Agent to use (e.g. claude-code)")
    i.add_argument("--model", help="Model to use for the agent. Defaults to the agent's default model")
    i.add_argument("--sandbox", help="Sandbox provider to use (bwrap, seatbelt, docker or podman)")
    i.add_argument("--issue-tracker", help="Issue tracker to use (e.g. github-issues, beads, custom)")
    i.add_argument("--create-label", choices=tf, help='Whether to create the "Sandcastle" GitHub label')
    i.add_argument("--build-image", choices=tf, help="Whether to build the sandbox image now")
    i.add_argument("--install-template-deps", choices=tf, help="Whether to install the template's Python dependencies (pydantic)")
    i.set_defaults(func=cmd_init)

    for engine, file_flag in (("docker", "--dockerfile"), ("podman", "--containerfile")):
        e = sub.add_parser(engine, help=f"{engine.capitalize()} sandbox commands")
        esub = e.add_subparsers(dest="subcommand")
        b = esub.add_parser("build-image", help=f"Build the {engine} image from .sandcastle/")
        b.add_argument("--image-name")
        b.add_argument(file_flag, help="Path to a custom file (build context will be the current working directory)")
        b.set_defaults(func=lambda a, en=engine: cmd_build_image(en, a))
        r = esub.add_parser("remove-image", help=f"Remove the {engine} image")
        r.add_argument("--image-name")
        r.set_defaults(func=lambda a, en=engine: cmd_remove_image(en, a))
        e.set_defaults(func=lambda a, en=engine: (TerminalDisplay().status(f"{en.capitalize()} sandbox commands. Use --help to see available subcommands.", "info"), 0)[1])

    r = sub.add_parser(
        "run",
        help="Run an agent AFK (waits out usage limits and resumes)",
        description="Run an agent unattended. When it hits its usage limit (e.g. Claude's 5-hour window), "
        "sandcastle waits for the reset and resumes the same session. NOTE: agents run with their permission "
        "checks skipped; with --sandbox none that is directly on your machine.",
    )
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument("-p", "--prompt", help="Inline prompt (passed to the agent as-is)")
    src.add_argument("-f", "--prompt-file", help="Prompt file ({{KEY}} and !`cmd` expansion)")
    r.add_argument("--sandbox", required=True, choices=["none", "docker", "podman", "bwrap", "seatbelt"], help="Where the agent runs")
    r.add_argument("--agent", default="claude-code", help="claude-code, codex, pi, cursor, opencode or copilot (default claude-code)")
    r.add_argument("-m", "--model", help="Model (default: the agent's default)")
    r.add_argument("--effort", help="Reasoning effort (claude-code/codex/copilot) or thinking level (pi)")
    r.add_argument("--permission-mode", help="Claude permission mode instead of --dangerously-skip-permissions")
    r.add_argument("-C", "--cwd", help="Repo directory (default: current directory)")
    r.add_argument("-n", "--max-iterations", type=int, default=1, help="Maximum iterations (default 1)")
    r.add_argument("--name", help="Run name shown in logs")
    r.add_argument("--branch", help="Commit on this branch, in a worktree")
    r.add_argument("--merge-to-head", action="store_true", help="Work on a temp branch and merge back to HEAD")
    r.add_argument("--completion-signal", action="append", help="Stop early when the agent prints this (repeatable)")
    r.add_argument("--idle-timeout", type=float, help="Idle timeout in seconds (default 600)")
    r.add_argument("--image-name", help="docker/podman image name")
    r.add_argument("--setup", action="append", help="A command to run in the sandbox before the agent (repeatable)")
    r.add_argument("--log-file", help="Log to this file instead of the terminal")
    r.add_argument("--no-wait-for-reset", action="store_true", help="Fail instead of waiting when the usage limit is hit")
    r.add_argument("--reset-buffer", type=float, default=60, help="Seconds to wait past the reset time (default 60)")
    r.add_argument("--fallback-wait", type=float, default=15, help="Minutes between retries when the reset time is unknown (default 15)")
    r.add_argument("--max-waits", type=int, help="Give up after this many usage-limit waits (default unlimited)")
    r.set_defaults(func=cmd_run)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    setup_terminal_cleanup()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        d = TerminalDisplay()
        d.status(f"Sandcastle v{VERSION}", "info")
        d.status("Use --help to see available commands.", "info")
        return 0
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except (SandcastleError, ValueError, RuntimeError, ImportError) as e:
        TerminalDisplay().status(format_error_message(e), "error")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
