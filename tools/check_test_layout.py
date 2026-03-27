"""Enforce a one-to-one mapping between source modules and test modules."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    src_dir = repo_root / "src" / "miniject"
    tests_dir = repo_root / "tests"

    source_modules = {
        source.name: f"test_{source.stem.removeprefix('_')}.py"
        for source in sorted(src_dir.glob("*.py"))
        if source.name != "__init__.py"
    }
    expected_tests = set(source_modules.values())
    actual_tests = {test.name for test in sorted(tests_dir.glob("test_*.py"))}

    missing_tests = [
        f"{source_name} -> {test_name}"
        for source_name, test_name in source_modules.items()
        if test_name not in actual_tests
    ]
    unexpected_tests = sorted(actual_tests - expected_tests)

    if not missing_tests and not unexpected_tests:
        sys.stdout.write("Test layout matches source layout.\n")
        return 0

    if missing_tests:
        sys.stderr.write("Missing test modules for source modules:\n")
        for mapping in missing_tests:
            sys.stderr.write(f"  - {mapping}\n")

    if unexpected_tests:
        sys.stderr.write("Unexpected test modules without matching source modules:\n")
        for test_name in unexpected_tests:
            sys.stderr.write(f"  - {test_name}\n")

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
