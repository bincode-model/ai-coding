#!/usr/bin/env python3
"""Run fail-closed Software Delivery Harness verification and archive the run."""

import argparse
import json
import os
import subprocess
import sys

from _harness_core import (
    HarnessError,
    _verify_repository_impl,
    collect_runtime,
    exit_code_for_status,
    hook_response,
    issue,
    read_hook_input,
    safe_verify,
)


def _print_result(result, machine_json):
    if machine_json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="repository root (default: current directory)")
    parser.add_argument("--strict", action="store_true", help="gate unresolved placeholder review findings")
    parser.add_argument("--json", action="store_true", help="emit exactly one compact JSON document")
    parser.add_argument("--run-id", help="explicit unique run ID (primarily for CI orchestration)")
    parser.add_argument(
        "--collect", action="store_true",
        help="run the trusted runtime collector before strict verification",
    )
    parser.add_argument(
        "--expected-hook", choices=("PreToolUse", "Stop", "SubagentStart", "SubagentStop"),
        help="configured Codex Hook event; preserves a valid protocol response on malformed stdin",
    )
    args = parser.parse_args(argv)

    try:
        hook_input = read_hook_input(expected_event=args.expected_hook)
    except HarnessError as exc:
        failed = {
            "standard_version": "1.0.0",
            "overall_status": "FAIL",
            "release_allowed": False,
            "issues": [{"code": "HOOK_INPUT_INVALID", "message": str(exc), "status": "FAIL"}],
        }
        _print_result(failed, True)
        return 1

    repo = args.repo
    if hook_input is not None:
        cwd = hook_input.get("cwd")
        if isinstance(cwd, str) and cwd:
            repo = cwd
    collection_issues = []
    collection_result = None
    if args.collect:
        if hook_input is not None:
            collection_issues.append(issue(
                "COLLECTOR_HOOK_MODE_FORBIDDEN",
                "runtime collection cannot be combined with a Codex Hook invocation",
            ))
        else:
            try:
                collection_result = collect_runtime(repo)
            except (HarnessError, OSError, subprocess.SubprocessError) as exc:
                collection_issues.append(issue(
                    "COLLECTOR_BLOCKED",
                    "%s: %s" % (type(exc).__name__, exc),
                    status="BLOCKED",
                ))
            except BaseException as exc:
                collection_issues.append(issue(
                    "COLLECTOR_INTERNAL_ERROR",
                    "%s: %s" % (type(exc).__name__, exc),
                ))

    verifier = None
    if args.collect:
        def verifier(repository, strict=False, run_id=None):
            return _verify_repository_impl(
                repository,
                strict=strict,
                run_id=run_id,
                initial_issues=collection_issues,
                collector_result=collection_result,
            )

    result = safe_verify(
        repo,
        strict=args.strict or hook_input is not None,
        run_id=args.run_id,
        verifier=verifier,
    )
    if hook_input is not None:
        response, hook_exit = hook_response(hook_input, result)
        print(json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return hook_exit
    _print_result(result, args.json)
    return exit_code_for_status(result.get("overall_status"))


if __name__ == "__main__":
    sys.exit(main())
