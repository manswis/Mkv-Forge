#!/usr/bin/env python3
"""Zero-dependency Python line coverage runner powered by `sys.monitoring` (PEP 669)."""

from __future__ import annotations

import argparse
import dis
import sys
from dataclasses import dataclass
from pathlib import Path
from types import CodeType

DEFAULT_SOURCE_DIR: str = "app"
DEFAULT_TEST_TARGET: str = "tests"
DEFAULT_FAIL_UNDER: float = 0.0
MIN_LINE_NO: int = 1
MAX_MISSING_RANGES_PER_FILE: int = 10
COL_PATH_WIDTH: int = 44
SEPARATOR_WIDTH: int = 72
EXIT_OK: int = 0
EXIT_FAIL_UNDER: int = 2
MONITORING_TOOL_NAME: str = "mkvforge_coverage"


@dataclass(frozen=True, slots=True)
class FileCoverageResult:
    """Computed line coverage statistics for a single Python source file."""

    rel_path: str
    total_lines: int
    covered_lines: int
    missing_lines: tuple[int, ...]

    @property
    def percent(self) -> float:
        if self.total_lines <= 0:
            return 100.0
        return (100.0 * self.covered_lines) / self.total_lines


def collect_executable_lines(code: CodeType) -> set[int]:
    """Recursively collect all bytecode-executable line numbers in `code`."""
    lines: set[int] = set()
    stack: list[CodeType] = [code]
    while stack:
        current = stack.pop()
        for _, lineno in dis.findlinestarts(current):
            if lineno is not None and lineno >= MIN_LINE_NO:
                lines.add(lineno)
        for const in current.co_consts:
            if isinstance(const, CodeType):
                stack.append(const)
    return lines


def format_line_ranges(
    line_numbers: tuple[int, ...] | list[int],
    *,
    max_ranges: int = MAX_MISSING_RANGES_PER_FILE,
) -> str:
    """Compact sorted line numbers into ranges like `'12-15, 22, 40-44'`."""
    ordered = sorted({int(n) for n in line_numbers if int(n) >= MIN_LINE_NO})
    if not ordered:
        return ""

    ranges: list[str] = []
    start = ordered[0]
    end = ordered[0]
    for num in ordered[1:]:
        if num == end + 1:
            end = num
        else:
            ranges.append(f"{start}" if start == end else f"{start}-{end}")
            start = num
            end = num
    ranges.append(f"{start}" if start == end else f"{start}-{end}")

    if max_ranges > 0 and len(ranges) > max_ranges:
        visible = ", ".join(ranges[:max_ranges])
        return f"{visible}, +{len(ranges) - max_ranges} more"
    return ", ".join(ranges)


def compute_coverage_results(
    source_root: Path,
    project_root: Path,
    executed_by_file: dict[str, set[int]],
) -> list[FileCoverageResult]:
    """Compile every `.py` file under `source_root` and intersect with executed lines."""
    if not source_root.is_dir():
        return []

    results: list[FileCoverageResult] = []
    resolved_project = project_root.resolve()
    for py_file in sorted(source_root.resolve().rglob("*.py")):
        try:
            src = py_file.read_text(encoding="utf-8")
            compiled = compile(src, str(py_file), "exec")
        except (OSError, SyntaxError):
            continue

        all_lines = collect_executable_lines(compiled)
        hit_lines = executed_by_file.get(str(py_file), set()) & all_lines
        missing = tuple(sorted(all_lines - hit_lines))
        try:
            rel = str(py_file.relative_to(resolved_project))
        except ValueError:
            rel = py_file.name

        results.append(
            FileCoverageResult(
                rel_path=rel,
                total_lines=len(all_lines),
                covered_lines=len(hit_lines),
                missing_lines=missing,
            )
        )
    return results


def render_coverage_report(
    results: list[FileCoverageResult],
    *,
    show_missing: bool = False,
) -> tuple[str, float]:
    """Format a human-readable per-file & total coverage report."""
    total_lines = sum(r.total_lines for r in results)
    covered_lines = sum(r.covered_lines for r in results)
    overall_pct = (100.0 * covered_lines / total_lines) if total_lines > 0 else 100.0

    header = f"{'Module':<{COL_PATH_WIDTH}} {'Stmts':>6} {'Miss':>6} {'Cover':>7}"
    if show_missing:
        header += "  Missing"
    lines: list[str] = ["", header, "-" * SEPARATOR_WIDTH]

    for item in results:
        missed_count = item.total_lines - item.covered_lines
        row = (
            f"{item.rel_path:<{COL_PATH_WIDTH}} "
            f"{item.total_lines:>6d} {missed_count:>6d} {item.percent:>6.1f}%"
        )
        if show_missing and item.missing_lines:
            row += f"  {format_line_ranges(item.missing_lines)}"
        lines.append(row)

    lines.append("-" * SEPARATOR_WIDTH)
    total_missed = total_lines - covered_lines
    lines.append(
        f"{'TOTAL':<{COL_PATH_WIDTH}} "
        f"{total_lines:>6d} {total_missed:>6d} {overall_pct:>6.1f}%"
    )
    return "\n".join(lines), overall_pct


def run_pytest_with_coverage(
    *,
    project_root: Path,
    source_dir: str = DEFAULT_SOURCE_DIR,
    pytest_args: list[str] | None = None,
    show_missing: bool = False,
    fail_under: float = DEFAULT_FAIL_UNDER,
) -> tuple[int, float, str]:
    """Execute pytest under `sys.monitoring` LINE tracking and return `(exit_code, pct, report)`."""
    import pytest  # Imported lazily so pure helpers stay testable without pytest runtime

    resolved_root = project_root.resolve()
    resolved_src = (resolved_root / source_dir).resolve()
    src_prefix = str(resolved_src)
    executed: dict[str, set[int]] = {}

    tool_id = sys.monitoring.COVERAGE_ID
    sys.monitoring.use_tool_id(tool_id, MONITORING_TOOL_NAME)

    def _on_line(code: CodeType, line_no: int) -> object:
        filename = code.co_filename
        if filename.startswith(src_prefix):
            bucket = executed.get(filename)
            if bucket is None:
                bucket = set()
                executed[filename] = bucket
            bucket.add(line_no)
        return sys.monitoring.DISABLE

    try:
        sys.monitoring.register_callback(tool_id, sys.monitoring.events.LINE, _on_line)
        sys.monitoring.set_events(tool_id, sys.monitoring.events.LINE)
        args = list(pytest_args) if pytest_args else [DEFAULT_TEST_TARGET, "-q"]
        pytest_exit = int(pytest.main(args))
    finally:
        sys.monitoring.set_events(tool_id, 0)
        sys.monitoring.register_callback(tool_id, sys.monitoring.events.LINE, None)
        sys.monitoring.free_tool_id(tool_id)

    results = compute_coverage_results(resolved_src, resolved_root, executed)
    report_text, overall_pct = render_coverage_report(
        results, show_missing=show_missing
    )

    if pytest_exit != EXIT_OK:
        return pytest_exit, overall_pct, report_text
    if fail_under > 0.0 and overall_pct < fail_under:
        return EXIT_FAIL_UNDER, overall_pct, report_text
    return EXIT_OK, overall_pct, report_text


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run pytest with zero-dependency Python line coverage reporting."
    )
    parser.add_argument(
        "--source",
        default=DEFAULT_SOURCE_DIR,
        help="Source directory relative to repo root to measure (default: app).",
    )
    parser.add_argument(
        "--fail-under",
        type=float,
        default=DEFAULT_FAIL_UNDER,
        help="Minimum required total coverage percentage (default: 0).",
    )
    parser.add_argument(
        "--show-missing",
        action="store_true",
        help="Include compacted uncovered line ranges per file.",
    )
    known, unknown = parser.parse_known_args(argv)
    known.pytest_args = unknown
    return known


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.fail_under < 0.0 or args.fail_under > 100.0:
        print("Error: --fail-under must be between 0 and 100.", file=sys.stderr)
        return EXIT_FAIL_UNDER

    project_root = Path(__file__).resolve().parent.parent
    pytest_args = args.pytest_args if args.pytest_args else [DEFAULT_TEST_TARGET, "-q"]
    exit_code, overall_pct, report_text = run_pytest_with_coverage(
        project_root=project_root,
        source_dir=args.source,
        pytest_args=pytest_args,
        show_missing=args.show_missing,
        fail_under=args.fail_under,
    )
    print(report_text)
    if exit_code == EXIT_FAIL_UNDER and args.fail_under > 0.0:
        print(
            f"\nError: Total coverage {overall_pct:.1f}% is below --fail-under {args.fail_under:.1f}%.",
            file=sys.stderr,
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
