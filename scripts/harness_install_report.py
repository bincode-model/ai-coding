#!/usr/bin/env python3
"""Generate and verify a current, fail-closed Harness installation report.

The report is not a self-attestation.  PASS is derived from the current Git
commit, the latest strict release report for that commit, two explicitly
selected self-test receipts, live Codex hooks/list state, canonical/project
file parity, specialist definitions, and the checked-in CI gate.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _harness_core import StrictSchemaValidator, strict_json_load  # noqa: E402
from harness_init import codex_hooks_list  # noqa: E402

STATUS_VALUES = ("PASS", "FAIL", "BLOCKED", "NOT_RUN")
HOOK_EVENTS = {
    "preToolUse": "PreToolUse",
    "subagentStart": "SubagentStart",
    "subagentStop": "SubagentStop",
    "stop": "Stop",
}
AGENT_ROLES = (
    "architecture_auditor",
    "backend_worker",
    "database_worker",
    "file_pipeline_worker",
    "frontend_worker",
    "release_judge",
    "requirements_auditor",
    "runtime_acceptance",
    "security_reviewer",
    "test_engineer",
)
MIRRORED_SCRIPTS = (
    "_harness_core.py",
    "harness_authorize_subagent.py",
    "harness_init.py",
    "harness_report.py",
    "harness_run_subagent.py",
    "harness_selftest.py",
    "harness_validate.py",
    "harness_verify.py",
    "harness_install_report.py",
)


class ReportError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise ReportError("missing timezone-aware timestamp")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ReportError("invalid timestamp %r: %s" % (value, exc)) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReportError("timestamp lacks timezone: %s" % value)
    return parsed


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_regular_file(path: Path, label: str) -> Path:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ReportError("%s must not be a symlink: %s" % (label, candidate))
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise ReportError("%s is missing or not a regular file: %s" % (label, resolved))
    return resolved


def artifact(path: Path, label: str = "artifact") -> dict[str, Any]:
    resolved = require_regular_file(path, label)
    return {
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "bytes": resolved.stat().st_size,
    }


def run_git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
    )
    if completed.returncode != 0:
        raise ReportError("git %s failed: %s" % (" ".join(args), completed.stderr.strip()))
    return completed.stdout.strip()


def issue(issue_id: str, status: str, reason: str, *, critical: bool = True) -> dict[str, Any]:
    if status not in STATUS_VALUES:
        raise ReportError("invalid issue status: %s" % status)
    return {"id": issue_id, "status": status, "critical": critical, "reason": reason}


def load_object(path: Path, label: str) -> tuple[Path, dict[str, Any]]:
    resolved = require_regular_file(path, label)
    value = strict_json_load(resolved)
    if not isinstance(value, dict):
        raise ReportError("%s must contain a JSON object: %s" % (label, resolved))
    return resolved, value


def relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def component(status: str, summary: str, paths: Iterable[Path]) -> dict[str, Any]:
    return {
        "status": status,
        "summary": summary,
        "evidence": [artifact(path) for path in paths],
    }


def release_record(repo: Path, report_path: Path, head: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    path, data = load_object(report_path, "release report")
    runs_root = (repo / ".harness" / "runs").resolve()
    if not relative_to(path, runs_root):
        problems.append(issue("release_path_outside_runs", "FAIL", "release report is outside .harness/runs"))
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or path.parent.name != run_id or path.name != "release-report.json":
        problems.append(issue("release_run_path_mismatch", "FAIL", "release run_id and report path do not agree"))
        run_id = str(run_id or "RUN-invalid")
    commit = data.get("git_commit")
    if commit != head:
        problems.append(issue("release_commit_mismatch", "FAIL", "release report is not bound to current Git HEAD"))
    status = data.get("overall_status") if data.get("overall_status") in STATUS_VALUES else "FAIL"
    release_allowed = data.get("release_allowed") is True
    failures = data.get("failures") if isinstance(data.get("failures"), list) else ["malformed failures"]
    blockers = data.get("blockers") if isinstance(data.get("blockers"), list) else ["malformed blockers"]
    not_run = data.get("not_run_items") if isinstance(data.get("not_run_items"), list) else ["malformed not_run_items"]
    completed_at = data.get("completed_at")
    try:
        selected_completed = parse_time(completed_at)
    except ReportError as exc:
        problems.append(issue("release_time_invalid", "FAIL", str(exc)))
        selected_completed = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        completed_at = "1970-01-01T00:00:00Z"

    current_commit_reports: list[tuple[dt.datetime, Path]] = []
    for candidate in runs_root.glob("RUN-*/release-report.json"):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        try:
            observed = strict_json_load(candidate)
            if not isinstance(observed, dict) or observed.get("git_commit") != head:
                continue
            current_commit_reports.append((parse_time(observed.get("completed_at")), candidate.resolve()))
        except (OSError, ValueError, ReportError):
            continue
    if current_commit_reports:
        latest_time, latest_path = max(current_commit_reports, key=lambda row: (row[0], str(row[1])))
        if path != latest_path or selected_completed != latest_time:
            problems.append(
                issue(
                    "release_not_latest_for_commit",
                    "FAIL",
                    "selected release report is not the latest report for current Git HEAD: %s" % latest_path,
                )
            )

    counts = data.get("evidence_counts") if isinstance(data.get("evidence_counts"), dict) else {}
    verified = counts.get("verified") if isinstance(counts.get("verified"), int) else 0
    total = counts.get("total") if isinstance(counts.get("total"), int) else 0
    record_status = "PASS"
    if problems or status == "FAIL" or not release_allowed or failures:
        record_status = "FAIL"
    elif status == "BLOCKED" or blockers:
        record_status = "BLOCKED"
    elif status == "NOT_RUN" or not_run:
        record_status = "NOT_RUN"
    if not (status == "PASS" and release_allowed and not failures and not blockers and not not_run and total > 0 and verified == total):
        problems.append(
            issue(
                "release_not_publishable",
                record_status,
                "strict release must be PASS with release_allowed=true and all evidence verified",
            )
        )
    return {
        "status": record_status,
        "run_id": run_id,
        "report": artifact(path),
        "git_commit": str(commit or "0" * 40),
        "overall_status": status,
        "release_allowed": release_allowed,
        "completed_at": completed_at,
        "failures": failures,
        "blockers": blockers,
        "not_run_items": not_run,
        "verified_evidence": verified,
        "total_evidence": total,
    }, problems


def _selftest_runner_paths(data: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    cases = data.get("cases") if isinstance(data.get("cases"), list) else []
    for case in cases:
        if not isinstance(case, dict):
            continue
        command = case.get("command")
        command = command.get("command") if isinstance(command, dict) else None
        if isinstance(command, list):
            result.update(str(value) for value in command if isinstance(value, str) and value.endswith(".py"))
    return result


def selftest_record(
    path: Path,
    expected_runner: Path,
    commit_time: dt.datetime,
    label: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    resolved, data = load_object(path, "%s self-test" % label)
    run_id = data.get("run_id") if isinstance(data.get("run_id"), str) else "SELFTEST-invalid"
    overall = data.get("overall_status") if data.get("overall_status") in STATUS_VALUES else "FAIL"
    allowed = data.get("release_allowed") is True
    integers: dict[str, int] = {}
    for name in ("total", "passed", "failed", "blocked"):
        value = data.get(name)
        integers[name] = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
    completed_at = data.get("completed_at")
    try:
        completed = parse_time(completed_at)
    except ReportError as exc:
        problems.append(issue("%s_selftest_time_invalid" % label, "FAIL", str(exc)))
        completed = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        completed_at = "1970-01-01T00:00:00Z"
    runner = expected_runner.resolve()
    observed_runners = {str(Path(value).resolve()) for value in _selftest_runner_paths(data) if os.path.isabs(value)}
    if str(runner) not in observed_runners:
        problems.append(
            issue(
                "%s_selftest_runner_mismatch" % label,
                "FAIL",
                "%s self-test does not prove execution of %s" % (label, runner),
            )
        )
    if completed < commit_time:
        problems.append(
            issue(
                "%s_selftest_stale" % label,
                "FAIL",
                "%s self-test completed before the current Git commit" % label,
            )
        )
    passed = (
        overall == "PASS"
        and allowed
        and integers["total"] > 0
        and integers["passed"] == integers["total"]
        and integers["failed"] == 0
        and integers["blocked"] == 0
        and not problems
    )
    status = "PASS" if passed else ("BLOCKED" if overall == "BLOCKED" else "FAIL")
    if not passed and not problems:
        problems.append(
            issue(
                "%s_selftest_not_pass" % label,
                status,
                "%s self-test is not a complete all-cases PASS" % label,
            )
        )
    return {
        "status": status,
        "run_id": run_id,
        "overall_status": overall,
        "release_allowed": allowed,
        "total": integers["total"],
        "passed": integers["passed"],
        "failed": integers["failed"],
        "blocked": integers["blocked"],
        "completed_at": completed_at,
        "runner_path": str(runner),
        "artifact": artifact(resolved),
    }, problems


def live_hooks(repo: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    empty = {
        "status": "BLOCKED",
        "recognized": False,
        "enabled": False,
        "trusted": False,
        "trust_status": "unknown",
        "source_path": str((repo / ".codex" / "hooks.json").resolve()),
        "command": "unavailable",
        "current_hash": "sha256:" + "0" * 64,
        "matcher": None,
    }
    result = {name: dict(empty) for name in HOOK_EVENTS.values()}
    try:
        response = codex_hooks_list(repo)
        payload = response.get("response") if isinstance(response, dict) else None
        payload = payload.get("result") if isinstance(payload, dict) else None
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ReportError("hooks/list returned no data array")
        matching_rows = [row for row in rows if isinstance(row, dict) and Path(str(row.get("cwd", ""))).resolve() == repo]
        if len(matching_rows) != 1:
            raise ReportError("hooks/list returned %d rows for repository" % len(matching_rows))
        row = matching_rows[0]
        if row.get("warnings") or row.get("errors"):
            raise ReportError("hooks/list returned warnings or errors: %s / %s" % (row.get("warnings"), row.get("errors")))
        values = row.get("hooks") if isinstance(row.get("hooks"), list) else []
        source = str((repo / ".codex" / "hooks.json").resolve())
        for wire_name, report_name in HOOK_EVENTS.items():
            matches = [
                value
                for value in values
                if isinstance(value, dict)
                and value.get("eventName") == wire_name
                and str(Path(str(value.get("sourcePath", ""))).resolve()) == source
            ]
            if len(matches) != 1:
                problems.append(
                    issue(
                        "hook_%s_count" % report_name,
                        "BLOCKED",
                        "expected exactly one recognized %s project Hook; found %d" % (report_name, len(matches)),
                    )
                )
                continue
            value = matches[0]
            enabled = value.get("enabled") is True
            trusted = value.get("trustStatus") == "trusted"
            current_hash = value.get("currentHash")
            hash_valid = isinstance(current_hash, str) and len(current_hash) == 71 and current_hash.startswith("sha256:")
            command = value.get("command")
            command_valid = isinstance(command, str) and "harness_verify.py" in command and "--strict" in command
            passed = enabled and trusted and hash_valid and command_valid
            result[report_name] = {
                "status": "PASS" if passed else "BLOCKED",
                "recognized": True,
                "enabled": enabled,
                "trusted": trusted,
                "trust_status": str(value.get("trustStatus") or "unknown"),
                "source_path": source,
                "command": command if isinstance(command, str) and command else "invalid",
                "current_hash": current_hash if hash_valid else "sha256:" + "0" * 64,
                "matcher": value.get("matcher") if isinstance(value.get("matcher"), str) else None,
            }
            if not passed:
                problems.append(
                    issue(
                        "hook_%s_not_ready" % report_name,
                        "BLOCKED",
                        "%s Hook is not enabled, canonical, hash-addressed, and trusted" % report_name,
                    )
                )
    except Exception as exc:
        problems.append(issue("hook_live_verification_failed", "BLOCKED", "live hooks/list failed: %s" % exc))
    return result, problems


def ci_record(repo: Path, remote_status: str, remote_reason: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    workflow = require_regular_file(repo / ".github" / "workflows" / "harness-gate.yml", "CI workflow")
    text = workflow.read_text(encoding="utf-8")
    predicates = (
        ("workflow_validate", "scripts/harness_validate.py" in text, "workflow invokes Harness semantic validation"),
        ("workflow_strict_collect", "harness_verify.py --collect --strict --json" in text, "workflow invokes strict evidence collection"),
        ("workflow_always_upload", "if: always()" in text, "workflow uploads evidence on every outcome"),
        ("workflow_fail_missing_artifacts", "if-no-files-found: error" in text, "workflow fails when evidence artifacts are missing"),
    )
    checks = []
    for check_id, passed, detail in predicates:
        checks.append({"id": check_id, "status": "PASS" if passed else "FAIL", "detail": detail})
        if not passed:
            problems.append(issue(check_id, "FAIL", detail + " check failed"))
    local = "PASS" if all(value[1] for value in predicates) else "FAIL"
    return {
        "status": local,
        "workflow": artifact(workflow),
        "local_validation_status": local,
        "checks": checks,
        "remote_execution": {
            "status": remote_status,
            "critical": False,
            "reason": remote_reason,
        },
    }, problems


def artifact_paths(records: Iterable[dict[str, Any]]) -> list[Path]:
    return [Path(str(record["path"])) for record in records]


def derive_status(problems: list[dict[str, Any]]) -> str:
    critical = [value for value in problems if value.get("critical") is True]
    if any(value.get("status") == "FAIL" for value in critical):
        return "FAIL"
    if any(value.get("status") == "BLOCKED" for value in critical):
        return "BLOCKED"
    if any(value.get("status") == "NOT_RUN" for value in critical):
        return "NOT_RUN"
    return "PASS"


def build_report(
    repo: Path,
    release_path: Path,
    project_selftest_path: Path,
    canonical_selftest_path: Path,
    schema_path: Path,
    *,
    remote_ci_status: str,
    remote_ci_reason: str,
) -> dict[str, Any]:
    repo = repo.resolve()
    if remote_ci_status not in STATUS_VALUES:
        raise ReportError("invalid remote CI status")
    head = run_git(repo, "rev-parse", "HEAD^{commit}")
    commit_time_text = run_git(repo, "show", "-s", "--format=%cI", head)
    commit_time = parse_time(commit_time_text)
    dirty = bool(run_git(repo, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"))
    problems: list[dict[str, Any]] = []
    if dirty:
        problems.append(issue("repository_dirty", "BLOCKED", "repository contains uncommitted or untracked files"))

    canonical_root = (Path.home() / ".agents" / "skills" / "software-delivery-harness").resolve()
    agents_root = (Path.home() / ".codex" / "agents").resolve()
    project_scripts = repo / "scripts"
    canonical_scripts = canonical_root / "scripts"

    canonical_paths = [canonical_root / "SKILL.md"] + [canonical_scripts / name for name in MIRRORED_SCRIPTS]
    project_paths = [repo / ".harness" / "config.json"] + [project_scripts / name for name in MIRRORED_SCRIPTS]
    role_paths = [agents_root / (role + ".toml") for role in AGENT_ROLES]
    for path in canonical_paths + project_paths + role_paths:
        require_regular_file(path, "installed component")

    parity_failures = []
    for name in MIRRORED_SCRIPTS:
        left = project_scripts / name
        right = canonical_scripts / name
        if sha256_file(left) != sha256_file(right):
            parity_failures.append(name)
    if parity_failures:
        problems.append(
            issue(
                "project_canonical_parity",
                "FAIL",
                "project and canonical scripts differ: %s" % ", ".join(parity_failures),
            )
        )
    invalid_roles = []
    for role, path in zip(AGENT_ROLES, role_paths):
        text = path.read_text(encoding="utf-8")
        if ('name = "%s"' % role) not in text or any((label + ":") not in text for label in ("TASK_STATUS", "FILES", "EVIDENCE", "FAILURES", "BLOCKERS", "NOT_RUN")):
            invalid_roles.append(role)
    if invalid_roles:
        problems.append(issue("specialist_agent_protocol", "FAIL", "invalid specialist definitions: %s" % ", ".join(invalid_roles)))

    hooks, hook_problems = live_hooks(repo)
    problems.extend(hook_problems)
    project_test, project_test_problems = selftest_record(
        project_selftest_path,
        project_scripts / "harness_verify.py",
        commit_time,
        "project",
    )
    canonical_test, canonical_test_problems = selftest_record(
        canonical_selftest_path,
        canonical_scripts / "harness_verify.py",
        commit_time,
        "canonical",
    )
    problems.extend(project_test_problems)
    problems.extend(canonical_test_problems)
    if project_test["run_id"] == canonical_test["run_id"] or project_test["artifact"]["path"] == canonical_test["artifact"]["path"]:
        problems.append(issue("selftests_not_independent", "FAIL", "project and canonical self-tests must be distinct runs and artifacts"))
    selftests_status = "PASS" if project_test["status"] == canonical_test["status"] == "PASS" else derive_status(problems)

    ci, ci_problems = ci_record(repo, remote_ci_status, remote_ci_reason)
    problems.extend(ci_problems)
    release, release_problems = release_record(repo, release_path, head)
    problems.extend(release_problems)

    schema_file = require_regular_file(schema_path, "installation report schema")
    generator_file = require_regular_file(Path(__file__), "installation report generator")
    hooks_file = require_regular_file(repo / ".codex" / "hooks.json", "project Hooks configuration")
    components = {
        "canonical_skill": component("PASS", "canonical Skill and runtime scripts are installed", canonical_paths),
        "project_harness": component(
            "FAIL" if parity_failures else "PASS",
            "project Harness scripts match the canonical installation" if not parity_failures else "project/canonical parity failed",
            project_paths,
        ),
        "specialist_agents": component(
            "FAIL" if invalid_roles else "PASS",
            "all ten specialist definitions implement the six-line receipt protocol" if not invalid_roles else "one or more specialist definitions are invalid",
            role_paths,
        ),
        "authorization_launcher": component(
            "FAIL" if any(name in parity_failures for name in ("harness_authorize_subagent.py", "harness_run_subagent.py")) else "PASS",
            "authorization and exclusive-launcher scripts are installed with canonical parity",
            [project_scripts / "harness_authorize_subagent.py", project_scripts / "harness_run_subagent.py"],
        ),
        "hooks": component(
            "PASS" if all(value["status"] == "PASS" for value in hooks.values()) else "BLOCKED",
            "all four project Hooks are recognized, enabled, canonical, and trusted",
            [hooks_file],
        ),
        "selftests": component(
            selftests_status,
            "current project and canonical self-tests are complete all-case PASS runs",
            artifact_paths((project_test["artifact"], canonical_test["artifact"])),
        ),
        "ci_gate": component(ci["status"], "checked-in CI gate contains local fail-closed validation and evidence upload", [Path(ci["workflow"]["path"])]),
        "current_release": component(release["status"], "latest strict release report is bound to current Git HEAD", [Path(release["report"]["path"])]),
    }
    status = derive_status(problems)
    if status == "PASS" and any(value["status"] != "PASS" for value in components.values()):
        status = "BLOCKED"
        problems.append(issue("component_not_pass", "BLOCKED", "one or more critical installation components are not PASS"))
    failures = [value for value in problems if value["status"] == "FAIL"]
    blockers = [value for value in problems if value["status"] == "BLOCKED"]
    not_run_items = [value for value in problems if value["status"] == "NOT_RUN"]
    if remote_ci_status != "PASS":
        not_run_items.append(issue("remote_ci", remote_ci_status, remote_ci_reason, critical=False))
    critical_blockers = [value for value in blockers + not_run_items if value["critical"]]

    generated = utc_now()
    stamp = generated.replace("-", "").replace(":", "").replace(".", "").replace("+", "").replace("Z", "Z")
    return {
        "$schema": "software-delivery-harness-installation-report.schema.json",
        "schema_version": "2.0.0",
        "report_id": "INSTALL-" + stamp,
        "generated_at": generated,
        "installation_status": status,
        "installation_complete": status == "PASS",
        "release_allowed": status == "PASS" and release["release_allowed"],
        "verdict": "Harness installation: " + status,
        "repository": {
            "root": str(repo),
            "git_head": head,
            "git_clean": not dirty,
            "commit_time": commit_time_text,
        },
        "provenance": {
            "generator": artifact(generator_file),
            "schema": artifact(schema_file),
        },
        "components": components,
        "hooks": hooks,
        "selftests": {
            "status": selftests_status,
            "project": project_test,
            "canonical": canonical_test,
        },
        "ci": ci,
        "current_release": release,
        "failures": failures,
        "blockers": blockers,
        "critical_blockers": critical_blockers,
        "not_run_items": not_run_items,
        "known_limitations": [
            "Remote CI execution is reported independently from local CI gate validation and is never upgraded implicitly.",
            "The installation report proves the named current artifacts and receipts; it does not claim that future software has zero defects.",
        ],
    }


def schema_errors(report: dict[str, Any], schema_path: Path) -> list[str]:
    schema_file, schema = load_object(schema_path, "installation report schema")
    validator = StrictSchemaValidator(schema, source=str(schema_file))
    return validator.validate(report)


def verify_artifact_record(record: Any) -> list[str]:
    if not isinstance(record, dict):
        return ["artifact record is not an object"]
    try:
        path = require_regular_file(Path(str(record.get("path", ""))), "reported artifact")
    except ReportError as exc:
        return [str(exc)]
    errors = []
    if record.get("sha256") != sha256_file(path):
        errors.append("artifact hash mismatch: %s" % path)
    if record.get("bytes") != path.stat().st_size:
        errors.append("artifact byte count mismatch: %s" % path)
    return errors


def semantic_errors(report: dict[str, Any], schema_path: Path) -> list[str]:
    errors = list(schema_errors(report, schema_path))
    for record in report.get("provenance", {}).values() if isinstance(report.get("provenance"), dict) else []:
        errors.extend(verify_artifact_record(record))
    components = report.get("components") if isinstance(report.get("components"), dict) else {}
    for value in components.values():
        if isinstance(value, dict) and isinstance(value.get("evidence"), list):
            for record in value["evidence"]:
                errors.extend(verify_artifact_record(record))
    hooks = report.get("hooks") if isinstance(report.get("hooks"), dict) else {}
    selftests = report.get("selftests") if isinstance(report.get("selftests"), dict) else {}
    current = report.get("current_release") if isinstance(report.get("current_release"), dict) else {}
    ci = report.get("ci") if isinstance(report.get("ci"), dict) else {}
    try:
        repo = Path(str(report.get("repository", {}).get("root", ""))).resolve()
        rebuilt = build_report(
            repo,
            Path(str(current.get("report", {}).get("path", ""))),
            Path(str(selftests.get("project", {}).get("artifact", {}).get("path", ""))),
            Path(str(selftests.get("canonical", {}).get("artifact", {}).get("path", ""))),
            schema_path,
            remote_ci_status=str(ci.get("remote_execution", {}).get("status", "NOT_RUN")),
            remote_ci_reason=str(ci.get("remote_execution", {}).get("reason", "not supplied")),
        )
        compared = (
            "installation_status", "installation_complete", "release_allowed", "verdict", "repository", "provenance",
            "components", "hooks", "selftests", "ci", "current_release", "failures", "blockers",
            "critical_blockers", "not_run_items", "known_limitations",
        )
        for name in compared:
            if report.get(name) != rebuilt.get(name):
                errors.append("reported %s does not match live recomputation" % name)
    except Exception as exc:
        errors.append("live semantic recomputation failed: %s" % exc)
    if set(hooks) != {"PreToolUse", "SubagentStart", "SubagentStop", "Stop"}:
        errors.append("report must contain exactly four named Hooks")
    return sorted(set(errors))


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Software Delivery Harness 安装报告",
        "",
        "- 状态：`%s`" % report["installation_status"],
        "- 允许发布：`%s`" % str(report["release_allowed"]).lower(),
        "- 当前提交：`%s`" % report["repository"]["git_head"],
        "- Harness run：`%s`" % report["current_release"]["run_id"],
        "- 生成时间：`%s`" % report["generated_at"],
        "",
        "## 关键组件",
        "",
    ]
    for name, value in report["components"].items():
        lines.append("- `%s`: **%s** — %s" % (name, value["status"], value["summary"]))
    lines.extend(["", "## 四项 Hook", ""])
    for name, value in report["hooks"].items():
        lines.append(
            "- `%s`: **%s**（recognized=%s, enabled=%s, trusted=%s）"
            % (name, value["status"], str(value["recognized"]).lower(), str(value["enabled"]).lower(), str(value["trusted"]).lower())
        )
    lines.extend(["", "## 双路径自检", ""])
    for name in ("project", "canonical"):
        value = report["selftests"][name]
        lines.append("- `%s`: **%s**, %d/%d, `%s`" % (name, value["status"], value["passed"], value["total"], value["run_id"]))
    for title, key in (("失败", "failures"), ("阻断", "blockers"), ("未运行", "not_run_items")):
        lines.extend(["", "## %s" % title, ""])
        values = report[key]
        if values:
            for value in values:
                lines.append("- `%s` (%s, critical=%s): %s" % (value["id"], value["status"], str(value["critical"]).lower(), value["reason"]))
        else:
            lines.append("- NONE")
    lines.extend(
        [
            "",
            "## 机器判定",
            "",
            "```text",
            "HARNESS_STATUS: %s" % report["installation_status"],
            "RELEASE_ALLOWED: %s" % str(report["release_allowed"]).lower(),
            "HARNESS_RUN: %s" % report["current_release"]["run_id"],
            "FAILURES: %s" % ("NONE" if not report["failures"] else ", ".join(value["id"] for value in report["failures"])),
            "BLOCKERS: %s" % ("NONE" if not report["blockers"] else ", ".join(value["id"] for value in report["blockers"])),
            "NOT_RUN: %s" % ("NONE" if not report["not_run_items"] else ", ".join(value["id"] for value in report["not_run_items"])),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def write_atomic(path: Path, data: bytes) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument(
        "--schema",
        default=".harness/schemas/software-delivery-harness-installation-report.schema.json",
    )
    parser.add_argument("--release-report")
    parser.add_argument("--project-selftest")
    parser.add_argument("--canonical-selftest")
    parser.add_argument("--remote-ci-status", choices=STATUS_VALUES, default="NOT_RUN")
    parser.add_argument("--remote-ci-reason", default="No protected remote CI receipt was supplied; local CI gate structure was validated.")
    parser.add_argument("--output-json", default="outputs/software-delivery-harness-installation-report.json")
    parser.add_argument("--output-md", default="outputs/software-delivery-harness-installation-report.md")
    parser.add_argument("--validate", metavar="REPORT_JSON")
    parser.add_argument("--require-pass", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = Path(args.repo).expanduser().resolve()
    schema_path = Path(args.schema)
    if not schema_path.is_absolute():
        schema_path = repo / schema_path
    try:
        if args.validate:
            report_path = Path(args.validate)
            if not report_path.is_absolute():
                report_path = repo / report_path
            _, report = load_object(report_path, "installation report")
            errors = semantic_errors(report, schema_path)
            result = {
                "kind": "installation_report_validation",
                "status": "PASS" if not errors else "FAIL",
                "valid": not errors,
                "report": str(report_path.resolve()),
                "installation_status": report.get("installation_status"),
                "release_allowed": report.get("release_allowed") is True,
                "errors": errors,
            }
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            if errors or (args.require_pass and report.get("installation_status") != "PASS"):
                return 1
            return 0
        missing = [name for name in ("release_report", "project_selftest", "canonical_selftest") if not getattr(args, name)]
        if missing:
            raise ReportError("generation requires explicit current evidence: %s" % ", ".join("--" + name.replace("_", "-") for name in missing))
        def resolve_arg(value: str) -> Path:
            path = Path(value).expanduser()
            return path if path.is_absolute() else repo / path
        report = build_report(
            repo,
            resolve_arg(args.release_report),
            resolve_arg(args.project_selftest),
            resolve_arg(args.canonical_selftest),
            schema_path,
            remote_ci_status=args.remote_ci_status,
            remote_ci_reason=args.remote_ci_reason,
        )
        errors = schema_errors(report, schema_path)
        if errors:
            raise ReportError("generated report failed schema validation: %s" % "; ".join(errors))
        output_json = resolve_arg(args.output_json)
        output_md = resolve_arg(args.output_md)
        write_atomic(output_json, (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        write_atomic(output_md, render_markdown(report).encode("utf-8"))
        print(
            json.dumps(
                {
                    "kind": "installation_report_generation",
                    "status": report["installation_status"],
                    "installation_complete": report["installation_complete"],
                    "release_allowed": report["release_allowed"],
                    "run_id": report["current_release"]["run_id"],
                    "output_json": str(output_json.resolve()),
                    "output_md": str(output_md.resolve()),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1 if args.require_pass and report["installation_status"] != "PASS" else 0
    except (OSError, ValueError, ReportError) as exc:
        print(json.dumps({"kind": "installation_report_error", "status": "FAIL", "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
