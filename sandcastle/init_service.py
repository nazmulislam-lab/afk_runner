"""``sandcastle init`` scaffolding. Port of ``InitService.ts`` and
``templates.ts``.

The Python port scaffolds ``.sandcastle/main.py`` (run with
``python .sandcastle/main.py``) instead of ``main.mts``. Dockerfiles,
``.env.example`` blocks, issue-tracker commands and prompts are the
original's, verbatim. The planner templates validate their ``<plan>`` with
pydantic instead of Zod.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from . import init_assets as A
from .errors import InitError, NativeSandboxUnavailableError

SETUP_ISSUE_TRACKER_DOC = "SETUP_ISSUE_TRACKER.md"
SETUP_ISSUE_TRACKER_PATH = f".sandcastle/{SETUP_ISSUE_TRACKER_DOC}"
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
MAIN_FILENAME = "main.py"
RUN_COMMAND = "python .sandcastle/main.py"

SKELETON_PROMPT = """# Context

<!-- Use !`command` to pull in dynamic context. Commands run inside the sandbox. -->
<!-- Example: !`git log --oneline -10` or !`gh issue list --label Sandcastle --json number,title` -->

# Task

<!-- Describe what the agent should do. -->

# Done

<!-- When the task is complete, output <promise>COMPLETE</promise> to signal early termination. -->
"""

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplateMetadata:
    name: str
    description: str
    #: Python packages the template's main.py imports directly.
    dependencies: Sequence[str] = ()


TEMPLATES = [
    TemplateMetadata("blank", "Bare scaffold — write your own prompt and orchestration"),
    TemplateMetadata("simple-loop", "Picks issues one by one and closes them"),
    TemplateMetadata("sequential-reviewer", "Implements issues one by one, with a code review step after each"),
    TemplateMetadata("parallel-planner", "Plans parallelizable issues, executes on separate branches, merges", ("pydantic",)),
    TemplateMetadata("parallel-planner-with-review", "Plans parallelizable issues, executes with per-branch review, merges", ("pydantic",)),
]


def list_templates() -> List[TemplateMetadata]:
    return list(TEMPLATES)


def get_template_dependencies(name: str) -> Sequence[str]:
    return next((t.dependencies for t in TEMPLATES if t.name == name), ())


def host_has_python_package(pkg: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(pkg) is not None


def install_python_package_command(pkg: str) -> str:
    return f"{sys.executable} -m pip install {pkg}"


# ---------------------------------------------------------------------------
# Node package manager detection (for the target project's own deps)
# ---------------------------------------------------------------------------

PACKAGE_MANAGERS = ("npm", "pnpm", "yarn", "bun")
LOCKFILES = (("bun.lockb", "bun"), ("bun.lock", "bun"), ("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("package-lock.json", "npm"))


def _read_package_json(repo_dir: str) -> Optional[dict]:
    try:
        with open(os.path.join(repo_dir, "package.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def detect_package_manager(repo_dir: str) -> str:
    pkg = _read_package_json(repo_dir) or {}
    field_ = pkg.get("packageManager")
    if isinstance(field_, str):
        name = field_.split("@")[0]
        if name in PACKAGE_MANAGERS:
            return name
    for f, pm in LOCKFILES:
        if os.path.exists(os.path.join(repo_dir, f)):
            return pm
    return "npm"


def install_command(pm: str) -> str:
    return f"{pm} install"


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentEntry:
    name: str
    label: str
    default_model: str
    factory_import: str
    dockerfile_template: str
    env_example: str
    setup_command: str
    host_cli_command: str
    host_cli_install: str


AGENT_REGISTRY = [
    AgentEntry("claude-code", "Claude Code", "claude-opus-4-8", "claude_code", A.CLAUDE_CODE_DOCKERFILE, A.ENV_EXAMPLES[0],
               f'claude "$(cat {SETUP_ISSUE_TRACKER_PATH})"', "claude", "curl -fsSL https://claude.ai/install.sh | bash"),
    AgentEntry("pi", "Pi", "claude-sonnet-4-6", "pi", A.PI_DOCKERFILE, A.ENV_EXAMPLES[1],
               f'pi "$(cat {SETUP_ISSUE_TRACKER_PATH})"', "pi", "npm install -g @earendil-works/pi-coding-agent"),
    AgentEntry("codex", "Codex", "gpt-5.4", "codex", A.CODEX_DOCKERFILE, A.ENV_EXAMPLES[2],
               f'codex "$(cat {SETUP_ISSUE_TRACKER_PATH})"', "codex", "npm install -g @openai/codex"),
    AgentEntry("cursor", "Cursor", "composer-2", "cursor", A.CURSOR_DOCKERFILE, A.ENV_EXAMPLES[3],
               f'agent "$(cat {SETUP_ISSUE_TRACKER_PATH})"', "agent", "curl https://cursor.com/install -fsS | bash"),
    AgentEntry("opencode", "OpenCode", "opencode/big-pickle", "opencode", A.OPENCODE_DOCKERFILE, A.ENV_EXAMPLES[4],
               f'opencode --prompt "$(cat {SETUP_ISSUE_TRACKER_PATH})"', "opencode", "npm install -g opencode-ai@latest"),
    AgentEntry("copilot", "GitHub Copilot CLI", "claude-sonnet-4.5", "copilot", A.COPILOT_DOCKERFILE, A.ENV_EXAMPLES[5],
               f'copilot -i "$(cat {SETUP_ISSUE_TRACKER_PATH})"', "copilot", "npm install -g @github/copilot"),
]


def list_agents() -> List[AgentEntry]:
    return list(AGENT_REGISTRY)


def get_agent(name: str) -> Optional[AgentEntry]:
    return next((a for a in AGENT_REGISTRY if a.name == name), None)


def agent_cli_warning(agent: AgentEntry, provider_name: str) -> Optional[str]:
    if shutil.which(agent.host_cli_command):
        return None
    return (
        f"The {agent.label} CLI (`{agent.host_cli_command}`) isn't on your PATH, and {provider_name}() runs the host's "
        f"own agent CLI. Install it with: {agent.host_cli_install}"
    )


# ---------------------------------------------------------------------------
# Issue trackers
# ---------------------------------------------------------------------------

BEADS_VERSION = "1.3.2"
BEADS_SANDBOX_DIR = "/home/agent/host-beads"

BEADS_SANDBOX_OPTIONS_PY = f'''# Beads keeps its database in .beads/, which git ignores, so a worktree
# sandbox never gets it. Mount the host's .beads/ into every sandbox and point
# bd at it, so every phase reads and closes issues in the one shared database.
# Run this file from the repo root (host_path is relative to the cwd).
sandbox_options = {{
    "mounts": [{{"host_path": ".beads", "sandbox_path": "{BEADS_SANDBOX_DIR}"}}],
    "env": {{"BEADS_DIR": "{BEADS_SANDBOX_DIR}"}},
}}'''

BEADS_NATIVE_SANDBOX_OPTIONS_PY = '''# Beads keeps its database in .beads/, which git ignores, so a worktree
# sandbox never gets it. Mount the host's .beads/ into every sandbox, at its
# own path, and point bd at it, so every phase reads and closes issues in the
# one shared database. Run this file from the repo root.
import os as _os

beads_dir = _os.path.join(_os.getcwd(), ".beads")
sandbox_options = {
    "mounts": [{"host_path": beads_dir, "sandbox_path": beads_dir}],
    "env": {"BEADS_DIR": beads_dir},
}'''


@dataclass(frozen=True)
class IssueTrackerEntry:
    name: str
    label: str
    template_args: Dict[str, str]
    env_example: str
    sandbox_options: Optional[str] = None
    native_sandbox_options: Optional[str] = None


ISSUE_TRACKER_REGISTRY = [
    IssueTrackerEntry(
        "github-issues",
        "GitHub Issues",
        {
            "LIST_TASKS_COMMAND": "gh issue list --state open --label Sandcastle --limit 100 --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'",
            "VIEW_TASK_COMMAND": "gh issue view <ID>",
            "CLOSE_TASK_COMMAND": 'gh issue close <ID> --comment "Completed by Sandcastle"',
            "ISSUE_TRACKER_TOOLS": A.GITHUB_CLI_TOOLS,
        },
        A.ENV_EXAMPLES[6],
    ),
    IssueTrackerEntry(
        "beads",
        "Beads",
        {
            "LIST_TASKS_COMMAND": "bd ready --json",
            "VIEW_TASK_COMMAND": "bd show <ID>",
            "CLOSE_TASK_COMMAND": 'bd close <ID> --reason="Completed by Sandcastle"',
            "ISSUE_TRACKER_TOOLS": A.BEADS_TOOLS,
        },
        "",
        BEADS_SANDBOX_OPTIONS_PY,
        BEADS_NATIVE_SANDBOX_OPTIONS_PY,
    ),
    IssueTrackerEntry(
        "custom",
        "Custom",
        {
            "LIST_TASKS_COMMAND": A.CUSTOM_LIST_TASKS_SENTINEL,
            "VIEW_TASK_COMMAND": A.CUSTOM_VIEW_TASK_MARKER,
            "CLOSE_TASK_COMMAND": A.CUSTOM_CLOSE_TASK_MARKER,
            "ISSUE_TRACKER_TOOLS": A.CUSTOM_TRACKER_TOOLS,
        },
        A.CUSTOM_ENV_EXAMPLE,
    ),
]


def list_issue_trackers() -> List[IssueTrackerEntry]:
    return list(ISSUE_TRACKER_REGISTRY)


def get_issue_tracker(name: str) -> Optional[IssueTrackerEntry]:
    return next((t for t in ISSUE_TRACKER_REGISTRY if t.name == name), None)


def parse_bd_version(output: str) -> Optional[str]:
    m = re.search(r"\bbd version v?(\d+\.\d+\.\d+)\b", output)
    return m.group(1) if m else None


def _cmp(a: str, b: str) -> int:
    pa, pb = [int(x) for x in a.split(".")], [int(x) for x in b.split(".")]
    for i in range(3):
        d = (pa[i] if i < len(pa) else 0) - (pb[i] if i < len(pb) else 0)
        if d:
            return d
    return 0


def beads_version_warning(host_version: Optional[str]) -> Optional[str]:
    if host_version == BEADS_VERSION:
        return None
    shared = f"the image installs bd {BEADS_VERSION}, and every sandbox opens your .beads/ with it"
    if host_version is None:
        return (
            f"bd isn't on your PATH, and {shared}. Use bd {BEADS_VERSION} on the host too, or an older host bd can no "
            "longer create issues once a sandbox has opened the database."
        )
    if _cmp(host_version, BEADS_VERSION) < 0:
        return (
            f"Your host runs bd {host_version}, but {shared}. bd {BEADS_VERSION} migrates the database, after which host bd "
            f"{host_version} can no longer create issues in it. Upgrade the host's bd to {BEADS_VERSION} before you run."
        )
    return (
        f"Your host runs bd {host_version}, but {shared}, and an older bd can't safely write a database a newer one has "
        f"migrated. Set ARG BEADS_VERSION in the Dockerfile to {host_version} and rebuild the image, or use bd "
        f"{BEADS_VERSION} on the host."
    )


def detect_host_bd_version(cwd: str) -> Optional[str]:
    try:
        r = subprocess.run(["bd", "version"], cwd=cwd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_bd_version(f"{r.stdout}\n{r.stderr}")


# ---------------------------------------------------------------------------
# Sandbox providers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxProviderImage:
    containerfile_name: str
    cli_namespace: str


def _bwrap_preflight() -> None:
    from .sandboxes.bwrap import assert_bwrap_usable

    assert_bwrap_usable()


def _seatbelt_preflight() -> None:
    from .sandboxes.seatbelt import assert_seatbelt_usable

    assert_seatbelt_usable()


@dataclass(frozen=True)
class SandboxProviderEntry:
    name: str
    label: str
    image: Optional[SandboxProviderImage] = None
    native_platform: Optional[str] = None
    preflight: Optional[Callable[[], None]] = None


SANDBOX_PROVIDER_REGISTRY = [
    SandboxProviderEntry("docker", "Docker", SandboxProviderImage("Dockerfile", "docker")),
    SandboxProviderEntry("podman", "Podman", SandboxProviderImage("Containerfile", "podman")),
    SandboxProviderEntry("bwrap", "Bubblewrap", None, "linux", _bwrap_preflight),
    SandboxProviderEntry("seatbelt", "Seatbelt", None, "darwin", _seatbelt_preflight),
]


def list_sandbox_providers() -> List[SandboxProviderEntry]:
    return list(SANDBOX_PROVIDER_REGISTRY)


def get_sandbox_provider(name: str) -> Optional[SandboxProviderEntry]:
    return next((p for p in SANDBOX_PROVIDER_REGISTRY if p.name == name), None)


def _platform() -> str:
    return "linux" if sys.platform.startswith("linux") else sys.platform


def list_sandbox_provider_choices(platform: Optional[str] = None):
    p = platform or _platform()
    native = [(e, True) for e in SANDBOX_PROVIDER_REGISTRY if e.native_platform == p]
    others = [(e, False) for e in SANDBOX_PROVIDER_REGISTRY if e.native_platform is None]
    return native + others


def sandbox_provider_preflight_warning(entry: SandboxProviderEntry) -> Optional[str]:
    try:
        if entry.preflight:
            entry.preflight()
        return None
    except Exception as e:  # noqa: BLE001
        return f"This host can't run {entry.name}() yet, so the scaffolded main file fails until you fix it. {e}"


def build_image_shell_command(image: SandboxProviderImage, image_name: Optional[str] = None) -> str:
    return f"sandcastle {image.cli_namespace} build-image" + (f" --image-name {image_name}" if image_name else "")


# ---------------------------------------------------------------------------
# Next steps
# ---------------------------------------------------------------------------


def _env_notes(tracker: IssueTrackerEntry, agent: AgentEntry, provider: SandboxProviderEntry) -> List[str]:
    out = []
    if agent.name == "claude-code":
        out.append(
            "   To use your Claude subscription instead of an API key, run `claude setup-token` on your host and paste "
            "the result into CLAUDE_CODE_OAUTH_TOKEN."
        )
    if tracker.name == "github-issues" and provider.image is None:
        out.append(f"   {provider.name}() hides your host's `gh` login from the agent, so put a GH_TOKEN in .sandcastle/.env for `gh` to use.")
    return out


def _image_package_manager_note(pm: str, tracker: IssueTrackerEntry, provider: SandboxProviderEntry) -> str:
    if provider.image is None or pm == "npm":
        return ""
    f = f".sandcastle/{provider.image.containerfile_name}"
    if pm == "bun":
        return f", then install bun in {f}, before its `USER` line (`RUN npm install -g bun`), and rebuild the image"
    if "corepack enable" in tracker.template_args["ISSUE_TRACKER_TOOLS"]:
        return ""
    return f", then add `RUN corepack enable` to {f}, before its `USER` line, and rebuild the image"


def get_next_steps_lines(
    template: str,
    tracker: IssueTrackerEntry,
    agent: AgentEntry,
    package_manager: str,
    installed_dependencies: Sequence[str],
    provider: Optional[SandboxProviderEntry] = None,
) -> List[str]:
    provider = provider or SANDBOX_PROVIDER_REGISTRY[0]
    run_step = f"Run `{RUN_COMMAND}` to start the agent"
    if tracker.name == "custom":
        has_image = provider.image is not None
        return [
            "Next steps:",
            "1. Your custom issue tracker isn't wired up yet — runs hard-fail until you configure it.",
            f"2. Feed the setup prompt to {agent.label} on your host to finish wiring it up:",
            f"   {agent.setup_command}",
            f"   (Runs on the host — you need the {agent.label} CLI installed locally, since the sandbox image isn't built yet.)"
            if has_image
            else f"   (Runs on the host — you need the {agent.label} CLI installed locally.)",
            f"3. Follow .sandcastle/{SETUP_ISSUE_TRACKER_DOC} to edit the scaffolded files in place, build the image, and verify."
            if has_image
            else f"3. Follow .sandcastle/{SETUP_ISSUE_TRACKER_DOC} to edit the scaffolded files in place and verify.",
        ]
    if template == "blank":
        return [
            "Next steps:",
            "1. Set the required env vars in .sandcastle/.env (see .sandcastle/.env.example)",
            *_env_notes(tracker, agent, provider),
            "2. Read and customize .sandcastle/prompt.md to describe what you want the agent to do",
            f"3. Customize .sandcastle/{MAIN_FILENAME} — it uses the Python API (`run()`) to control how the agent runs",
            f"4. {run_step}",
        ]
    step = [1]

    def n() -> int:
        v = step[0]
        step[0] += 1
        return v

    pm = package_manager
    copy_step = (
        'Templates use `copy_to_worktree=["node_modules"]` to copy your host node_modules into the sandbox for fast startup — '
        "the `npm install` in the onSandboxReady hook is a safety net for platform-specific binaries. "
        + (
            "Adjust both if you use a different package manager (or remove them for a non-Node project)"
            if pm == "npm"
            else f"This project uses {pm}, so change the hook to `{install_command(pm)}` and check `copy_to_worktree` suits it"
            + _image_package_manager_note(pm, tracker, provider)
        )
    )
    lines = [
        "Next steps:",
        f"{n()}. Set the required env vars in .sandcastle/.env (see .sandcastle/.env.example)",
        *_env_notes(tracker, agent, provider),
        f"{n()}. {copy_step}",
    ]
    if "pydantic" in get_template_dependencies(template) and "pydantic" not in installed_dependencies:
        lines.append(
            f"{n()}. Install a validator for the planner's `<plan>` output — the template uses pydantic "
            f"(`{install_python_package_command('pydantic')}`), but any callable or schema object works"
        )
    lines.append(f"{n()}. Read and customize the prompt files in .sandcastle/ — they shape what the agent does")
    if "review" in template:
        lines.append(f"{n()}. Customize .sandcastle/CODING_STANDARDS.md with your project's standards — the reviewer agent loads it during review")
    lines.append(f"{n()}. {run_step}")
    return lines


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------


def _rewrite_main_content(content: str, agent: AgentEntry, model: str, provider: SandboxProviderEntry, image_name: Optional[str]) -> str:
    content = re.sub(r"\bclaude_code\b", agent.factory_import, content)
    content = re.sub(rf"{agent.factory_import}\([\"']([^\"']+)[\"']\)", f'{agent.factory_import}("{model}")', content)
    content = re.sub(r"\bdocker\b", provider.name, content)
    if image_name is not None and provider.image is not None:
        content = re.sub(rf"\b{provider.name}\(\)", f"{provider.name}(image_name={json.dumps(image_name)})", content)
    return content


def _apply_sandbox_options(content: str, tracker: IssueTrackerEntry, provider: SandboxProviderEntry) -> str:
    opts = (tracker.native_sandbox_options or tracker.sandbox_options) if provider.image is None else tracker.sandbox_options
    if not opts:
        return content
    import_re = re.compile(rf"^from sandcastle\.sandboxes\.{provider.name} import {provider.name}$", re.M)
    if not import_re.search(content):
        raise InitError(f"Cannot apply {tracker.label} sandbox options: {MAIN_FILENAME} has no {provider.name} import.")
    content = import_re.sub(lambda m: f"{m.group(0)}\n\n{opts}", content, count=1)
    return re.sub(rf"\b{provider.name}\(\)", f"{provider.name}(**sandbox_options)", content)


def _is_text_file(name: str) -> bool:
    if name in ("Dockerfile", "Containerfile", ".gitignore"):
        return True
    return os.path.splitext(name)[1] in (".md", ".txt", ".env", ".example")


def _build_setup_doc(build_command: Optional[str]) -> str:
    tools = (
        ""
        if build_command is None
        else f"- **Dockerfile / Containerfile** — replace the line\n\n  ```\n  {A.CUSTOM_TRACKER_TOOLS}\n  ```\n\n  with the install steps for your tracker's CLI (if it needs one).\n\n"
    )
    verify = (
        "## 4. Verify\n\nRun your **list** command and confirm it returns the open tasks as JSON. If it errors, fix the command or the auth.\n"
        if build_command is None
        else f"## 4. Build the image\n\nOnce the files are wired up, build the sandbox image:\n\n```\n{build_command}\n```\n\n## 5. Verify\n\nRun your **list** command inside the built image and confirm it returns the open tasks as JSON. If it errors, fix the command or the auth and rebuild.\n"
    )
    return f"""# Set up your custom issue tracker

You are a coding agent. Finish wiring up the **custom issue tracker** for this Sandcastle project. It was scaffolded in a deliberately broken-until-configured state: until you complete the steps below, every Sandcastle run hard-fails with a pointer back to this file.

## Goal

Wire up the issue tracker so the scaffolded prompts can **list**, **view**, and **close** tasks. There is no runtime abstraction to implement — the tracker commands are baked into the scaffolded files, so you edit those files **in place**.

## 1. Interview the user

Ask the user:

- Which issue tracker do they use (e.g. Jira, Linear, a GitHub repo other than this one, an internal API)?
- How should the sandbox authenticate — a CLI that is already logged in, or an API token? If a token, what is the environment variable name?

## 2. Produce three commands

Work out, together with the user, the shell commands for:

- **list** — print all open tasks **as JSON** (match the shape the built-in trackers emit: an array of objects, each with at least an id/number, title, and body). This is what the agent reads at the start of every iteration.
- **view** `<ID>` — show a single task by id.
- **close** `<ID>` — close a single task by id.

## 3. Edit the scaffolded files in place

{tools}- **Prompt files (`.sandcastle/*.md`)** — replace the sentinel

  ```
  {A.CUSTOM_LIST_TASKS_SENTINEL}
  ```

  with your **list** command. In the prompt file the sentinel sits inside a Sandcastle **shell expression** — a leading `!` followed by the command in backticks — whose output is injected into the prompt before each run. Keep that `!` and the surrounding backticks; replace only the command between them, and **remove the `exit 1`** (leaving it keeps every run hard-failing). Then replace the `{A.CUSTOM_VIEW_TASK_MARKER}` and `{A.CUSTOM_CLOSE_TASK_MARKER}` markers with your **view** and **close** commands.

- **`.env.example`** — replace the `# TODO` block with the real env var(s) your tracker needs, then tell the user to set them in `.sandcastle/.env`.

{verify}"""


def scaffold(
    repo_dir: str,
    *,
    agent: AgentEntry,
    model: str,
    template_name: str = "blank",
    create_label: bool = True,
    issue_tracker: Optional[IssueTrackerEntry] = None,
    sandbox_provider: Optional[SandboxProviderEntry] = None,
    image_name: Optional[str] = None,
) -> str:
    """Write ``.sandcastle/``. Returns the main file name."""
    tracker = issue_tracker or ISSUE_TRACKER_REGISTRY[0]
    provider = sandbox_provider or SANDBOX_PROVIDER_REGISTRY[0]
    config_dir = os.path.join(repo_dir, ".sandcastle")
    if os.path.exists(config_dir):
        raise InitError(".sandcastle/ directory already exists. Remove it first if you want to re-initialize.")
    if not any(t.name == template_name for t in TEMPLATES):
        raise InitError(f'Unknown template: "{template_name}". Available: {", ".join(t.name for t in TEMPLATES)}')
    template_dir = os.path.join(TEMPLATES_DIR, template_name)
    try:
        os.mkdir(config_dir)
        env_parts = [agent.env_example] + ([tracker.env_example] if tracker.env_example else [])
        if provider.image is not None:
            with open(os.path.join(config_dir, provider.image.containerfile_name), "w", encoding="utf-8") as f:
                f.write(agent.dockerfile_template)
        with open(os.path.join(config_dir, ".gitignore"), "w", encoding="utf-8") as f:
            f.write(A.GITIGNORE)
        with open(os.path.join(config_dir, ".env.example"), "w", encoding="utf-8") as f:
            f.write("\n".join(env_parts) + "\n")
        for name in os.listdir(template_dir):
            if name in ("template.json", ".env.example", "__pycache__") or name.endswith((".pyc",)):
                continue
            shutil.copyfile(os.path.join(template_dir, name), os.path.join(config_dir, name))
        main_path = os.path.join(config_dir, MAIN_FILENAME)
        if os.path.exists(main_path):
            with open(main_path, "r", encoding="utf-8") as f:
                content = f.read()
            content = _rewrite_main_content(content, agent, model, provider, image_name if provider.image else None)
            content = _apply_sandbox_options(content, tracker, provider)
            with open(main_path, "w", encoding="utf-8") as f:
                f.write(content)
        for name in os.listdir(config_dir):
            if not _is_text_file(name):
                continue
            p = os.path.join(config_dir, name)
            with open(p, "r", encoding="utf-8") as f:
                content = f.read()
            new = content
            for k, v in tracker.template_args.items():
                new = new.replace("{{" + k + "}}", v)
            if not create_label and name.endswith(".md"):
                new = new.replace(" --label Sandcastle", "")
            if new != content:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(new)
        if tracker.name == "custom":
            build = build_image_shell_command(provider.image, image_name) if provider.image else None
            with open(os.path.join(config_dir, SETUP_ISSUE_TRACKER_DOC), "w", encoding="utf-8") as f:
                f.write(_build_setup_doc(build))
    except OSError as e:
        raise InitError(str(e)) from e
    return MAIN_FILENAME
