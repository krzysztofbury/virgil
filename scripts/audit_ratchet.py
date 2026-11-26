#!/usr/bin/env python3
"""Ratchet for code comments and tests marked for deletion (Python 3.10+).

Counts, per tracked file under the configured roots:

- comments: comment lines, except tool pragmas, license headers, script
  metadata (PEP 723) and one-line `Why: ...` comments. Python is read with
  the standard tokenizer; the other languages a baseline enables are read
  with Pygments, so their comments inside strings are never counted;
- to_delete: `pytest.mark.to_delete(issue=N)` markers in Python files.

Files `.gitattributes` marks `linguist-vendored` or `linguist-generated` are
not counted. Both counts may only fall. The baseline is a JSON file committed
to the repository; a file missing from it has a baseline of zero.

  audit_ratchet.py init --roots src tests [--languages py sql js html]
                                write the first baseline (default: py)
  audit_ratchet.py extend --languages js html
                                count more languages from now on; records
                                their current counts and leaves the rest
  audit_ratchet.py check        fail when any count rose, or fell without the
                                baseline lowered
  audit_ratchet.py update       lower the baseline to the current counts;
                                refuses a rise
  audit_ratchet.py report [--explain PATH]
                                totals and the largest files, or each counted
                                comment line of one file

Options: --baseline PATH (default .agents/audit-baseline.json).
Copy this file into the project (scripts/audit_ratchet.py) and run `check`
in CI or as a pre-commit hook. Python needs only the standard library; any
other language needs Pygments in the environment that runs the check
(pytest already depends on it), pinned by the project's lock file so the
counts stay the same on every machine.
"""

import argparse
import ast
import importlib
import io
import json
import re
import subprocess
import sys
import tokenize
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

VERSION = 2
DEFAULT_BASELINE = ".agents/audit-baseline.json"
KINDS = ("comments", "to_delete")
Counts = dict[str, dict[str, int]]

LANGUAGES: dict[str, tuple[tuple[str, ...], str]] = {
    "py": ((".py",), ""),
    "sql": ((".sql",), "sql"),
    "js": ((".js", ".mjs", ".cjs", ".jsx"), "javascript"),
    "ts": ((".ts", ".tsx", ".mts", ".cts"), "typescript"),
    "css": ((".css", ".scss"), "css"),
    "html": ((".html", ".htm", ".jinja", ".j2"), "html+jinja"),
    "sh": ((".sh", ".bash"), "bash"),
    "yaml": ((".yml", ".yaml"), "yaml"),
    "toml": ((".toml",), "toml"),
}

PRAGMAS = (
    r"noqa\b",
    r"type:\s*ignore",
    "pragma:",
    "pyright:",
    "mypy:",
    "fmt:",
    "isort:",
    "ruff:",
    r"nosec\b",
    "pylint:",
    "eslint-disable",
    "eslint-enable",
    r"eslint\s+[\w/@-]+\s*:",
    "@ts-",
    "prettier-ignore",
    "stylelint-",
    r"istanbul\s+ignore",
    r"c8\s+ignore",
    r"v8\s+ignore",
    "biome-ignore",
    r"shellcheck\s",
    "sqlfluff:",
    "yaml-language-server:",
    "taplo:",
    r"@license\b",
    "SPDX-License-Identifier:",
    "SPDX-FileCopyrightText:",
)
PRAGMA = re.compile("^(" + "|".join(PRAGMAS) + ")", re.IGNORECASE)
CODING = re.compile(r"^#.*coding[:=]")
WHY = re.compile(r"^Why:\s*\S")
SCRIPT_BLOCK = re.compile(r"^# /// [a-z][a-z0-9-]*$")
DELIMITERS = re.compile(r"^(?:#+|//+|--+|/\*+|<!--|\{#|\*+)\s*|\s*(?:\*+/|-->|#\})$")
# Why: these Pygments comment tokens are directives (a shebang, a preprocessor line).
SKIPPED_TOKENS = ("Token.Comment.Hashbang", "Token.Comment.Preproc")


class RatchetError(Exception):
    pass


class Lexer(Protocol):
    def get_tokens(self, text: str) -> Iterable[tuple[object, str]]: ...


@dataclass
class Baseline:
    roots: list[str]
    languages: list[str]
    counts: Counts


class Args(argparse.Namespace):
    command: str = ""
    baseline: str = DEFAULT_BASELINE
    roots: list[str] | None = None
    languages: list[str] | None = None
    explain: str | None = None


def body(text: str) -> str:
    """A comment's text without its delimiters."""
    previous = None
    text = text.strip()
    while text != previous:
        previous = text
        text = DELIMITERS.sub("", text).strip()
    return text


def python_comments(source: str) -> Iterator[tuple[int, str]]:
    """(line, text) of each counted Python comment."""
    in_script_block = False
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    for tok in tokens:
        if tok.type != tokenize.COMMENT:
            continue
        text = tok.string
        line = tok.start[0]
        if in_script_block:
            in_script_block = text.rstrip() != "# ///"
            continue
        if tok.start[1] == 0 and SCRIPT_BLOCK.match(text.rstrip()):
            in_script_block = True
            continue
        if line == 1 and text.startswith("#!"):
            continue
        if line <= 2 and CODING.match(text):
            continue
        inner = body(text)
        if PRAGMA.match(inner) or WHY.match(inner):
            continue
        yield line, text


def lexer_for(name: str) -> Lexer:
    try:
        module = importlib.import_module("pygments.lexers")
    except ImportError:
        message = "Pygments is needed to count comments outside Python; "
        raise RatchetError(message + "add it to the dev dependencies") from None
    factory = cast(Callable[..., Lexer], module.get_lexer_by_name)
    return factory(name, stripnl=False, ensurenl=False)


def is_comment(token: object) -> bool:
    kind = str(token)
    return kind.startswith("Token.Comment") and not kind.startswith(SKIPPED_TOKENS)


def lexed_comments(source: str, lexer_name: str) -> Iterator[tuple[int, str]]:
    """(line, text) of each counted comment line, read with Pygments."""
    found: dict[int, list[str]] = {}
    run: list[str] = []
    run_start = line = 1

    def flush() -> None:
        # Why: lexers split one comment into several tokens (`/*`, text, `*/`).
        for offset, piece in enumerate("".join(run).split("\n")):
            inner = body(piece)
            if inner and not (PRAGMA.match(inner) or WHY.match(inner)):
                found.setdefault(run_start + offset, []).append(piece.strip())

    for token, value in lexer_for(lexer_name).get_tokens(source):
        if is_comment(token):
            if not run:
                run_start = line
            run.append(value)
        elif run:
            flush()
            run = []
        line += value.count("\n")
    if run:
        flush()
    for number in sorted(found):
        yield number, " ".join(found[number])


def comments(path: str, source: str) -> Iterator[tuple[int, str]]:
    suffix = Path(path).suffix.lower()
    for extensions, lexer_name in LANGUAGES.values():
        if suffix in extensions:
            if not lexer_name:
                return python_comments(source)
            return lexed_comments(source, lexer_name)
    raise RatchetError(f"{path}: no comment rules for {suffix or 'files without a suffix'}")


def count_comments(source: str, path: str = "x.py") -> int:
    return sum(1 for _ in comments(path, source))


def dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def count_to_delete(source: str, path: str) -> int:
    tree = ast.parse(source, filename=path)
    total = 0
    calls: set[int] = set()
    missing = f"{path}:{{}}: to_delete needs issue=<issue number>"
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and dotted(node.func).endswith("mark.to_delete"):
            calls.add(id(node.func))
            issue = next((k.value for k in node.keywords if k.arg == "issue"), None)
            if not (
                isinstance(issue, ast.Constant)
                and isinstance(issue.value, int)
                and not isinstance(issue.value, bool)
                and issue.value > 0
            ):
                raise RatchetError(missing.format(node.lineno))
            total += 1
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "to_delete"
            and dotted(node).endswith("mark.to_delete")
            and id(node) not in calls
        ):
            raise RatchetError(missing.format(node.lineno))
    return total


def extensions(languages: list[str]) -> tuple[str, ...]:
    unknown = sorted(set(languages) - set(LANGUAGES))
    if unknown:
        raise RatchetError(f"unknown languages {unknown}; known: {sorted(LANGUAGES)}")
    return tuple(ext for name in languages for ext in LANGUAGES[name][0])


def tracked_files(roots: list[str], languages: list[str]) -> list[str]:
    wanted = extensions(languages)
    try:
        out = subprocess.run(["git", "ls-files", "-z", "--", *roots], capture_output=True, check=True).stdout.decode()
        files = [f for f in out.split("\0") if f.lower().endswith(wanted)]
    except OSError, subprocess.CalledProcessError:
        files = [str(p) for r in roots for p in Path(r).rglob("*") if str(p).lower().endswith(wanted)]
    files = [f for f in files if Path(f).is_file()]
    return sorted(set(files) - vendored(files))


def vendored(files: list[str]) -> set[str]:
    """Files `.gitattributes` marks `linguist-vendored` or `linguist-generated`."""
    if not files:
        return set()
    attributes = ["linguist-vendored", "linguist-generated"]
    try:
        out = subprocess.run(
            ["git", "check-attr", "-z", "--stdin", *attributes],
            input="\0".join(files).encode(),
            capture_output=True,
            check=True,
        ).stdout.decode()
    except OSError, subprocess.CalledProcessError:
        return set()
    fields = out.split("\0")
    return {fields[i] for i in range(0, len(fields) - 2, 3) if fields[i + 2] not in ("unspecified", "unset", "false")}


def measure(roots: list[str], languages: list[str]) -> Counts:
    counts: Counts = {kind: {} for kind in KINDS}
    for path in tracked_files(roots, languages):
        source = Path(path).read_text(encoding="utf-8")
        found = {
            "comments": count_comments(source, path),
            "to_delete": count_to_delete(source, path) if path.endswith(".py") else 0,
        }
        for kind in KINDS:
            if found[kind]:
                counts[kind][path] = found[kind]
    return counts


def strings(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    items = cast(list[object], value)
    if all(isinstance(item, str) for item in items):
        return cast(list[str], items)
    return None


def counts_of(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    pairs = cast(dict[object, object], value)
    if all(isinstance(k, str) and type(v) is int for k, v in pairs.items()):
        return cast(dict[str, int], pairs)
    return None


def load(path: str) -> Baseline:
    try:
        data = cast(object, json.loads(Path(path).read_text(encoding="utf-8")))
    except FileNotFoundError:
        raise RatchetError(f"no baseline at {path}; run init first") from None
    unsupported = RatchetError(f"{path}: unsupported baseline (version 1 or {VERSION})")
    if not isinstance(data, dict):
        raise unsupported
    fields = cast(dict[str, object], data)
    roots = strings(fields.get("roots"))
    languages = strings(fields.get("languages", ["py"]))
    if fields.get("version") not in (1, VERSION) or not roots or not languages:
        raise unsupported
    counts: Counts = {}
    for kind in KINDS:
        found = counts_of(fields.get(kind, {}))
        if found is None:
            raise unsupported
        counts[kind] = found
    _ = extensions(languages)
    return Baseline(roots, languages, counts)


def save(path: str, baseline: Baseline) -> None:
    data: dict[str, object] = {
        "version": VERSION,
        "roots": baseline.roots,
        "languages": baseline.languages,
    }
    data.update({kind: dict(sorted(baseline.counts[kind].items())) for kind in KINDS})
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    _ = Path(path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def rises(baseline: Baseline, counts: Counts) -> list[str]:
    out: list[str] = []
    for kind in KINDS:
        old = baseline.counts[kind]
        for path, n in counts[kind].items():
            if n > old.get(path, 0):
                out.append(f"{kind}: {path} has {n}, baseline {old.get(path, 0)}")
    return out


def falls(baseline: Baseline, counts: Counts) -> int:
    return sum(1 for kind in KINDS for path, n in baseline.counts[kind].items() if counts[kind].get(path, 0) < n)


def totals(counts: Counts) -> str:
    return ", ".join(f"{sum(counts[k].values())} {k}" for k in KINDS)


def parse(argv: list[str] | None) -> Args:
    parser = argparse.ArgumentParser(description="Comment and marked-test ratchet")
    commands = ("init", "extend", "check", "update", "report")
    _ = parser.add_argument("command", choices=commands)
    _ = parser.add_argument("--baseline", default=DEFAULT_BASELINE)
    _ = parser.add_argument("--roots", nargs="+")
    _ = parser.add_argument("--languages", nargs="+")
    _ = parser.add_argument("--explain", metavar="PATH")
    return parser.parse_args(argv, namespace=Args())


def run(args: Args) -> int:
    if args.command == "init":
        if Path(args.baseline).exists():
            raise RatchetError(f"{args.baseline} exists; use update or extend")
        if not args.roots:
            raise RatchetError("init needs --roots")
        languages = args.languages or ["py"]
        counts = measure(args.roots, languages)
        save(args.baseline, Baseline(args.roots, languages, counts))
        print(f"wrote {args.baseline}: {totals(counts)}")
        return 0
    baseline = load(args.baseline)
    if args.command == "extend":
        known = baseline.languages
        added = [name for name in args.languages or [] if name not in known]
        if not added:
            raise RatchetError("extend needs --languages not yet in the baseline")
        new = measure(baseline.roots, added)
        for kind in KINDS:
            baseline.counts[kind].update(new[kind])
        baseline.languages = known + added
        save(args.baseline, baseline)
        print(f"added {', '.join(added)} to {args.baseline}: {totals(new)}")
        return 0
    if args.command == "report" and args.explain:
        source = Path(args.explain).read_text(encoding="utf-8")
        for number, text in comments(args.explain, source):
            print(f"{args.explain}:{number}: {text}")
        return 0
    counts = measure(baseline.roots, baseline.languages)
    if args.command == "report":
        for kind in KINDS:
            top = sorted(counts[kind].items(), key=lambda kv: -kv[1])[:10]
            print(f"{kind}: {sum(counts[kind].values())} in {len(counts[kind])} files")
            for path, n in top:
                print(f"  {n:5d}  {path}")
        return 0
    up = rises(baseline, counts)
    if up:
        print("audit ratchet: counts rose above the baseline:", file=sys.stderr)
        for line in up:
            print(f"  {line}", file=sys.stderr)
        hint = "Remove the new comment or marker; `report --explain PATH` lists "
        hint += "what counts. One-line `Why: ...` comments and pragmas do not count."
        print(hint, file=sys.stderr)
        return 1
    if args.command == "update":
        save(args.baseline, Baseline(baseline.roots, baseline.languages, counts))
        print(f"lowered {args.baseline}")
        return 0
    lower = falls(baseline, counts)
    if lower:
        # Why: lock in every fall, so a later change cannot spend the headroom.
        hint = f"audit ratchet: {lower} counts fell below the baseline; run "
        hint += f"`audit_ratchet.py update` and commit {args.baseline}"
        print(hint, file=sys.stderr)
        return 1
    print("audit ratchet: ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parse(argv))
    except (RatchetError, SyntaxError, UnicodeDecodeError) as error:
        print(f"audit ratchet: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
