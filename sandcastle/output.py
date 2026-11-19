"""Structured output. Port of ``Output.ts`` and ``extractStructuredOutput.ts``.

``Output.object(tag=..., schema=...)`` asks the agent for JSON inside an XML
tag and validates it against a schema. The schema can be:

* a pydantic ``BaseModel`` subclass (``model_validate``),
* a pydantic ``TypeAdapter`` (``validate_python``),
* any object with a ``validate(value)`` method that returns the value or raises,
* a plain callable ``fn(value) -> value`` that raises on invalid input,
* a JSON-schema ``dict`` (validated with ``jsonschema`` when it is installed),
* ``None`` to accept any JSON.

``Output.string(tag=...)`` returns the tag contents trimmed.
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Union


@dataclass(frozen=True)
class OutputObjectDefinition:
    tag: str
    schema: Any = None
    #: Extra attempts after the first when extraction or validation fails.
    max_retries: int = 0
    _tag: Literal["object"] = "object"


@dataclass(frozen=True)
class OutputStringDefinition:
    tag: str
    max_retries: int = 0
    _tag: Literal["string"] = "string"


OutputDefinition = Union[OutputObjectDefinition, OutputStringDefinition]


class Output:
    """Helpers for declaring structured output on ``run()``."""

    @staticmethod
    def object(tag: str, schema: Any = None, max_retries: int = 0) -> OutputObjectDefinition:
        return OutputObjectDefinition(tag=tag, schema=schema, max_retries=max_retries)

    @staticmethod
    def string(tag: str, max_retries: int = 0) -> OutputStringDefinition:
        return OutputStringDefinition(tag=tag, max_retries=max_retries)


class StructuredOutputError(Exception):
    """Raised by ``run()`` when structured output extraction or validation fails.

    ``raw_matched`` is ``None`` when the tag was not found. ``cause`` carries
    the JSON parse error or the validation issues. ``commits``, ``branch`` and
    ``preserved_worktree_path`` keep the run's side effects; ``session_id``
    (and ``session_file_path``) let you resume the session to ask for a fix.
    """

    _tag = "StructuredOutputError"
    name = "StructuredOutputError"

    def __init__(
        self,
        message: str,
        *,
        tag: str,
        raw_matched: Optional[str],
        cause: Any = None,
        commits: Optional[List[Dict[str, str]]] = None,
        branch: str = "",
        preserved_worktree_path: Optional[str] = None,
        session_id: Optional[str] = None,
        session_file_path: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.tag = tag
        self.raw_matched = raw_matched
        self.cause = cause
        self.commits = list(commits or [])
        self.branch = branch
        self.preserved_worktree_path = preserved_worktree_path
        self.session_id = session_id
        self.session_file_path = session_file_path
        if isinstance(cause, BaseException):
            self.__cause__ = cause


@dataclass
class ExtractionContext:
    commits: List[Dict[str, str]] = field(default_factory=list)
    branch: str = ""
    preserved_worktree_path: Optional[str] = None
    session_id: Optional[str] = None
    session_file_path: Optional[str] = None

    def as_kwargs(self) -> Dict[str, Any]:
        return {
            "commits": self.commits,
            "branch": self.branch,
            "preserved_worktree_path": self.preserved_worktree_path,
            "session_id": self.session_id,
            "session_file_path": self.session_file_path,
        }


def find_last_tag_content(text: str, tag: str) -> Optional[str]:
    """Content between the LAST ``<tag>`` / ``</tag>`` pair, or ``None``."""
    open_tag, close_tag = f"<{tag}>", f"</{tag}>"
    last: Optional[str] = None
    search_from = 0
    while True:
        oi = text.find(open_tag, search_from)
        if oi == -1:
            break
        start = oi + len(open_tag)
        ci = text.find(close_tag, start)
        if ci == -1:
            break
        last = text[start:ci]
        search_from = ci + len(close_tag)
    return last


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(?P<body>[\s\S]*?)\n\s*```\s*$")


def unwrap_fences(text: str) -> str:
    m = _FENCE_RE.match(text)
    return m.group("body").strip() if m else text


class SchemaValidationIssues(Exception):
    def __init__(self, issues: Any) -> None:
        super().__init__(str(issues))
        self.issues = issues


async def validate_with_schema(schema: Any, value: Any) -> Any:
    """Validate ``value`` against any supported schema; raise
    :class:`SchemaValidationIssues` on failure."""
    if schema is None:
        return value
    try:
        if isinstance(schema, dict):
            try:
                import jsonschema  # type: ignore
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("A JSON-schema dict needs the 'jsonschema' package: pip install jsonschema") from e
            errors = [err.message for err in jsonschema.Draft202012Validator(schema).iter_errors(value)]
            if errors:
                raise SchemaValidationIssues(errors)
            return value
        if inspect.isclass(schema) and hasattr(schema, "model_validate"):
            return schema.model_validate(value)
        if hasattr(schema, "validate_python"):
            return schema.validate_python(value)
        if hasattr(schema, "~standard"):
            result = getattr(schema, "~standard")["validate"](value)
            if inspect.isawaitable(result):
                result = await result
            if result.get("issues"):
                raise SchemaValidationIssues(result["issues"])
            return result.get("value")
        if hasattr(schema, "validate") and not inspect.isclass(schema):
            result = schema.validate(value)
        elif callable(schema):
            result = schema(value)
        else:
            raise TypeError(f"Unsupported output schema: {schema!r}")
        if inspect.isawaitable(result):
            result = await result
        return value if result is None else result
    except SchemaValidationIssues:
        raise
    except RuntimeError:
        raise
    except Exception as e:  # pydantic.ValidationError, ValueError, ...
        issues = e.errors() if hasattr(e, "errors") and callable(e.errors) else str(e)
        raise SchemaValidationIssues(issues) from e


async def extract_structured_output(stdout: str, definition: OutputDefinition, context: ExtractionContext) -> Any:
    """Find the last ``<tag>...</tag>``; for objects unwrap fences, parse JSON
    and validate; for strings trim. Raises :class:`StructuredOutputError`."""
    raw = find_last_tag_content(stdout, definition.tag)
    if raw is None:
        raise StructuredOutputError(
            f"Structured output tag <{definition.tag}> not found in agent output",
            tag=definition.tag,
            raw_matched=None,
            **context.as_kwargs(),
        )
    if isinstance(definition, OutputStringDefinition):
        return raw.strip()
    unwrapped = unwrap_fences(raw.strip())
    try:
        parsed = json.loads(unwrapped)
    except ValueError as e:
        raise StructuredOutputError(
            f"Structured output tag <{definition.tag}> contains invalid JSON",
            tag=definition.tag,
            raw_matched=raw,
            cause=e,
            **context.as_kwargs(),
        ) from e
    try:
        return await validate_with_schema(definition.schema, parsed)
    except SchemaValidationIssues as e:
        raise StructuredOutputError(
            f"Structured output tag <{definition.tag}> failed schema validation",
            tag=definition.tag,
            raw_matched=raw,
            cause=e.issues,
            **context.as_kwargs(),
        ) from e
