"""Progressive Python complexity guardrail for HeySure Server.

The baseline records existing debt. A normal run fails only when a violation is
new or an existing metric grows, so teams can refactor large modules in small,
behaviour-preserving changes. Run with ``--write-baseline`` only after reviewing
the generated report; baseline entries may be removed but must not be enlarged.
"""

from __future__ import annotations

import argparse
import ast
import json
import tokenize
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


SERVER_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = Path(__file__).with_name("guardrail_baseline.json")


@dataclass(frozen=True)
class Limits:
    file_lines: int
    function_lines: int
    complexity: int
    parameters: int = 8
    nesting: int = 4
    dependencies: int = 15


PRODUCTION_LIMITS = Limits(500, 80, 15)
TEST_LIMITS = Limits(800, 120, 20)


@dataclass(frozen=True)
class Violation:
    key: str
    metric: str
    value: int
    limit: int
    path: str
    symbol: str = ""


def _relative(path: Path) -> str:
    return path.relative_to(SERVER_ROOT).as_posix()


def _code_lines(path: Path) -> set[int]:
    """Return physical lines containing Python tokens other than comments."""
    lines: set[int] = set()
    with tokenize.open(path) as source:
        tokens = tokenize.generate_tokens(source.readline)
        for token in tokens:
            if token.type in {
                tokenize.ENCODING,
                tokenize.ENDMARKER,
                tokenize.INDENT,
                tokenize.DEDENT,
                tokenize.NEWLINE,
                tokenize.NL,
                tokenize.COMMENT,
            }:
                continue
            lines.update(range(token.start[0], token.end[0] + 1))
    return lines


def _complexity(node: ast.AST) -> int:
    score = 1
    branch_nodes = (
        ast.If,
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.IfExp,
        ast.ExceptHandler,
        ast.comprehension,
        ast.Assert,
    )
    for child in ast.walk(node):
        if isinstance(child, branch_nodes):
            score += 1
        elif isinstance(child, ast.BoolOp):
            score += max(1, len(child.values) - 1)
        elif hasattr(ast, "Match") and isinstance(child, ast.Match):
            score += max(0, len(child.cases) - 1)
    return score


def _parameter_count(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    args = node.args
    return len(args.posonlyargs) + len(args.args) + len(args.kwonlyargs) + bool(args.vararg) + bool(args.kwarg)


def _nesting(node: ast.AST) -> int:
    nesting_nodes = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith)
    if hasattr(ast, "Match"):
        nesting_nodes += (ast.Match,)

    def visit(current: ast.AST, depth: int) -> int:
        next_depth = depth + 1 if isinstance(current, nesting_nodes) else depth
        children = [visit(child, next_depth) for child in ast.iter_child_nodes(current)]
        return max([next_depth, *children])

    return visit(node, 0)


def _dependencies(tree: ast.Module) -> int:
    modules: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            modules.add(prefix + (node.module or ""))
    return len(modules)


def _function_name(node: ast.AST, parents: list[str]) -> str:
    name = getattr(node, "name", "<anonymous>")
    return ".".join([*parents, name])


def _functions(tree: ast.AST) -> Iterable[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]]:
    def visit(node: ast.AST, parents: list[str]):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                next_parents = [*parents, child.name] if isinstance(child, ast.ClassDef) else parents
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield child, _function_name(child, parents)
                    next_parents = [*parents, child.name]
                yield from visit(child, next_parents)
            else:
                yield from visit(child, parents)

    yield from visit(tree, [])


def _violation(metric: str, value: int, limit: int, path: str, symbol: str = "") -> Violation:
    suffix = f":{symbol}" if symbol else ""
    return Violation(f"{metric}:{path}{suffix}", metric, value, limit, path, symbol)


def inspect_file(path: Path, limits: Limits) -> list[Violation]:
    relative = _relative(path)
    code_lines = _code_lines(path)
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=relative)
    found: list[Violation] = []
    if len(code_lines) > limits.file_lines:
        found.append(_violation("file_lines", len(code_lines), limits.file_lines, relative))
    dependencies = _dependencies(tree)
    if dependencies > limits.dependencies:
        found.append(_violation("dependencies", dependencies, limits.dependencies, relative))
    for node, name in _functions(tree):
        end = node.end_lineno or node.lineno
        effective = sum(node.lineno <= line <= end for line in code_lines)
        metrics = {
            "function_lines": (effective, limits.function_lines),
            "complexity": (_complexity(node), limits.complexity),
            "parameters": (_parameter_count(node), limits.parameters),
            "nesting": (_nesting(node), limits.nesting),
        }
        for metric, (value, limit) in metrics.items():
            if value > limit:
                found.append(_violation(metric, value, limit, relative, name))
    return found


def collect() -> list[Violation]:
    files = [(path, PRODUCTION_LIMITS) for path in (SERVER_ROOT / "main").rglob("*.py")]
    files += [(path, TEST_LIMITS) for path in (SERVER_ROOT / "other" / "tests").rglob("*.py")]
    violations: list[Violation] = []
    for path, limits in sorted(files, key=lambda item: str(item[0])):
        violations.extend(inspect_file(path, limits))
    return sorted(violations, key=lambda item: item.key)


def _payload(violations: list[Violation]) -> dict:
    counts: dict[str, int] = {}
    for item in violations:
        counts[item.metric] = counts.get(item.metric, 0) + 1
    return {
        "version": 1,
        "limits": {"production": asdict(PRODUCTION_LIMITS), "tests": asdict(TEST_LIMITS)},
        "counts": counts,
        "violations": {item.key: item.value for item in violations},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--json", action="store_true", help="print the current report as JSON")
    args = parser.parse_args()
    current = collect()
    payload = _payload(current)
    if args.write_baseline:
        BASELINE_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {BASELINE_PATH} with {len(current)} violations")
        return 0
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    if not BASELINE_PATH.exists():
        print("guardrail baseline is missing; run with --write-baseline and review the result")
        return 2
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    old = baseline.get("violations", {})
    regressions = [item for item in current if item.key not in old or item.value > int(old[item.key])]
    if regressions:
        print("complexity guardrail regressions:")
        for item in regressions:
            symbol = f"::{item.symbol}" if item.symbol else ""
            print(f"  {item.path}{symbol} {item.metric}={item.value} (limit {item.limit})")
        return 1
    print(f"guardrails passed: {len(current)} grandfathered violations; baseline cannot grow")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
