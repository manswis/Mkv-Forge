"""Unit tests for scripts/run_coverage.py and scripts/release.sh --cov CLI support."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUN_COV_PATH = PROJECT_ROOT / "scripts" / "run_coverage.py"
RELEASE_SH_PATH = PROJECT_ROOT / "scripts" / "release.sh"


def _load_run_coverage_module():
    spec = importlib.util.spec_from_file_location("run_coverage", RUN_COV_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules["run_coverage"] = module
    spec.loader.exec_module(module)
    return module


class TestRunCoverageHelpers:
    """Exhaustive positive and negative/edge tests for coverage calculation & formatting."""

    def test_format_line_ranges_compacts_contiguous_and_truncates_long_lists(self) -> None:
        mod = _load_run_coverage_module()
        assert mod.format_line_ranges([]) == ""
        assert mod.format_line_ranges([0, -5]) == ""
        assert mod.format_line_ranges([5]) == "5"
        assert mod.format_line_ranges([10, 2, 3, 4, 2, 8, 9]) == "2-4, 8-10"
        assert mod.format_line_ranges([1, 3, 5, 7], max_ranges=2) == "1, 3, +2 more"

    def test_compute_and_render_coverage_positive_partial_empty_and_syntax_error(
        self, tmp_path: Path
    ) -> None:
        mod = _load_run_coverage_module()
        src_dir = tmp_path / "app"
        src_dir.mkdir()

        full_file = src_dir / "full.py"
        full_file.write_text("x = 1\ny = 2\n", encoding="utf-8")

        partial_file = src_dir / "partial.py"
        partial_file.write_text(
            "def f(flag):\n    if flag:\n        return 1\n    return 2\n",
            encoding="utf-8",
        )

        broken_file = src_dir / "broken.py"
        broken_file.write_text("def broken(:\n", encoding="utf-8")

        executed = {
            str(full_file.resolve()): {1, 2},
            str(partial_file.resolve()): {1, 2, 3},
        }
        results = mod.compute_coverage_results(src_dir, tmp_path, executed)
        by_path = {r.rel_path: r for r in results}

        assert "app/broken.py" not in by_path
        assert by_path["app/full.py"].percent == 100.0
        assert by_path["app/full.py"].missing_lines == ()
        assert by_path["app/partial.py"].covered_lines == 3
        assert by_path["app/partial.py"].total_lines == 4
        assert by_path["app/partial.py"].missing_lines == (4,)

        report, total_pct = mod.render_coverage_report(results, show_missing=True)
        assert "app/full.py" in report
        assert "app/partial.py" in report
        assert "TOTAL" in report
        assert "4" in report
        assert round(total_pct, 1) == round((5 / 6) * 100.0, 1)

        # Non-existent source dir fails safely with empty list & 100% empty report
        empty_results = mod.compute_coverage_results(tmp_path / "missing", tmp_path, {})
        assert empty_results == []
        _, empty_pct = mod.render_coverage_report(empty_results)
        assert empty_pct == 100.0

    def test_cli_rejects_out_of_range_fail_under_and_forwards_pytest_flags(self) -> None:
        mod = _load_run_coverage_module()
        assert mod.main(["--fail-under", "105"]) == mod.EXIT_FAIL_UNDER
        assert mod.main(["--fail-under", "-1"]) == mod.EXIT_FAIL_UNDER
        parsed = mod.parse_args(["--show-missing", "tests/unit", "-q", "-k", "batch"])
        assert parsed.show_missing is True
        assert parsed.pytest_args == ["tests/unit", "-q", "-k", "batch"]


class TestReleaseScriptCovFlag:
    """Verify scripts/release.sh --help, syntax, and --cov standalone wiring."""

    def test_release_sh_syntax_and_help_document_cov_flag(self) -> None:
        syntax = subprocess.run(
            ["bash", "-n", str(RELEASE_SH_PATH)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert syntax.returncode == 0

        help_proc = subprocess.run(
            ["bash", str(RELEASE_SH_PATH), "--help"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "--cov" in help_proc.stdout
        assert "./scripts/release.sh --cov" in help_proc.stdout

        script_text = RELEASE_SH_PATH.read_text(encoding="utf-8")
        assert "scripts/run_coverage.py" in script_text
        assert "RUN_COV=1" in script_text
