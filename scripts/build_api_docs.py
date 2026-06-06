#!/usr/bin/env python3
"""
Build the Sphinx HTML API documentation for Durex.

The script is intentionally small and dependency-light. It gives maintainers one
stable command to run after changing Python docstrings:

    python3 scripts/build_api_docs.py

By default the build runs in warning-as-error mode so malformed docstrings,
broken imports, or invalid references fail early.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SPHINX_SOURCE = REPO_ROOT / "docs" / "sphinx"
SPHINX_BUILD = REPO_ROOT / "docs" / "_build" / "html"


def parse_args(argv: list[str]) -> argparse.Namespace:
    """
    Parse command-line options for the API documentation build.

    Args:
        argv:
            Command-line arguments excluding the executable name.

    Returns:
        Parsed argparse namespace.
    """

    parser = argparse.ArgumentParser(
        description="Build the Durex Sphinx HTML API documentation."
    )
    parser.add_argument(
        "--no-strict",
        action="store_true",
        help="Do not treat Sphinx warnings as errors.",
    )
    parser.add_argument(
        "--fresh-env",
        action="store_true",
        help="Do not reuse the saved Sphinx environment between builds.",
    )
    return parser.parse_args(argv)


def build_docs(strict: bool = True, fresh_env: bool = False) -> int:
    """
    Build the HTML API documentation with Sphinx.

    Args:
        strict:
            Treat Sphinx warnings as build failures when true.
        fresh_env:
            Re-read every source file instead of reusing Sphinx's environment
            cache. This is useful after larger docstring restructures.

    Returns:
        Sphinx process-style exit code.

    Raises:
        RuntimeError:
            Raised when Sphinx is not installed in the current Python
            environment.
    """

    try:
        from sphinx.cmd.build import build_main
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Sphinx is not installed. Install development dependencies with "
            "`python3 -m pip install -r requirements-dev.txt`."
        ) from exc

    args = ["-b", "html"]
    if strict:
        args.append("-W")
    if fresh_env:
        args.append("-E")
    args.extend([str(SPHINX_SOURCE), str(SPHINX_BUILD)])

    return int(build_main(args))


def main(argv: list[str] | None = None) -> int:
    """
    Run the command-line entry point.

    Args:
        argv:
            Optional argument list for tests or programmatic callers.

    Returns:
        Process exit code.
    """

    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return build_docs(strict=not args.no_strict, fresh_env=args.fresh_env)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
