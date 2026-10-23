"""Detect a SKILL.md read made through Bash or ExecuteCode.

The skills middleware auto-loads a skill (and unlocks the tools it gates) when
the agent reads its SKILL.md with ``Read``. Agents also read it with
``cat``/``sed`` or ``open(...)``; without this the skill's body is in context
but its tools stay hidden, and the agent falls back to doing the work by hand.

Kept conservative: only a reader command (or ``open``/``Path.read_text``) whose
literal path argument is a known skill's ``.agents/skills/<name>/SKILL.md``
counts. Listing, globbing, ``cd`` + relative names and computed paths do not.
"""

from __future__ import annotations

import ast
import re
import shlex
from collections.abc import Mapping

BASH_TOOL_NAME = "Bash"
EXECUTE_CODE_TOOL_NAME = "ExecuteCode"

_SKILLS_ROOT = ".agents/"
_READER_COMMANDS = frozenset({"cat", "head", "tail", "sed", "less", "more", "nl", "bat"})
_SHELL_PUNCTUATION = "();<>|&\n"
_PY_PATH_CTORS = frozenset({"Path", "PurePath", "PosixPath"})
_PY_MODULE_OPENS = frozenset({"io", "codecs", "builtins"})
_WRITE_MODE_CHARS = frozenset("wax+")
# `<<WORD`, `<<-WORD`, `<<'WORD'`, `<<"WORD"`; not the `<<<` here-string.
_HEREDOC_RE = re.compile(r"(?<!<)<<-?(?!<)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _match_path(path: str, skill_md_to_name: Mapping[str, str]) -> str | None:
    """Exact match of a literal path against ``.agents/<skill_md_path>``."""
    if not path.endswith("/SKILL.md"):
        return None
    for md_path, skill_name in skill_md_to_name.items():
        rooted = _SKILLS_ROOT + md_path
        if path == rooted or path.endswith("/" + rooted):
            return skill_name
    return None


def _strip_heredoc_bodies(command: str) -> str:
    """Drop heredoc bodies: their lines are data, not commands."""
    kept: list[str] = []
    terminators: list[str] = []
    for line in command.split("\n"):
        if terminators:
            if line.strip() == terminators[0]:
                terminators.pop(0)
            continue
        kept.append(line)
        terminators.extend(m.group(2) for m in _HEREDOC_RE.finditer(line))
    return "\n".join(kept)


def _bash_paths(command: str) -> list[str]:
    """Literal file arguments of reader commands in a shell command line.

    Newlines, ``;``, ``&&``, ``||``, ``|``, ``&`` and parentheses separate
    commands. Output redirection targets (``> P``, ``>> P``, ``2> P``) and
    heredoc delimiters are skipped; ``< P`` feeds P to the reader, so it counts.
    """
    lexer = shlex.shlex(
        _strip_heredoc_bodies(command), posix=True, punctuation_chars=_SHELL_PUNCTUATION
    )
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return []

    paths: list[str] = []
    words: list[str] = []
    inputs: list[str] = []

    def flush() -> None:
        # Skip leading VAR=value assignments.
        while words and "=" in words[0] and not words[0].startswith("/"):
            words.pop(0)
        if words and words[0].rsplit("/", 1)[-1] in _READER_COMMANDS:
            paths.extend(w for w in words[1:] if not w.startswith("-"))
            paths.extend(inputs)
        words.clear()
        inputs.clear()

    skip_next = False
    read_next = False
    for token in tokens:
        if token and all(c in _SHELL_PUNCTUATION for c in token):
            if "<" in token or ">" in token:
                # `<` reads its target; `>`, `>>`, `>&`, `<<`, `<<<` do not.
                read_next = token == "<"
                skip_next = not read_next
                continue
            flush()
            skip_next = read_next = False
            continue
        if skip_next:
            skip_next = False
            continue
        if read_next:
            read_next = False
            inputs.append(token)
            continue
        words.append(token)
    flush()
    return paths


def _str_value(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_arg(call: ast.Call, index: int, keyword: str) -> ast.expr | None:
    if len(call.args) > index:
        return call.args[index]
    for kw in call.keywords:
        if kw.arg == keyword:
            return kw.value
    return None


def _is_read_mode(mode_node: ast.expr | None) -> bool:
    """Absent mode, or a literal one that reads without writing."""
    if mode_node is None:
        return True
    mode = _str_value(mode_node)
    return mode is not None and "r" in mode and not (set(mode) & _WRITE_MODE_CHARS)


def _open_path(call: ast.Call) -> str | None:
    """Path of a read-mode ``open``/``io.open``/``codecs.open`` call."""
    func = call.func
    if isinstance(func, ast.Name):
        if func.id != "open":
            return None
    elif not (
        isinstance(func, ast.Attribute)
        and func.attr == "open"
        and isinstance(func.value, ast.Name)
        and func.value.id in _PY_MODULE_OPENS
    ):
        return None
    if not _is_read_mode(_call_arg(call, 1, "mode")):
        return None
    return _str_value(_call_arg(call, 0, "file"))


def _pathlib_read_path(call: ast.Call) -> str | None:
    """Path of ``Path('...').read_text()`` / ``.read_bytes()`` / read-mode ``.open()``."""
    func = call.func
    if not (
        isinstance(func, ast.Attribute)
        and func.attr in {"read_text", "read_bytes", "open"}
        and isinstance(func.value, ast.Call)
    ):
        return None
    ctor = func.value.func
    ctor_name = ctor.id if isinstance(ctor, ast.Name) else getattr(ctor, "attr", None)
    if ctor_name not in _PY_PATH_CTORS:
        return None
    if func.attr == "open" and not _is_read_mode(_call_arg(call, 0, "mode")):
        return None
    return _str_value(func.value.args[0] if func.value.args else None)


def _python_paths(code: str) -> list[str]:
    """Literal paths read via ``open(...)`` or ``Path(...).read_text()``."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return []
    paths: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            path = _open_path(node) or _pathlib_read_path(node)
            if path:
                paths.append(path)
    return paths


def _tool_result_failed(result: object) -> bool:
    """True when the Bash/ExecuteCode call errored (its content starts ``ERROR``).

    Both tools report a non-zero exit, a refused path and a crash that way.
    """
    if getattr(result, "status", None) == "error":
        return True
    content = getattr(result, "content", "")
    if isinstance(content, list):
        content = next(
            (
                b if isinstance(b, str) else b.get("text", "")
                for b in content
                if isinstance(b, str) or (isinstance(b, dict) and b.get("type") == "text")
            ),
            "",
        )
    return isinstance(content, str) and content.lstrip().startswith("ERROR")


def match_skills_from_shell_read(
    tool_name: str | None,
    tool_args: Mapping[str, object] | None,
    skill_md_to_name: Mapping[str, str],
    result: object = None,
) -> list[str]:
    """Skill names whose SKILL.md this Bash/ExecuteCode call clearly read.

    ``result`` is the call's ToolMessage; a failed call loads nothing.
    """
    if not skill_md_to_name or not tool_args:
        return []
    if tool_name == BASH_TOOL_NAME:
        source = tool_args.get("command")
        extract = _bash_paths
    elif tool_name == EXECUTE_CODE_TOOL_NAME:
        source = tool_args.get("code")
        extract = _python_paths
    else:
        return []
    if not isinstance(source, str) or "SKILL.md" not in source:
        return []
    if result is not None and _tool_result_failed(result):
        return []

    matched: list[str] = []
    for path in extract(source):
        skill_name = _match_path(path, skill_md_to_name)
        if skill_name and skill_name not in matched:
            matched.append(skill_name)
    return matched
