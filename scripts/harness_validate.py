#!/usr/bin/env python3
"""Validate Software Delivery Harness schemas and static cross-file invariants."""

import argparse
import json
import sys

from _harness_core import exit_code_for_status, validate_repository


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="repository root (default: current directory)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--include-report", action="store_true", help="also validate .harness/release-report.json when present")
    args = parser.parse_args(argv)
    result = validate_repository(args.repo, include_report=args.include_report)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return exit_code_for_status(result.get("overall_status"))


if __name__ == "__main__":
    sys.exit(main())
