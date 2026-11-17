#!/usr/bin/env python3
"""Generate or retrieve a machine-derived Software Delivery Harness report."""

import argparse
import json
import sys

from _harness_core import (
    HarnessError,
    atomic_write_json,
    build_fail_closed_report,
    exit_code_for_status,
    find_repo,
    load_latest_report,
    safe_verify,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="repository root (default: current directory)")
    parser.add_argument("--run-id", help="read an existing archived run instead of verifying again")
    parser.add_argument("--strict", action="store_true", help="retained for compatibility; new verification is always strict")
    parser.add_argument("--json", action="store_true", help="emit compact machine-readable JSON")
    parser.add_argument("--output", help="optional output path; defaults to .harness/release-report.json")
    args = parser.parse_args(argv)
    repository_error = None
    try:
        repo = find_repo(args.repo)
    except BaseException as exc:
        repository_error = exc
        repo = args.repo
    if repository_error is not None:
        report = build_fail_closed_report(
            args.repo,
            "REPOSITORY_LAYOUT_UNSAFE",
            "repository could not be safely resolved: %s: %s" % (type(repository_error).__name__, repository_error),
        )
    elif args.run_id:
        try:
            report, _ = load_latest_report(repo, args.run_id)
        except BaseException as exc:
            error_name = "KeyboardInterrupt" if isinstance(exc, KeyboardInterrupt) else type(exc).__name__
            report = build_fail_closed_report(
                repo,
                "ARCHIVED_REPORT_INVALID",
                "archived run %s was rejected: %s: %s" % (args.run_id, error_name, exc),
            )
    else:
        result = safe_verify(repo, strict=True)
        report = result["report"]
    output_path = args.output or (str(repo / ".harness" / "release-report.json") if repository_error is None else None)
    if output_path is not None:
        atomic_write_json(output_path, report)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return exit_code_for_status(report.get("overall_status"))


if __name__ == "__main__":
    sys.exit(main())
