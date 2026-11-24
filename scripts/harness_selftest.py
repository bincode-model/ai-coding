#!/usr/bin/env python3
"""Run positive and negative self-tests for Software Delivery Harness."""

import argparse
import copy
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from io import StringIO
from pathlib import Path

import _harness_core as harness_core
import harness_init
from _harness_core import (
    HarnessError,
    STANDARD_VERSION,
    TEST_REPORT_FORMAT,
    StrictSchemaValidator,
    atomic_write_json,
    canonical_sha256,
    exit_code_for_status,
    find_repo,
    find_schema,
    guarded_requirement_snapshot,
    hook_response,
    load_latest_report,
    new_run_id,
    read_hook_input,
    requirement_set_sha256,
    safe_verify,
    strict_json_load,
    strict_json_loads,
    utc_now,
)


SCRIPT_DIR = Path(__file__).resolve().parent
VERIFY_SCRIPT = SCRIPT_DIR / "harness_verify.py"
REPORT_SCRIPT = SCRIPT_DIR / "harness_report.py"
SCHEMA_NAMES = (
    "requirement.schema.json", "change-ledger.schema.json", "test-case.schema.json",
    "test-execution.schema.json", "evidence.schema.json", "traceability.schema.json",
    "acceptance-result.schema.json", "release-report.schema.json",
    "software-delivery-harness-installation-report.schema.json",
)
SCRIPT_NAMES = (
    "_harness_core.py", "harness_authorize_subagent.py", "harness_run_subagent.py", "harness_init.py", "harness_validate.py",
    "harness_verify.py", "harness_report.py", "harness_selftest.py", "harness_install_report.py",
)
SELFTEST_SCHEMA_ROOT = None


def _write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _run(argv, cwd, input_text="", timeout=60):
    started_at = utc_now()
    try:
        completed = subprocess.run(
            [str(value) for value in argv],
            cwd=str(cwd),
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        return {
            "command": [str(value) for value in argv],
            "cwd": str(cwd),
            "started_at": started_at,
            "completed_at": utc_now(),
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": [str(value) for value in argv],
            "cwd": str(cwd),
            "started_at": started_at,
            "completed_at": utc_now(),
            "exit_code": 124,
            "stdout": exc.stdout or "",
            "stderr": (exc.stderr or "") + "\nself-test command timed out",
        }


def _git(root, *args):
    result = _run([shutil.which("git") or "git", "-c", "commit.gpgsign=false"] + list(args), root)
    if result["exit_code"] != 0:
        raise RuntimeError("git command failed: %s\n%s" % (" ".join(args), result["stderr"]))
    return result["stdout"].strip()


def _select_schema_root(target_repo):
    candidates = [
        Path(target_repo) / ".harness" / "schemas",
        SCRIPT_DIR.parent / "assets",
        Path.home() / ".agents" / "skills" / "software-delivery-harness" / "assets",
    ]
    configured = os.environ.get("HARNESS_SCHEMA_ROOT")
    if configured:
        candidates.insert(0, Path(configured).expanduser())
    for candidate in candidates:
        if all((candidate / name).is_file() for name in SCHEMA_NAMES):
            return candidate.resolve()
    raise FileNotFoundError("no complete Harness schema set found; checked: %s" % ", ".join(str(path) for path in candidates))


def _install_fixture_schemas(root):
    if SELFTEST_SCHEMA_ROOT is None:
        raise RuntimeError("self-test schema root was not initialized")
    target = Path(root) / ".harness" / "schemas"
    target.mkdir(parents=True, exist_ok=True)
    for name in SCHEMA_NAMES:
        shutil.copy2(str(Path(SELFTEST_SCHEMA_ROOT) / name), str(target / name))
    canonical_scripts = Path.home() / ".agents" / "skills" / "software-delivery-harness" / "scripts"
    script_target = Path(root) / "scripts"
    script_target.mkdir(parents=True, exist_ok=True)
    for name in SCRIPT_NAMES:
        source = canonical_scripts / name
        if not source.is_file():
            raise RuntimeError("canonical fixture script is missing: %s" % source)
        shutil.copy2(str(source), str(script_target / name))


def _requirement(level, total_levels):
    requirement_id = "REQ-L%d" % level
    parent_id = "REQ-L%d" % (level - 1) if level > 1 else None
    path = ["REQ-L%d" % number for number in range(1, level + 1)]
    return {
        "requirement_id": requirement_id,
        "parent_id": parent_id,
        "level": level,
        "category_path": path,
        "node_type": "leaf" if level == total_levels else "group",
        "title": "Fixture requirement level %d" % level,
        "description": "Observable fixture behavior at hierarchy level %d." % level,
        "source": {"type": "user", "reference": "self-test fixture", "excerpt": None},
        "priority": "P0",
        "criticality": "critical",
        "actors": ["fixture-user"],
        "permissions": [],
        "ui_contract": [],
        "api_contract": [],
        "database_contract": [],
        "file_contract": [],
        "acceptance_criteria": [{
            "criterion_id": "AC-L%d" % level,
            "given": "a committed fixture repository",
            "when": "the deterministic fixture test runs",
            "then": "the observable fixture assertion succeeds",
            "criticality": "critical",
        }],
        "test_case_ids": ["TC-001"],
        "dependencies": [],
        "out_of_scope": [],
        "assumptions": [],
        "created_at": "2026-08-20T00:00:00Z",
        "updated_at": "2026-08-20T00:00:00Z",
    }


def _lane(state, level, name):
    if state == "LINKED":
        return {
            "state": "LINKED",
            "references": [{
                "id": "REF-%s-L%d" % (name.upper(), level),
                "path": "src/feature.py",
                "symbol": "feature_value",
                "line": 1,
            }],
            "reason": None,
        }
    return {
        "state": "NOT_APPLICABLE",
        "references": [],
        "reason": "%s is outside this isolated standard-library fixture" % name,
    }


def _make_static_bundle(levels=5):
    requirements = [_requirement(level, levels) for level in range(1, levels + 1)]
    requirement_ids = [item["requirement_id"] for item in requirements]
    criterion_ids = [item["acceptance_criteria"][0]["criterion_id"] for item in requirements]
    test_case = {
        "test_case_id": "TC-001",
        "requirement_ids": requirement_ids,
        "acceptance_criterion_ids": criterion_ids,
        "contract_ids": [],
        "test_type": "integration",
        "preconditions": ["fixture repository is committed"],
        "steps": [{"step_no": 1, "action": "run the fixture assertion", "input": None}],
        "expected_results": [{"step_no": 1, "expected": "assertion succeeds"}],
        "environment": {
            "name": "self-test",
            "os": sys.platform,
            "services": [],
            "variables": [],
            "secrets_redacted": True,
        },
        "data_fixture": [],
        "automation_path": "tests/test_feature.py",
        "is_mocked": False,
        "criticality": "critical",
    }
    traces = []
    for level in range(1, levels + 1):
        traces.append({
            "trace_id": "TRACE-L%d" % level,
            "requirement_id": "REQ-L%d" % level,
            "acceptance_criterion_id": "AC-L%d" % level,
            "implementation": _lane("LINKED", level, "implementation"),
            "frontend": _lane("NOT_APPLICABLE", level, "frontend"),
            "backend": _lane("NOT_APPLICABLE", level, "backend"),
            "storage": _lane("NOT_APPLICABLE", level, "storage"),
            "permissions": _lane("NOT_APPLICABLE", level, "permissions"),
            "test_case_ids": ["TC-001"],
            "test_execution_ids": ["EX-001"],
            "evidence_ids": ["EV-001"],
            "final_verdict": {
                "state": "LINKED",
                "acceptance_result_requirement_id": "REQ-L%d" % level,
                "status": "PASS",
                "reason": None,
            },
        })
    return {
        "requirements": requirements,
        "baseline_requirements": copy.deepcopy(requirements),
        "test_cases": [test_case],
        "traceability": traces,
        "source_text": "def feature_value():\n    return 42\n",
        "test_text": (
            "import unittest\n\n"
            "from src.feature import feature_value\n\n\n"
            "class FeatureTests(unittest.TestCase):\n"
            "    def test_feature_value(self):\n"
            "        self.assertEqual(feature_value(), 42)\n"
        ),
        "runner_text": (
            "import datetime as dt\n"
            "import hashlib\n"
            "import io\n"
            "import json\n"
            "import pathlib\n"
            "import subprocess\n"
            "import sys\n"
            "import unittest\n\n"
            "def now():\n"
            "    return dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')\n\n"
            "def main():\n"
            "    output = pathlib.Path(sys.argv[1])\n"
            "    runner_path = pathlib.Path('tests/run_harness_tests.py')\n"
            "    automation_path = pathlib.Path('tests/test_feature.py')\n"
            "    command = '%s %s %s' % (sys.executable, runner_path.as_posix(), sys.argv[1])\n"
            "    sys.path.insert(0, str(pathlib.Path.cwd()))\n"
            "    started = now()\n"
            "    stream = io.StringIO()\n"
            "    suite = unittest.defaultTestLoader.discover('tests', pattern='test_feature.py')\n"
            "    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)\n"
            "    completed = now()\n"
            "    failed = len(result.failures) + len(result.errors) + len(result.unexpectedSuccesses)\n"
            "    skipped = len(result.skipped) + len(result.expectedFailures)\n"
            "    passed = result.testsRun - failed - skipped\n"
            "    exit_code = 0 if result.wasSuccessful() and skipped == 0 and result.testsRun > 0 else 1\n"
            "    status = 'PASS' if exit_code == 0 else ('FAIL' if failed or exit_code else 'NOT_RUN')\n"
            "    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD^{commit}'], text=True).strip().lower()\n"
            "    report = {\n"
            "        'format': 'software-delivery-harness-test-report/1',\n"
            "        'runner': 'unittest', 'tool_version': 'python-' + sys.version.split()[0],\n"
            "        'command': command, 'cwd': '.', 'started_at': started, 'completed_at': completed,\n"
            "        'exit_code': exit_code, 'status': status, 'git_commit': commit,\n"
            "        'execution_id': 'EX-001', 'test_case_id': 'TC-001',\n"
            "        'runner_path': runner_path.as_posix(),\n"
            "        'runner_sha256': hashlib.sha256(runner_path.read_bytes()).hexdigest(),\n"
            "        'automation_path': automation_path.as_posix(),\n"
            "        'automation_sha256': hashlib.sha256(automation_path.read_bytes()).hexdigest(),\n"
            "        'test_identifiers': ['test_feature.FeatureTests.test_feature_value'],\n"
            "        'summary': {'tests_run': result.testsRun, 'tests_passed': passed, 'tests_failed': failed, 'tests_skipped': skipped},\n"
            "        'raw_output': stream.getvalue(),\n"
            "    }\n"
            "    output.parent.mkdir(parents=True, exist_ok=True)\n"
            "    output.write_text(json.dumps(report, sort_keys=True, separators=(',', ':')) + '\\n', encoding='utf-8')\n"
            "    sys.stdout.write(stream.getvalue())\n"
            "    return exit_code\n\n"
            "if __name__ == '__main__':\n"
            "    raise SystemExit(main())\n"
        ),
    }


def _write_runtime(root, bundle, head, status="PASS", include_evidence=True):
    root = Path(root)
    requirement_ids = [item["requirement_id"] for item in bundle["requirements"]]
    criterion_ids = []
    for requirement in bundle["requirements"]:
        criterion_ids.extend(item["criterion_id"] for item in requirement.get("acceptance_criteria", []))
    run_dir = root / ".harness" / "runs" / "FIXTURE-RUN"
    run_dir.mkdir(parents=True, exist_ok=True)
    evidence_type = "test_report"
    if status in ("PASS", "FAIL"):
        artifact_path = run_dir / "test-report.json"
        relative_artifact = artifact_path.relative_to(root).as_posix()
        runner_command = [sys.executable, "tests/run_harness_tests.py", relative_artifact]
        runner_result = _run(runner_command, root, timeout=90)
        report = strict_json_load(artifact_path)
        if runner_result["exit_code"] != report.get("exit_code"):
            raise RuntimeError("fixture runner process and report exit codes disagree")
        status = report["status"]
        summary = report["summary"]
        execution = {
            "execution_id": "EX-001", "test_case_id": "TC-001",
            "command": report["command"], "cwd": report["cwd"],
            "started_at": report["started_at"], "completed_at": report["completed_at"],
            "status": status, "exit_code": report["exit_code"],
            "tests_run": summary["tests_run"], "tests_passed": summary["tests_passed"],
            "tests_failed": summary["tests_failed"], "tests_skipped": summary["tests_skipped"],
            "evidence_ids": ["EV-001"] if include_evidence else [],
            "git_commit": head, "environment": {"name": "self-test", "os": sys.platform, "details": []},
            "runner": report["runner"],
            "failure_reason": "fixture unittest failed" if status == "FAIL" else None,
            "blocker": None, "notes": "machine-derived from archived runner report",
        }
        evidence_result, evidence_exit = status, report["exit_code"]
        evidence_command = report["command"]
        evidence_started, evidence_completed = report["started_at"], report["completed_at"]
        evidence_tool_version = report["tool_version"]
    elif status == "BLOCKED":
        artifact_path = run_dir / "blocker-record.json"
        atomic_write_json(artifact_path, {"status": "BLOCKED", "blocker": "required fixture service unavailable"})
        evidence_type = "blocker_record"
        execution = {
            "execution_id": "EX-001", "test_case_id": "TC-001",
            "command": None, "cwd": None, "started_at": None, "completed_at": None,
            "status": "BLOCKED", "exit_code": None, "tests_run": 0, "tests_passed": 0,
            "tests_failed": 0, "tests_skipped": 0, "evidence_ids": ["EV-001"],
            "git_commit": head, "environment": {"name": "self-test", "os": sys.platform, "details": []},
            "runner": "unittest", "failure_reason": None, "blocker": "required fixture service unavailable", "notes": "",
        }
        evidence_result, evidence_exit = "BLOCKED", 2
        evidence_command = "fixture preflight"
        evidence_started, evidence_completed = "2026-08-20T00:01:00Z", "2026-08-20T00:01:01Z"
        evidence_tool_version = "harness-selftest"
    else:
        artifact_path = run_dir / "not-run-record.json"
        execution = {
            "execution_id": "EX-001", "test_case_id": "TC-001",
            "command": None, "cwd": None, "started_at": None, "completed_at": None,
            "status": "NOT_RUN", "exit_code": None, "tests_run": 0, "tests_passed": 0,
            "tests_failed": 0, "tests_skipped": 0, "evidence_ids": [],
            "git_commit": head, "environment": {"name": "self-test", "os": sys.platform, "details": []},
            "runner": "unittest", "failure_reason": None, "blocker": None, "notes": "not executed",
        }
        evidence_result, evidence_exit = "NOT_RUN", 3
        evidence_command = "fixture test was not run"
        evidence_started, evidence_completed = "2026-08-20T00:01:00Z", "2026-08-20T00:01:01Z"
        evidence_tool_version = "harness-selftest"
    evidence_items = []
    if include_evidence:
        digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        evidence_items.append({
            "evidence_id": "EV-001", "execution_ids": ["EX-001"],
            "requirement_ids": requirement_ids, "acceptance_criterion_ids": criterion_ids,
            "test_case_ids": ["TC-001"], "evidence_type": evidence_type,
            "path": artifact_path.relative_to(root).as_posix(), "sha256": digest,
            "command": evidence_command, "exit_code": evidence_exit,
            "started_at": evidence_started, "completed_at": evidence_completed,
            "git_commit": head, "environment": {"name": "self-test", "os": sys.platform, "details": []},
            "tool_version": evidence_tool_version, "result": evidence_result, "notes": "fixture evidence",
        })
    acceptance_results = []
    for requirement_id in requirement_ids:
        if status == "PASS":
            failure_reason, blocker, scope, evidence_ids = None, None, [], ["EV-001"]
        elif status == "FAIL":
            failure_reason, blocker, scope, evidence_ids = "fixture unittest failed", None, [], ["EV-001"]
        elif status == "BLOCKED":
            failure_reason, blocker, scope, evidence_ids = None, "required fixture service unavailable", ["runtime behavior"], ["EV-001"]
        else:
            failure_reason, blocker, scope, evidence_ids = None, None, ["runtime behavior"], []
        acceptance_results.append({
            "requirement_id": requirement_id,
            "status": status,
            "implementation_locations": ["src/feature.py"],
            "test_case_ids": ["TC-001"],
            "evidence_ids": evidence_ids,
            "verified_by": {"agent_id": "SELFTEST-AUDITOR", "role": "runtime_acceptance", "independent_of_implementation": True},
            "verified_at": "2026-08-20T00:02:00Z",
            "failure_reason": failure_reason,
            "blocker": blocker,
            "unverified_scope": scope,
        })
    atomic_write_json(root / ".harness" / "spec" / "test-executions.json", {
        "standard_version": STANDARD_VERSION, "run_id": "FIXTURE-RUN", "git_commit": head,
        "test_executions": [execution],
    })
    atomic_write_json(root / ".harness" / "spec" / "evidence.json", {
        "standard_version": STANDARD_VERSION, "run_id": "FIXTURE-RUN", "git_commit": head,
        "evidence": evidence_items,
    })
    atomic_write_json(root / ".harness" / "spec" / "acceptance-results.json", {
        "standard_version": STANDARD_VERSION, "run_id": "FIXTURE-RUN", "git_commit": head,
        "acceptance_results": acceptance_results,
    })
    return artifact_path


def _build_project(root, levels=5, mutator=None, runtime_status="PASS", include_evidence=True):
    root = Path(root)
    (root / ".harness" / "spec").mkdir(parents=True, exist_ok=True)
    (root / ".harness" / "state").mkdir(parents=True, exist_ok=True)
    _install_fixture_schemas(root)
    bundle = _make_static_bundle(levels)
    if mutator:
        mutator(bundle)
    if runtime_status == "FAIL":
        bundle["test_text"] = bundle["test_text"].replace("feature_value(), 42", "feature_value(), 43")
    for trace in bundle["traceability"]:
        trace["final_verdict"]["status"] = runtime_status
        trace["evidence_ids"] = ["EV-001"] if include_evidence else []
    atomic_write_json(root / ".harness" / "config.json", {
        "standard_version": STANDARD_VERSION,
        "project_name": "harness-selftest-fixture",
        "required_hierarchy_depth": 5,
        "baseline_required": True,
        "strict": True,
        "git_required": True,
        "group_nodes_require_children": True,
        "allowed_statuses": ["PASS", "FAIL", "BLOCKED", "NOT_RUN"],
        "source_scan_paths": ["."],
        "scan_excludes": [],
        "heuristics": {
            "exclude_paths": [".git", ".harness/runs", "__pycache__"],
            "strict_unresolved_critical_findings": True,
        },
        "checks": {
            name: {
                "applicable": name == "integration",
                "command": "python3 -m unittest discover -s tests -p test_feature.py -v" if name == "integration" else None,
                "reason": None if name == "integration" else "check is outside this isolated self-test fixture",
            }
            for name in (
                "dependency_install", "lint", "type_check", "build", "unit", "api",
                "database", "integration", "e2e", "file", "permission", "security",
            )
        },
    })
    atomic_write_json(root / ".harness" / "spec" / "requirements.json", {
        "standard_version": STANDARD_VERSION, "requirements": bundle["requirements"],
    })
    atomic_write_json(root / ".harness" / "spec" / "change-ledger.json", {
        "standard_version": STANDARD_VERSION, "change_ledger": [],
    })
    atomic_write_json(root / ".harness" / "spec" / "test-cases.json", {
        "standard_version": STANDARD_VERSION, "test_cases": bundle["test_cases"],
    })
    atomic_write_json(root / ".harness" / "spec" / "traceability.json", {
        "standard_version": STANDARD_VERSION, "traceability": bundle["traceability"],
    })
    baseline_path = root / ".harness" / "state" / "requirements-baseline.json"
    atomic_write_json(baseline_path, {
        "standard_version": STANDARD_VERSION,
        "approved_at": None, "approved_by": None, "git_commit": None,
        "requirements_sha256": None,
        "requirements": bundle["baseline_requirements"],
    })
    _write_text(root / "src" / "feature.py", bundle["source_text"])
    _write_text(root / "tests" / "test_feature.py", bundle["test_text"])
    _write_text(root / "tests" / "run_harness_tests.py", bundle["runner_text"])
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Harness Selftest")
    _git(root, "config", "user.email", "harness-selftest@example.invalid")
    _git(root, "add", ".harness/config.json", ".harness/schemas", ".harness/spec/requirements.json", ".harness/spec/change-ledger.json", ".harness/spec/test-cases.json", ".harness/spec/traceability.json", "scripts", "src", "tests")
    _git(root, "commit", "-q", "-m", "selftest fixture requirements anchor")
    baseline_anchor = _git(root, "rev-parse", "HEAD^{commit}")
    atomic_write_json(baseline_path, {
        "standard_version": STANDARD_VERSION,
        "approved_at": "2026-08-20T00:00:00Z",
        "approved_by": "SELFTEST-BASELINE-APPROVER",
        "git_commit": baseline_anchor,
        "requirements_sha256": requirement_set_sha256(bundle["baseline_requirements"]),
        "requirements": bundle["baseline_requirements"],
    })
    _git(root, "add", ".harness/state/requirements-baseline.json")
    _git(root, "commit", "-q", "-m", "approve selftest requirement baseline")
    head = _git(root, "rev-parse", "HEAD^{commit}")
    artifact_path = _write_runtime(root, bundle, head, runtime_status, include_evidence)
    return bundle, head, artifact_path


def _run_verify(root):
    command = [sys.executable, str(VERIFY_SCRIPT), "--repo", str(root), "--strict", "--json"]
    result = _run(command, root, input_text="", timeout=90)
    try:
        parsed = strict_json_loads(result["stdout"], "verifier stdout")
    except Exception:
        parsed = None
    result["parsed"] = parsed
    return result


def _expect_verification(root, expected_status, required_codes=()):
    command = _run_verify(root)
    parsed = command.get("parsed") or {}
    codes = {item.get("code") for item in parsed.get("issues", []) if isinstance(item, dict)}
    passed = parsed.get("overall_status") == expected_status
    passed = passed and command["exit_code"] == exit_code_for_status(expected_status)
    passed = passed and all(code in codes for code in required_codes)
    passed = passed and parsed.get("release_allowed") is (expected_status == "PASS")
    return {
        "passed": passed,
        "outcome": "PASS" if passed else "FAIL",
        "expected": {"overall_status": expected_status, "required_codes": list(required_codes)},
        "observed": {"overall_status": parsed.get("overall_status"), "release_allowed": parsed.get("release_allowed"), "codes": sorted(code for code in codes if code)},
        "command_result": command,
    }


def _with_project(callback, **builder_kwargs):
    with tempfile.TemporaryDirectory(prefix="harness-selftest-") as temp_dir:
        root = Path(temp_dir)
        bundle, head, artifact = _build_project(root, **builder_kwargs)
        return callback(root, bundle, head, artifact)


def case_01_pass():
    return _with_project(lambda root, bundle, head, artifact: _expect_verification(root, "PASS"))


def case_02_fail():
    return _with_project(lambda root, bundle, head, artifact: _expect_verification(root, "FAIL", ("TEST_OUTCOME_FAIL",)), runtime_status="FAIL")


def case_03_blocked():
    return _with_project(lambda root, bundle, head, artifact: _expect_verification(root, "BLOCKED", ("TEST_OUTCOME_BLOCKED",)), runtime_status="BLOCKED")


def case_04_not_run():
    return _with_project(lambda root, bundle, head, artifact: _expect_verification(root, "NOT_RUN", ("TEST_OUTCOME_NOT_RUN",)), runtime_status="NOT_RUN", include_evidence=False)


def case_05_missing_evidence():
    def run_case(root, bundle, head, artifact):
        # Keep the PASS execution's evidence ID while removing the evidence record.
        evidence_doc = strict_json_load(root / ".harness" / "spec" / "evidence.json")
        evidence_doc["evidence"] = []
        atomic_write_json(root / ".harness" / "spec" / "evidence.json", evidence_doc)
        return _expect_verification(root, "NOT_RUN", ("EXECUTION_MISSING_EVIDENCE", "EXECUTION_EVIDENCE_NOT_AVAILABLE", "EXECUTION_PASS_WITHOUT_TEST_REPORT"))
    return _with_project(run_case)


def case_06_tampered_evidence():
    def run_case(root, bundle, head, artifact):
        _write_text(artifact, "tampered after digest was recorded\n")
        return _expect_verification(root, "FAIL", ("EVIDENCE_HASH_MISMATCH",))
    return _with_project(run_case)


def case_07_stale_commit():
    def run_case(root, bundle, head, artifact):
        _write_text(root / "src" / "feature.py", "def feature_value():\n    return 43\n")
        _git(root, "add", "src/feature.py")
        _git(root, "commit", "-q", "-m", "change code after evidence")
        return _expect_verification(root, "FAIL", ("EVIDENCE_COMMIT_MISMATCH", "EXECUTION_COMMIT_MISMATCH"))
    return _with_project(run_case)


def case_08_missing_level_five():
    def mutate(bundle):
        bundle["requirements"][-1]["node_type"] = "group"
        bundle["baseline_requirements"] = copy.deepcopy(bundle["requirements"])
    return _with_project(
        lambda root, bundle, head, artifact: _expect_verification(root, "FAIL", ("REQUIRED_DEPTH_MISSING",)),
        levels=4,
        mutator=mutate,
    )


def case_09_parent_cycle():
    def mutate(bundle):
        bundle["requirements"][0]["parent_id"] = "REQ-L2"
        bundle["requirements"][1]["parent_id"] = "REQ-L1"
        bundle["baseline_requirements"] = copy.deepcopy(bundle["requirements"])
    return _with_project(
        lambda root, bundle, head, artifact: _expect_verification(root, "FAIL", ("PARENT_CYCLE",)),
        mutator=mutate,
    )


def case_10_unapproved_deletion():
    def mutate(bundle):
        baseline = copy.deepcopy(bundle["requirements"])
        bundle["requirements"] = bundle["requirements"][:-1]
        bundle["baseline_requirements"] = baseline
        active_requirements = {item["requirement_id"] for item in bundle["requirements"]}
        active_criteria = {criterion["criterion_id"] for item in bundle["requirements"] for criterion in item["acceptance_criteria"]}
        bundle["test_cases"][0]["requirement_ids"] = sorted(active_requirements)
        bundle["test_cases"][0]["acceptance_criterion_ids"] = sorted(active_criteria)
        bundle["traceability"] = [item for item in bundle["traceability"] if item["requirement_id"] in active_requirements]
    return _with_project(
        lambda root, bundle, head, artifact: _expect_verification(root, "FAIL", ("UNAPPROVED_REQUIREMENT_DELETION",)),
        mutator=mutate,
    )


def case_11_skip_only_placeholder():
    def mutate(bundle):
        bundle["test_text"] = "def test_feature_value():\n    assert 42 == 42\n\n# test.only( focused sentinel\n"
        bundle["source_text"] = "def feature_value():\n    raise NotImplementedError\n"
    def run_case(root, bundle, head, artifact):
        execution_doc = strict_json_load(root / ".harness" / "spec" / "test-executions.json")
        execution = execution_doc["test_executions"][0]
        execution.update({
            "status": "NOT_RUN", "exit_code": None, "tests_run": 1, "tests_passed": 0,
            "tests_failed": 0, "tests_skipped": 1, "evidence_ids": [],
            "command": None, "started_at": None, "completed_at": None,
        })
        atomic_write_json(root / ".harness" / "spec" / "test-executions.json", execution_doc)
        acceptance_doc = strict_json_load(root / ".harness" / "spec" / "acceptance-results.json")
        for result in acceptance_doc["acceptance_results"]:
            result.update({"status": "NOT_RUN", "evidence_ids": [], "failure_reason": None, "blocker": None, "unverified_scope": ["skipped fixture"]})
        atomic_write_json(root / ".harness" / "spec" / "acceptance-results.json", acceptance_doc)
        evidence_doc = strict_json_load(root / ".harness" / "spec" / "evidence.json")
        evidence_doc["evidence"] = []
        atomic_write_json(root / ".harness" / "spec" / "evidence.json", evidence_doc)
        result = _expect_verification(root, "FAIL", ("TESTS_SKIPPED", "FOCUSED_TEST", "PLACEHOLDER_REVIEW"))
        return result
    return _with_project(run_case, mutator=mutate)


def case_12_internal_exception():
    def run_case(root, bundle, head, artifact):
        def explode(repo, strict=False, run_id=None):
            raise RuntimeError("injected self-test verifier failure")
        started = utc_now()
        result = safe_verify(root, strict=True, verifier=explode)
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
        codes = {item.get("code") for item in result.get("issues", [])}
        passed = result.get("overall_status") == "FAIL" and not result.get("release_allowed") and "VERIFIER_INTERNAL_ERROR" in codes
        return {
            "passed": passed,
            "outcome": "PASS" if passed else "FAIL",
            "expected": {"overall_status": "FAIL", "code": "VERIFIER_INTERNAL_ERROR"},
            "observed": {"overall_status": result.get("overall_status"), "codes": sorted(codes)},
            "command_result": {
                "command": ["in-process", "safe_verify", "injected RuntimeError"], "cwd": str(root),
                "started_at": started, "completed_at": utc_now(), "exit_code": 1,
                "stdout": payload, "stderr": "",
            },
        }
    return _with_project(run_case)


def case_13_derived_statistics():
    def run_case(root, bundle, head, artifact):
        atomic_write_json(root / ".harness" / "release-report.json", {
            "overall_status": "PASS", "release_allowed": True,
            "requirement_counts": {"total": 999}, "test_counts": {"total": 999},
        })
        command = _run([sys.executable, str(REPORT_SCRIPT), "--repo", str(root), "--strict", "--json"], root, input_text="", timeout=90)
        try:
            report = strict_json_loads(command["stdout"], "report stdout")
        except Exception:
            report = {}
        passed = command["exit_code"] == 0 and report.get("requirement_counts", {}).get("total") == 5 and report.get("test_counts", {}).get("total") == 1
        return {
            "passed": passed, "outcome": "PASS" if passed else "FAIL",
            "expected": {"requirement_total": 5, "test_total": 1},
            "observed": {"requirement_counts": report.get("requirement_counts"), "test_counts": report.get("test_counts")},
            "command_result": command,
        }
    return _with_project(run_case)


def case_14_release_invariant():
    def run_case(root, bundle, head, artifact):
        verification = _run_verify(root)
        report = copy.deepcopy((verification.get("parsed") or {}).get("report", {}))
        report["overall_status"] = "FAIL"
        report["release_allowed"] = True
        report.setdefault("failures", []).append({
            "id": "INVARIANT-FAIL", "requirement_id": None, "test_case_id": None,
            "message": "fixture failure", "evidence_ids": [],
        })
        schema_path = find_schema(root, "release-report.schema.json")
        validator = StrictSchemaValidator(strict_json_load(schema_path), str(schema_path))
        errors = validator.validate(report)
        passed = bool(errors)
        return {
            "passed": passed, "outcome": "PASS" if passed else "FAIL",
            "expected": {"schema_rejects_non_pass_release_true": True},
            "observed": {"schema_error_count": len(errors), "errors": errors[:10]},
            "command_result": {
                "command": ["in-process", "StrictSchemaValidator", "release invariant"], "cwd": str(root),
                "started_at": verification["started_at"], "completed_at": utc_now(), "exit_code": 0 if passed else 1,
                "stdout": json.dumps({"errors": errors}, ensure_ascii=False), "stderr": "",
            },
        }
    return _with_project(run_case)


def _wait_json_response(message_queue, request_id, timeout=15):
    deadline = time.monotonic() + timeout
    captured = []
    while time.monotonic() < deadline:
        remaining = max(0.01, deadline - time.monotonic())
        try:
            raw = message_queue.get(timeout=remaining)
        except queue.Empty:
            break
        captured.append(raw)
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        if message.get("id") == request_id:
            return message, captured
    raise RuntimeError("timed out waiting for app-server response id=%s; received=%s" % (request_id, captured[-5:]))


def _hooks_list(cwd):
    codex = shutil.which("codex")
    if not codex:
        raise FileNotFoundError("codex executable is unavailable")
    started_at = utc_now()
    process = subprocess.Popen(
        [codex, "app-server", "--listen", "stdio://"],
        cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    messages = queue.Queue()

    def read_stdout():
        for line in process.stdout:
            messages.put(line.rstrip("\n"))

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    captured = []
    try:
        initialize = {
            "id": 1, "method": "initialize",
            "params": {"clientInfo": {"name": "harness-selftest", "version": STANDARD_VERSION}, "capabilities": {"experimentalApi": True}},
        }
        process.stdin.write(json.dumps(initialize, separators=(",", ":")) + "\n")
        process.stdin.flush()
        _, lines = _wait_json_response(messages, 1)
        captured.extend(lines)
        process.stdin.write('{"method":"initialized"}\n')
        request = {"id": 2, "method": "hooks/list", "params": {"cwds": [str(Path(cwd).resolve())]}}
        process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        process.stdin.flush()
        response, lines = _wait_json_response(messages, 2)
        captured.extend(lines)
        return response, {
            "command": [codex, "app-server", "hooks/list"], "cwd": str(cwd),
            "started_at": started_at, "completed_at": utc_now(), "exit_code": 0,
            "stdout": "\n".join(captured + [json.dumps(response, ensure_ascii=False)]), "stderr": "",
        }
    finally:
        try:
            process.stdin.close()
        except Exception:
            pass
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


def case_15_hook_protocol_and_recognition(target_repo):
    def run_protocol(root, bundle, head, artifact):
        hook_input = json.dumps({
            "session_id": "SELFTEST-SESSION", "cwd": str(root),
            "hook_event_name": "Stop", "stop_hook_active": False,
            "last_assistant_message": "fixture",
            "model": "selftest-model", "permission_mode": "default",
            "transcript_path": None, "turn_id": "SELFTEST-TURN",
        })
        hook_command = _run(
            [
                sys.executable, str(VERIFY_SCRIPT), "--repo", str(root),
                "--strict", "--json", "--expected-hook", "Stop",
            ],
            root, input_text=hook_input, timeout=90,
        )
        try:
            hook_output = strict_json_loads(hook_command["stdout"], "hook stdout")
        except Exception:
            hook_output = None
        try:
            response, list_command = _hooks_list(target_repo)
        except Exception as exc:
            return {
                "passed": False, "outcome": "BLOCKED",
                "expected": {"hook_stdin": {}, "events": ["preToolUse", "stop", "subagentStart", "subagentStop"]},
                "observed": {"hook_stdin": hook_output, "hooks_list_error": "%s: %s" % (type(exc).__name__, exc)},
                "command_result": hook_command,
            }
        data = response.get("result", {}).get("data", []) if isinstance(response, dict) else []
        matching = None
        for entry in data:
            if Path(entry.get("cwd", "")).resolve() == Path(target_repo).resolve():
                matching = entry
                break
        matching = matching or {}
        events = {hook.get("eventName") for hook in matching.get("hooks", []) if isinstance(hook, dict) and hook.get("enabled")}
        trust_statuses = {hook.get("eventName"): hook.get("trustStatus") for hook in matching.get("hooks", []) if isinstance(hook, dict)}
        hook_records = {
            hook.get("eventName"): {
                "enabled": hook.get("enabled"),
                "trust_status": hook.get("trustStatus"),
                "current_hash": hook.get("currentHash"),
                "source": hook.get("source"),
            }
            for hook in matching.get("hooks", []) if isinstance(hook, dict) and hook.get("eventName")
        }
        errors = matching.get("errors", [])
        required_events = {"preToolUse", "stop", "subagentStart", "subagentStop"}
        passed = (
            hook_command["exit_code"] == 0 and hook_output == {} and not errors
            and required_events.issubset(events)
            and all(trust_statuses.get(event) == "trusted" for event in required_events)
        )
        combined = copy.deepcopy(hook_command)
        combined["command"] = hook_command["command"] + ["AND"] + list_command["command"]
        combined["completed_at"] = list_command["completed_at"]
        combined["stdout"] = hook_command["stdout"] + "\n" + list_command["stdout"]
        combined["stderr"] = hook_command["stderr"] + "\n" + list_command["stderr"]
        return {
            "passed": passed,
            "outcome": "PASS" if passed else ("BLOCKED" if not matching else "FAIL"),
            "expected": {
                "hook_stdin": {},
                "events": ["preToolUse", "stop", "subagentStart", "subagentStop"],
                "trust_statuses": {
                    "preToolUse": "trusted", "stop": "trusted", "subagentStart": "trusted",
                    "subagentStop": "trusted",
                },
                "errors": [],
            },
            "observed": {"hook_stdin": hook_output, "events": sorted(item for item in events if item), "errors": errors, "trust_statuses": trust_statuses, "hook_records": hook_records},
            "command_result": combined,
        }
    return _with_project(run_protocol)


def case_16_missing_project_release_schema():
    def run_case(root, bundle, head, artifact):
        (root / ".harness" / "schemas" / "release-report.schema.json").unlink()
        return _expect_verification(root, "FAIL", ("RELEASE_REPORT_SCHEMA_MISSING",))
    return _with_project(run_case)


def case_17_required_baseline_missing():
    def run_case(root, bundle, head, artifact):
        (root / ".harness" / "state" / "requirements-baseline.json").unlink()
        config = strict_json_load(root / ".harness" / "config.json")
        config["baseline_required"] = False
        atomic_write_json(root / ".harness" / "config.json", config)
        return _expect_verification(root, "FAIL", ("BASELINE_MISSING", "CONFIG_TRUTH_WEAKENED"))
    return _with_project(run_case)


def case_18_forged_change_approval():
    def run_case(root, bundle, head, artifact):
        baseline_doc = strict_json_load(root / ".harness" / "state" / "requirements-baseline.json")
        requirements_doc = strict_json_load(root / ".harness" / "spec" / "requirements.json")
        current = requirements_doc["requirements"][-1]
        current["description"] = "weakened current description"
        old = baseline_doc["requirements"][-1]
        forged_before = guarded_requirement_snapshot(old)
        forged_before["normalized_sha256"] = "0" * 64
        change = {
            "change_id": "CHG-001", "requirement_ids": [current["requirement_id"]],
            "change_type": "scope_reduce", "before": [forged_before],
            "after": [guarded_requirement_snapshot(current)],
            "reason": "fixture forged approval", "requested_by": "SELFTEST-REQUESTER",
            "approved_by": "SELFTEST-APPROVER",
            "impact": {
                "risk_level": "high", "requirements": ["scope changed"], "ui": [],
                "api": [], "database": [], "files": [], "permissions": [],
                "tests": ["TC-001"], "migration_required": False,
            },
            "tests_to_update": ["TC-001"], "timestamp": "2026-08-20T00:00:00Z",
            "base_requirements_sha256": "1" * 64,
            "result_requirements_sha256": "2" * 64,
        }
        atomic_write_json(root / ".harness" / "spec" / "requirements.json", requirements_doc)
        atomic_write_json(root / ".harness" / "spec" / "change-ledger.json", {
            "standard_version": STANDARD_VERSION, "change_ledger": [change],
        })
        return _expect_verification(root, "FAIL", (
            "CHANGE_BASE_HASH_MISMATCH", "CHANGE_RESULT_HASH_MISMATCH",
            "CHANGE_BEFORE_SNAPSHOT_MISMATCH", "UNAPPROVED_REQUIREMENT_CHANGE",
        ))
    return _with_project(run_case)


def case_19_early_leaf_depth_bypass():
    def mutate(bundle):
        bundle["requirements"][3]["node_type"] = "leaf"
        bundle["baseline_requirements"] = copy.deepcopy(bundle["requirements"])
    return _with_project(
        lambda root, bundle, head, artifact: _expect_verification(root, "FAIL", ("REQUIRED_DEPTH_EARLY_LEAF", "LEAF_HAS_CHILDREN")),
        mutator=mutate,
    )


def case_20_trace_contract_path_and_verdict_bypass():
    def mutate(bundle):
        requirement = bundle["requirements"][-1]
        requirement["ui_contract"] = [{
            "contract_id": "UI-001", "page": "fixture", "element_type": "button",
            "label": "Run", "action": "execute", "expected_states": ["complete"],
        }]
        trace = bundle["traceability"][-1]
        trace["implementation"]["references"][0]["path"] = ""
        trace["frontend"] = {
            "state": "LINKED", "references": [{"id": "UI-WRONG", "path": "src/feature.py", "symbol": "feature_value", "line": 1}], "reason": None,
        }
        trace["final_verdict"]["acceptance_result_requirement_id"] = "REQ-L4"
        bundle["baseline_requirements"] = copy.deepcopy(bundle["requirements"])
    return _with_project(
        lambda root, bundle, head, artifact: _expect_verification(root, "FAIL", (
            "TRACE_LINKED_PATH_MISSING", "TRACE_CONTRACT_ID_MISMATCH",
            "TRACE_CONTRACT_ID_MISSING", "TRACE_VERDICT_WRONG_REQUIREMENT",
        )),
        mutator=mutate,
    )


def case_21_handwritten_pass_report():
    def run_case(root, bundle, head, artifact):
        atomic_write_json(artifact, {"status": "PASS", "tests_run": 1})
        evidence_doc = strict_json_load(root / ".harness" / "spec" / "evidence.json")
        evidence_doc["evidence"][0]["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
        atomic_write_json(root / ".harness" / "spec" / "evidence.json", evidence_doc)
        return _expect_verification(root, "FAIL", ("TEST_REPORT_FIELDS_MISSING", "TEST_REPORT_FORMAT_UNSUPPORTED"))
    return _with_project(run_case)


def case_22_owner_leaf_and_link_bypass():
    def run_case(root, bundle, head, artifact):
        requirements_doc = strict_json_load(root / ".harness" / "spec" / "requirements.json")
        requirements_doc["requirements"][-1]["acceptance_criteria"] = []
        atomic_write_json(root / ".harness" / "spec" / "requirements.json", requirements_doc)
        atomic_write_json(root / ".harness" / "state" / "requirements-baseline.json", requirements_doc)
        tests_doc = strict_json_load(root / ".harness" / "spec" / "test-cases.json")
        tests_doc["test_cases"][0]["requirement_ids"] = ["REQ-L1"]
        atomic_write_json(root / ".harness" / "spec" / "test-cases.json", tests_doc)
        evidence_doc = strict_json_load(root / ".harness" / "spec" / "evidence.json")
        evidence_doc["evidence"][0]["requirement_ids"] = ["REQ-L1"]
        atomic_write_json(root / ".harness" / "spec" / "evidence.json", evidence_doc)
        return _expect_verification(root, "FAIL", (
            "LEAF_ACCEPTANCE_CRITERIA_MISSING", "TEST_CRITERION_WRONG_OWNER",
            "EVIDENCE_CRITERION_WRONG_OWNER", "ASYMMETRIC_REQUIREMENT_LINK",
        ))
    return _with_project(run_case)


def case_23_config_weakening_and_default_scan():
    def run_case(root, bundle, head, artifact):
        config = strict_json_load(root / ".harness" / "config.json")
        config.update({
            "strict": False, "git_required": False, "baseline_required": False,
            "group_nodes_require_children": False, "required_hierarchy_depth": 1,
            "allowed_statuses": ["PASS"], "source_scan_paths": [],
        })
        config["heuristics"]["strict_unresolved_critical_findings"] = False
        atomic_write_json(root / ".harness" / "config.json", config)
        _write_text(root / "src" / "feature.py", "def feature_value():\n    raise NotImplementedError\n")
        return _expect_verification(root, "FAIL", (
            "CONFIG_TRUTH_WEAKENED", "CONFIG_HIERARCHY_DEPTH_WEAKENED",
            "CONFIG_STATUS_VOCABULARY_WEAKENED", "CONFIG_HEURISTICS_WEAKENED",
            "PLACEHOLDER_REVIEW",
        ))
    return _with_project(run_case)


def case_24_hook_protocol_matrix():
    def run_case(root, bundle, head, artifact):
        started = utc_now()
        verification = safe_verify(root, strict=True)
        failed = copy.deepcopy(verification)
        failed["overall_status"] = "FAIL"
        failed["release_allowed"] = False
        stop_wire = {
            "_normalized_event": "stop", "cwd": str(root),
            "hook_event_name": "Stop", "last_assistant_message": "fixture",
            "model": "selftest-model", "permission_mode": "default",
            "session_id": "SELFTEST-STOP-SESSION", "stop_hook_active": False,
            "transcript_path": None, "turn_id": "SELFTEST-STOP-TURN",
        }
        stop_pass, _ = hook_response(copy.deepcopy(stop_wire), verification)
        stop_fail, _ = hook_response(copy.deepcopy(stop_wire), failed)
        active_stop_wire = copy.deepcopy(stop_wire)
        active_stop_wire["stop_hook_active"] = True
        stop_active, _ = hook_response(active_stop_wire, failed)
        sub_wire = {
            "_normalized_event": "subagentstop", "agent_id": "SELFTEST-AGENT",
            "agent_transcript_path": None, "agent_type": "frontend_worker",
            "cwd": str(root), "hook_event_name": "SubagentStop",
            "last_assistant_message": "done", "model": "selftest-model",
            "permission_mode": "default", "session_id": "SELFTEST-SUB-SESSION",
            "stop_hook_active": False, "transcript_path": None,
            "turn_id": "SELFTEST-SUB-TURN",
        }
        sub_missing, _ = hook_response(copy.deepcopy(sub_wire), verification)
        sub_fail_wire = copy.deepcopy(sub_wire)
        sub_fail_wire["last_assistant_message"] = "TASK_STATUS: FAIL"
        sub_fail_missing, _ = hook_response(sub_fail_wire, failed)
        sub_active_wire = copy.deepcopy(sub_fail_wire)
        sub_active_wire["stop_hook_active"] = True
        sub_active_wire["last_assistant_message"] = "bad"
        sub_active, _ = hook_response(sub_active_wire, failed)
        evidence_path = artifact.relative_to(root).as_posix()
        complete_message = "\n".join([
            "TASK_STATUS: PASS", "FILES: src/feature.py", "EVIDENCE: %s" % evidence_path,
            "FAILURES: NONE", "BLOCKERS: NONE", "NOT_RUN: NONE",
        ])
        sub_complete_wire = copy.deepcopy(sub_wire)
        sub_complete_wire["last_assistant_message"] = complete_message
        sub_complete, _ = hook_response(sub_complete_wire, verification)
        malformed = read_hook_input(StringIO("{not-json"), expected_event="Stop")
        malformed_response, _ = hook_response(malformed, verification)
        oversized = read_hook_input(
            StringIO("x" * (2 * 1024 * 1024 + 1)), expected_event="Stop"
        )
        oversized_response, _ = hook_response(oversized, verification)
        pretool_wire = {
            "_normalized_event": "pretooluse", "cwd": str(root),
            "hook_event_name": "PreToolUse", "model": "selftest-model",
            "permission_mode": "default", "session_id": "SELFTEST-PRETOOL-SESSION",
            "tool_input": {"agent_type": "requirements_auditor"},
            "tool_name": "Agent", "tool_use_id": "SELFTEST-PRETOOL-CALL",
            "transcript_path": None, "turn_id": "SELFTEST-PRETOOL-TURN",
        }
        pretool_denied, _ = hook_response(pretool_wire, verification)
        observed = {
            "stop_pass": stop_pass, "stop_fail": stop_fail, "stop_active": stop_active,
            "sub_missing": sub_missing, "sub_fail_missing": sub_fail_missing,
            "sub_active": sub_active, "sub_complete": sub_complete,
            "malformed": malformed_response, "oversized": oversized_response,
            "pretool_denied": pretool_denied,
        }
        passed = (
            stop_pass == {}
            and stop_fail.get("decision") == "block"
            and stop_active.get("continue") is False and "stopReason" in stop_active and "PASS" not in json.dumps(stop_active)
            and sub_missing.get("decision") == "block"
            and sub_fail_missing.get("decision") == "block"
            and sub_active.get("continue") is False and "stopReason" in sub_active
            and sub_complete.get("decision") == "block"
            and malformed_response.get("decision") == "block"
            and oversized_response.get("decision") == "block"
            and pretool_denied.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
        )
        return {
            "passed": passed, "outcome": "PASS" if passed else "FAIL",
            "expected": {
                "first_nonpass_blocks": True, "active_stops": True,
                "subagent_authorization_required": True,
                "malformed_blocks": True, "unauthorized_agent_denied_before_spawn": True,
            },
            "observed": observed,
            "command_result": {
                "command": ["in-process", "hook protocol matrix"], "cwd": str(root),
                "started_at": started, "completed_at": utc_now(), "exit_code": 0 if passed else 1,
                "stdout": json.dumps(observed, ensure_ascii=False), "stderr": "",
            },
        }
    return _with_project(run_case)


def case_25_archive_manifest_tamper():
    def run_case(root, bundle, head, artifact):
        started = utc_now()
        verification = safe_verify(root, strict=True)
        run_id = verification["run_id"]
        report_path = Path(verification["run_dir"]) / "release-report.json"
        report = strict_json_load(report_path)
        report["known_limitations"].append("tampered after manifest generation")
        atomic_write_json(report_path, report)
        rejected = False
        error = None
        try:
            load_latest_report(root, run_id)
        except HarnessError as exc:
            rejected = True
            error = str(exc)
        return {
            "passed": rejected, "outcome": "PASS" if rejected else "FAIL",
            "expected": {"manifest_tamper_rejected": True}, "observed": {"rejected": rejected, "error": error},
            "command_result": {
                "command": ["in-process", "load_latest_report", run_id], "cwd": str(root),
                "started_at": started, "completed_at": utc_now(), "exit_code": 0 if rejected else 1,
                "stdout": json.dumps({"rejected": rejected, "error": error}, ensure_ascii=False), "stderr": "",
            },
        }
    return _with_project(run_case)


def case_26_contract_test_type_coverage():
    def mutate(bundle):
        requirement = bundle["requirements"][-1]
        requirement["ui_contract"] = [{"contract_id": "UI-001", "page": "fixture", "element_type": "button", "label": "Run", "action": "execute", "expected_states": ["complete"]}]
        requirement["api_contract"] = [{"contract_id": "API-001", "method": "GET", "path": "/fixture", "request": "none", "success_response": "200", "error_responses": ["403"]}]
        requirement["database_contract"] = [{"contract_id": "DB-001", "operation": "read", "entity": "fixture", "constraints": ["id"], "persistence_expectation": "stable"}]
        requirement["file_contract"] = [{"contract_id": "FILE-001", "action": "export", "formats": ["json"], "validations": ["schema"], "storage_expectation": "run archive"}]
        requirement["permissions"] = [{"permission_id": "PERM-001", "actor": "fixture-user", "action": "read", "resource": "fixture", "expected": "allow"}]
        trace = bundle["traceability"][-1]
        trace["frontend"] = {"state": "LINKED", "references": [{"id": "UI-001", "path": "src/feature.py", "symbol": "feature_value", "line": 1}], "reason": None}
        trace["backend"] = {"state": "LINKED", "references": [{"id": "API-001", "path": "src/feature.py", "symbol": "feature_value", "line": 1}], "reason": None}
        trace["storage"] = {"state": "LINKED", "references": [
            {"id": "DB-001", "path": "src/feature.py", "symbol": "feature_value", "line": 1},
            {"id": "FILE-001", "path": "src/feature.py", "symbol": "feature_value", "line": 1},
        ], "reason": None}
        trace["permissions"] = {"state": "LINKED", "references": [{"id": "PERM-001", "path": "src/feature.py", "symbol": "feature_value", "line": 1}], "reason": None}
        bundle["baseline_requirements"] = copy.deepcopy(bundle["requirements"])
    def run_case(root, bundle, head, artifact):
        config = strict_json_load(root / ".harness" / "config.json")
        config["checks"] = {"security": {"applicable": True, "command": "python3 -m unittest", "reason": None}}
        atomic_write_json(root / ".harness" / "config.json", config)
        result = _run_verify(root)
        parsed = result.get("parsed") or {}
        messages = [item.get("message", "") for item in parsed.get("issues", [])]
        required_types = {"e2e", "api", "database", "file", "permission"}
        observed_types = {test_type for test_type in required_types if any((" %s test" % test_type) in message for message in messages)}
        codes = {item.get("code") for item in parsed.get("issues", [])}
        passed = result["exit_code"] == 1 and observed_types == required_types and "APPLICABLE_CHECK_TEST_TYPE_MISSING" in codes
        return {
            "passed": passed, "outcome": "PASS" if passed else "FAIL",
            "expected": {"missing_contract_test_types": sorted(required_types), "missing_security_test": True},
            "observed": {"missing_contract_test_types": sorted(observed_types), "codes": sorted(code for code in codes if code)},
            "command_result": result,
        }
    return _with_project(run_case, mutator=mutate)


def case_27_config_missing():
    def run_case(root, bundle, head, artifact):
        (root / ".harness" / "config.json").unlink()
        return _expect_verification(root, "FAIL", ("CONFIG_MISSING",))
    return _with_project(run_case)


def case_28_archive_read_failure_report_shape():
    def run_case(root, bundle, head, artifact):
        verification = safe_verify(root, strict=True)
        run_id = verification["run_id"]
        archived_report_path = Path(verification["run_dir"]) / "release-report.json"
        archived_report = strict_json_load(archived_report_path)
        archived_report["known_limitations"].append("tampered after manifest generation")
        atomic_write_json(archived_report_path, archived_report)
        output_path = root / ".harness" / "runs" / "archive-read-failure-report.json"
        command = _run([
            sys.executable, str(REPORT_SCRIPT), "--repo", str(root),
            "--run-id", run_id, "--json", "--output", str(output_path),
        ], root, timeout=90)
        try:
            report = strict_json_loads(command["stdout"], "report stdout")
        except Exception:
            report = {}
        schema_path = root / ".harness" / "schemas" / "release-report.schema.json"
        schema_errors = StrictSchemaValidator(
            strict_json_load(schema_path), str(schema_path),
        ).validate(report)
        failure_ids = {
            item.get("id") for item in report.get("failures", [])
            if isinstance(item, dict)
        }
        passed = (
            command["exit_code"] == 1
            and report.get("overall_status") == "FAIL"
            and report.get("release_allowed") is False
            and "ARCHIVED_REPORT_INVALID" in failure_ids
            and not schema_errors
            and output_path.is_file()
        )
        return {
            "passed": passed, "outcome": "PASS" if passed else "FAIL",
            "expected": {
                "overall_status": "FAIL", "schema_valid": True,
                "failure_id": "ARCHIVED_REPORT_INVALID",
            },
            "observed": {
                "overall_status": report.get("overall_status"),
                "release_allowed": report.get("release_allowed"),
                "failure_ids": sorted(item for item in failure_ids if item),
                "schema_errors": schema_errors,
            },
            "command_result": command,
        }
    return _with_project(run_case)


def case_29_safe_verify_archive_error_report_shape():
    def run_case(root, bundle, head, artifact):
        started = utc_now()
        original_archive = harness_core._write_run_archive

        def explode_verifier(repo, strict=False, run_id=None):
            raise RuntimeError("injected verifier failure before archive")

        def explode_archive(repo, run_id, verification, report):
            raise OSError("injected run archive failure")

        harness_core._write_run_archive = explode_archive
        try:
            verification = safe_verify(root, strict=True, verifier=explode_verifier)
        finally:
            harness_core._write_run_archive = original_archive
        report = verification.get("report") or {}
        schema_path = root / ".harness" / "schemas" / "release-report.schema.json"
        schema_errors = StrictSchemaValidator(
            strict_json_load(schema_path), str(schema_path),
        ).validate(report)
        failure_ids = {
            item.get("id") for item in report.get("failures", [])
            if isinstance(item, dict)
        }
        passed = (
            verification.get("overall_status") == "FAIL"
            and verification.get("release_allowed") is False
            and {"VERIFIER_INTERNAL_ERROR", "RUN_ARCHIVE_ERROR"}.issubset(failure_ids)
            and not schema_errors
        )
        return {
            "passed": passed, "outcome": "PASS" if passed else "FAIL",
            "expected": {
                "overall_status": "FAIL", "schema_valid": True,
                "failure_ids": ["VERIFIER_INTERNAL_ERROR", "RUN_ARCHIVE_ERROR"],
            },
            "observed": {
                "overall_status": verification.get("overall_status"),
                "release_allowed": verification.get("release_allowed"),
                "failure_ids": sorted(item for item in failure_ids if item),
                "schema_errors": schema_errors,
            },
            "command_result": {
                "command": ["in-process", "safe_verify", "injected archive error"],
                "cwd": str(root), "started_at": started, "completed_at": utc_now(),
                "exit_code": 0 if passed else 1,
                "stdout": json.dumps(verification, ensure_ascii=False, sort_keys=True),
                "stderr": "",
            },
        }
    return _with_project(run_case)


def case_30_unsupported_test_runner():
    def run_case(root, bundle, head, artifact):
        report = strict_json_load(artifact)
        report["runner"] = "fabricated-runner"
        atomic_write_json(artifact, report)
        evidence_doc = strict_json_load(root / ".harness" / "spec" / "evidence.json")
        evidence_doc["evidence"][0]["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
        atomic_write_json(root / ".harness" / "spec" / "evidence.json", evidence_doc)
        execution_doc = strict_json_load(root / ".harness" / "spec" / "test-executions.json")
        execution_doc["test_executions"][0]["runner"] = "fabricated-runner"
        atomic_write_json(root / ".harness" / "spec" / "test-executions.json", execution_doc)
        return _expect_verification(root, "FAIL", ("TEST_REPORT_RUNNER_UNSUPPORTED",))
    return _with_project(run_case)


def case_31_pass_trace_links_old_fail_execution():
    def run_case(root, bundle, head, artifact):
        pass_report = strict_json_load(artifact)
        old_path = artifact.parent / "old-fail-report.json"
        old_report = copy.deepcopy(pass_report)
        old_report.update({
            "command": "%s tests/run_harness_tests.py %s" % (sys.executable, old_path.relative_to(root).as_posix()),
            "started_at": "2026-08-20T00:00:00Z", "completed_at": "2026-08-20T00:00:01Z",
            "exit_code": 1, "status": "FAIL", "execution_id": "EX-OLD",
            "summary": {"tests_run": 1, "tests_passed": 0, "tests_failed": 1, "tests_skipped": 0},
            "raw_output": "test_feature_value (test_feature.FeatureTests) ... FAIL\n\nRan 1 test in 0.001s\n\nFAILED (failures=1)\n",
        })
        atomic_write_json(old_path, old_report)
        execution_doc = strict_json_load(root / ".harness" / "spec" / "test-executions.json")
        old_execution = copy.deepcopy(execution_doc["test_executions"][0])
        old_execution.update({
            "execution_id": "EX-OLD", "command": old_report["command"],
            "started_at": old_report["started_at"], "completed_at": old_report["completed_at"],
            "status": "FAIL", "exit_code": 1, "tests_run": 1, "tests_passed": 0,
            "tests_failed": 1, "tests_skipped": 0, "evidence_ids": ["EV-OLD"],
            "failure_reason": "old real failure",
        })
        execution_doc["test_executions"].append(old_execution)
        atomic_write_json(root / ".harness" / "spec" / "test-executions.json", execution_doc)
        evidence_doc = strict_json_load(root / ".harness" / "spec" / "evidence.json")
        old_evidence = copy.deepcopy(evidence_doc["evidence"][0])
        old_evidence.update({
            "evidence_id": "EV-OLD", "execution_ids": ["EX-OLD"],
            "path": old_path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(old_path.read_bytes()).hexdigest(),
            "command": old_report["command"], "exit_code": 1,
            "started_at": old_report["started_at"], "completed_at": old_report["completed_at"],
            "result": "FAIL",
        })
        evidence_doc["evidence"].append(old_evidence)
        atomic_write_json(root / ".harness" / "spec" / "evidence.json", evidence_doc)
        trace_doc = strict_json_load(root / ".harness" / "spec" / "traceability.json")
        for trace in trace_doc["traceability"]:
            trace["test_execution_ids"] = ["EX-OLD"]
            trace["evidence_ids"] = ["EV-OLD"]
        atomic_write_json(root / ".harness" / "spec" / "traceability.json", trace_doc)
        acceptance_doc = strict_json_load(root / ".harness" / "spec" / "acceptance-results.json")
        for result in acceptance_doc["acceptance_results"]:
            result["evidence_ids"] = ["EV-OLD"]
        atomic_write_json(root / ".harness" / "spec" / "acceptance-results.json", acceptance_doc)
        return _expect_verification(root, "FAIL", (
            "TRACE_EXECUTION_NOT_PASS", "TRACE_EVIDENCE_INVALID", "ACCEPTANCE_EVIDENCE_INVALID",
        ))
    return _with_project(run_case)


def case_32_baseline_simultaneous_rewrite():
    def run_case(root, bundle, head, artifact):
        requirements_path = root / ".harness" / "spec" / "requirements.json"
        baseline_path = root / ".harness" / "state" / "requirements-baseline.json"
        requirements_doc = strict_json_load(requirements_path)
        requirements_doc["requirements"][-1]["description"] = "laundered weaker description"
        atomic_write_json(requirements_path, requirements_doc)
        baseline_doc = strict_json_load(baseline_path)
        baseline_doc["requirements"] = copy.deepcopy(requirements_doc["requirements"])
        baseline_doc["requirements_sha256"] = requirement_set_sha256(baseline_doc["requirements"])
        atomic_write_json(baseline_path, baseline_doc)
        return _expect_verification(root, "FAIL", ("BASELINE_ANCHOR_CONTENT_MISMATCH",))
    return _with_project(run_case)


def case_33_disguised_reducing_change_type():
    def run_case(root, bundle, head, artifact):
        baseline_doc = strict_json_load(root / ".harness" / "state" / "requirements-baseline.json")
        requirements_doc = strict_json_load(root / ".harness" / "spec" / "requirements.json")
        old = baseline_doc["requirements"][-1]
        current = requirements_doc["requirements"][-1]
        current["description"] = "weaker"
        atomic_write_json(root / ".harness" / "spec" / "requirements.json", requirements_doc)
        change = {
            "change_id": "CHG-DISGUISED", "requirement_ids": [current["requirement_id"]],
            "change_type": "rename", "before": [guarded_requirement_snapshot(old)],
            "after": [guarded_requirement_snapshot(current)], "reason": "pretend this is a rename",
            "requested_by": "SELFTEST-REQUESTER", "approved_by": None,
            "impact": {"risk_level": "high", "requirements": ["description weakened"], "ui": [], "api": [], "database": [], "files": [], "permissions": [], "tests": ["TC-001"], "migration_required": False},
            "tests_to_update": ["TC-001"], "timestamp": "2026-08-20T00:00:00Z",
            "base_requirements_sha256": requirement_set_sha256(baseline_doc["requirements"]),
            "result_requirements_sha256": requirement_set_sha256(requirements_doc["requirements"]),
        }
        atomic_write_json(root / ".harness" / "spec" / "change-ledger.json", {"standard_version": STANDARD_VERSION, "change_ledger": [change]})
        return _expect_verification(root, "FAIL", (
            "CHANGE_TYPE_MISMATCH", "CHANGE_APPROVER_INVALID", "UNAPPROVED_REQUIREMENT_CHANGE",
        ))
    return _with_project(run_case)


def case_34_unapproved_requirement_addition():
    def mutate(bundle):
        new_requirement = copy.deepcopy(bundle["requirements"][-1])
        new_requirement.update({
            "requirement_id": "REQ-NEW", "parent_id": "REQ-L4", "level": 5,
            "category_path": ["REQ-L1", "REQ-L2", "REQ-L3", "REQ-L4", "REQ-NEW"],
            "title": "New unapproved observable requirement",
        })
        new_requirement["acceptance_criteria"][0]["criterion_id"] = "AC-NEW"
        bundle["requirements"].append(new_requirement)
        bundle["test_cases"][0]["requirement_ids"].append("REQ-NEW")
        bundle["test_cases"][0]["acceptance_criterion_ids"].append("AC-NEW")
        trace = copy.deepcopy(bundle["traceability"][-1])
        trace.update({"trace_id": "TRACE-NEW", "requirement_id": "REQ-NEW", "acceptance_criterion_id": "AC-NEW"})
        trace["final_verdict"]["acceptance_result_requirement_id"] = "REQ-NEW"
        bundle["traceability"].append(trace)
    return _with_project(
        lambda root, bundle, head, artifact: _expect_verification(root, "FAIL", ("UNAPPROVED_REQUIREMENT_ADDITION",)),
        mutator=mutate,
    )


def case_35_scan_configuration_cannot_hide_sources():
    def run_case(root, bundle, head, artifact):
        config = strict_json_load(root / ".harness" / "config.json")
        config["source_scan_paths"] = ["does-not-exist-anywhere"]
        config["scan_excludes"] = ["src", "tests"]
        config["heuristics"]["exclude_paths"] = ["src", "tests"]
        atomic_write_json(root / ".harness" / "config.json", config)
        _write_text(root / "src" / "feature.py", "def feature_value():\n    raise NotImplementedError\n")
        return _expect_verification(root, "FAIL", (
            "SOURCE_SCAN_PATH_INVALID", "CONFIG_SCAN_REQUIRED_PATH_EXCLUDED", "PLACEHOLDER_REVIEW",
        ))
    return _with_project(run_case)


def case_36_applicable_check_fresh_failure():
    def run_case(root, bundle, head, artifact):
        config = strict_json_load(root / ".harness" / "config.json")
        config["checks"]["build"] = {
            "applicable": True,
            "command": "%s -c \"import sys; sys.exit(7)\"" % sys.executable,
            "reason": None,
        }
        atomic_write_json(root / ".harness" / "config.json", config)
        return _expect_verification(
            root, "FAIL", ("APPLICABLE_CHECK_TRUSTED_RECEIPT_REQUIRED",)
        )
    return _with_project(run_case)


def case_37_missing_check_matrix():
    def run_case(root, bundle, head, artifact):
        config = strict_json_load(root / ".harness" / "config.json")
        config["checks"] = {}
        atomic_write_json(root / ".harness" / "config.json", config)
        return _expect_verification(root, "FAIL", ("CONFIG_CHECK_MISSING",))
    return _with_project(run_case)


def case_38_run_id_path_escape():
    def run_case(root, bundle, head, artifact):
        started = utc_now()
        absolute_target = root / "absolute-run-id-escape"
        relative_target = root / ".harness" / "relative-run-id-escape"
        commands = []
        for malicious in (str(absolute_target), "../../relative-run-id-escape"):
            command = _run([
                sys.executable, str(VERIFY_SCRIPT), "--repo", str(root),
                "--strict", "--json", "--run-id", malicious,
            ], root, timeout=90)
            try:
                parsed = strict_json_loads(command["stdout"], "invalid run-id stdout")
            except Exception:
                parsed = {}
            commands.append((command, parsed))
        passed = all(
            command["exit_code"] == 1
            and parsed.get("overall_status") == "FAIL"
            and "RUN_ID_INVALID" in {item.get("code") for item in parsed.get("issues", [])}
            for command, parsed in commands
        ) and not absolute_target.exists() and not relative_target.exists()
        return {
            "passed": passed, "outcome": "PASS" if passed else "FAIL",
            "expected": {"unsafe_run_ids_rejected_before_write": True},
            "observed": {
                "targets_exist": [absolute_target.exists(), relative_target.exists()],
                "statuses": [parsed.get("overall_status") for _command, parsed in commands],
            },
            "command_result": {
                "command": commands[0][0]["command"] + ["AND"] + commands[1][0]["command"],
                "cwd": str(root), "started_at": started, "completed_at": utc_now(),
                "exit_code": 0 if passed else 1,
                "stdout": "\n".join(item[0]["stdout"] for item in commands),
                "stderr": "\n".join(item[0]["stderr"] for item in commands),
            },
        }
    return _with_project(run_case)


def case_39_archive_evidence_tamper_fail_closed_json():
    def run_case(root, bundle, head, artifact):
        verification = safe_verify(root, strict=True)
        _write_text(artifact, "tampered archived evidence\n")
        output_path = root / ".harness" / "runs" / "archive-evidence-failure.json"
        command = _run([
            sys.executable, str(REPORT_SCRIPT), "--repo", str(root),
            "--run-id", verification["run_id"], "--json", "--output", str(output_path),
        ], root, timeout=90)
        try:
            report = strict_json_loads(command["stdout"], "archive evidence failure stdout")
        except Exception:
            report = {}
        schema = strict_json_load(root / ".harness" / "schemas" / "release-report.schema.json")
        schema_errors = StrictSchemaValidator(schema, "release-report.schema.json").validate(report)
        passed = command["exit_code"] == 1 and report.get("overall_status") == "FAIL" and not schema_errors
        return {
            "passed": passed, "outcome": "PASS" if passed else "FAIL",
            "expected": {"schema_valid_fail_json": True},
            "observed": {"status": report.get("overall_status"), "schema_errors": schema_errors},
            "command_result": command,
        }
    return _with_project(run_case)


def case_40_handwritten_pass_rejected_by_fresh_failure():
    def run_case(root, bundle, head, artifact):
        report = strict_json_load(artifact)
        report.update({
            "status": "PASS", "exit_code": 0,
            "summary": {"tests_run": 1, "tests_passed": 1, "tests_failed": 0, "tests_skipped": 0},
            "raw_output": "test_feature_value (test_feature.FeatureTests) ... ok\n\nRan 1 test in 0.001s\n\nOK\n",
        })
        atomic_write_json(artifact, report)
        execution_doc = strict_json_load(root / ".harness" / "spec" / "test-executions.json")
        execution_doc["test_executions"][0].update({
            "status": "PASS", "exit_code": 0, "tests_run": 1, "tests_passed": 1,
            "tests_failed": 0, "tests_skipped": 0, "failure_reason": None,
        })
        atomic_write_json(root / ".harness" / "spec" / "test-executions.json", execution_doc)
        evidence_doc = strict_json_load(root / ".harness" / "spec" / "evidence.json")
        evidence_doc["evidence"][0].update({
            "result": "PASS", "exit_code": 0,
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        })
        atomic_write_json(root / ".harness" / "spec" / "evidence.json", evidence_doc)
        return _expect_verification(root, "FAIL", ("TEST_REPORT_FRESH_STATUS_MISMATCH",))
    return _with_project(run_case, runtime_status="FAIL")


def case_41_collector_end_to_end():
    def run_case(root, bundle, head, artifact):
        command = _run([
            sys.executable, str(VERIFY_SCRIPT), "--repo", str(root),
            "--collect", "--strict", "--json",
        ], root, timeout=180)
        try:
            verification = strict_json_loads(command["stdout"], "collector verifier stdout")
        except Exception:
            verification = {}
        collector = verification.get("collector_result")
        expected_state = {
            ".harness/state/collection-manifest.json",
            ".harness/state/test-executions.json",
            ".harness/state/evidence.json",
            ".harness/state/acceptance-results.json",
            ".harness/state/traceability.json",
        }
        state_files = set(collector.get("state_files", [])) if isinstance(collector, dict) else set()
        state_files.add(".harness/state/collection-manifest.json")
        try:
            archived, _archived_path = load_latest_report(root, verification.get("run_id"))
        except Exception:
            archived = {}
        passed = (
            command["exit_code"] == 0
            and verification.get("overall_status") == "PASS"
            and verification.get("release_allowed") is True
            and isinstance(collector, dict)
            and collector.get("statuses") == {"TC-001": "PASS"}
            and len(collector.get("commands", [])) >= 4
            and expected_state.issubset(state_files)
            and all((root / path).is_file() for path in expected_state)
            and archived.get("overall_status") == "PASS"
            and archived.get("release_allowed") is True
        )
        return {
            "passed": passed,
            "outcome": "PASS" if passed else "FAIL",
            "expected": {"collector_release_allowed": True, "state_files": sorted(expected_state)},
            "observed": {
                "exit_code": command["exit_code"],
                "overall_status": verification.get("overall_status"),
                "release_allowed": verification.get("release_allowed"),
                "collector_run_id": collector.get("run_id") if isinstance(collector, dict) else None,
                "collector_statuses": collector.get("statuses") if isinstance(collector, dict) else None,
                "collector_command_count": len(collector.get("commands", [])) if isinstance(collector, dict) else 0,
                "state_files_present": {path: (root / path).is_file() for path in sorted(expected_state)},
                "archived_status": archived.get("overall_status"),
            },
            "command_result": command,
        }
    return _with_project(run_case)


def case_42_authorized_subagent_lifecycle():
    def run_case(root, bundle, head, artifact):
        policy_dir = root / ".harness" / "policy"
        guard_root = root / ".harness" / "state" / "subagent-guard"
        policy_dir.mkdir(parents=True)
        for name in ("pending", "prestart", "active", "receipts"):
            (guard_root / name).mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            policy_dir / "subagent-roles.json",
            harness_core._canonical_subagent_role_policy(),
        )
        _git(root, "add", ".harness/policy/subagent-roles.json")
        _git(root, "commit", "-q", "-m", "install subagent role policy")
        current_head = _git(root, "rev-parse", "HEAD^{commit}")
        artifact = _write_runtime(root, bundle, current_head, "PASS", True)
        with tempfile.TemporaryDirectory(prefix="harness-subagent-codex-home-") as codex_dir:
            os.chmod(codex_dir, 0o700)
            previous_codex_home = os.environ.get("CODEX_HOME")
            os.environ["CODEX_HOME"] = codex_dir
            try:
                authorize = _run([
                    sys.executable, str(root / "scripts" / "harness_authorize_subagent.py"),
                    "--repo", str(root), "--agent-type", "requirements_auditor",
                    "--review-path", "src/feature.py",
                    "--requirement-id", "REQ-L5", "--test-case-id", "TC-001",
                    "--evidence-id", "EV-001",
                    "--expected-session-id", "SESSION-001",
                    "--expected-turn-id", "TURN-001",
                    "--authorization-id", "AUTH-SELFTEST-LIFECYCLE",
                ], root, timeout=60)
                try:
                    authorization = strict_json_loads(authorize["stdout"], "authorization stdout")
                except Exception:
                    authorization = {}
                before = safe_verify(root, strict=True)
                pretool_input = {
                    "_normalized_event": "pretooluse", "cwd": str(root),
                    "hook_event_name": "PreToolUse", "model": "gpt-selftest",
                    "permission_mode": "default", "session_id": "SESSION-001",
                    "tool_input": {"agent_type": "requirements_auditor"},
                    "tool_name": "Agent", "tool_use_id": "TOOL-001",
                    "transcript_path": None, "turn_id": "TURN-001",
                }
                pretool_response, pretool_exit = hook_response(pretool_input, before)
                reservation_created = (
                    len(list((guard_root / "prestart").glob("*.json"))) == 1
                    and (guard_root / "prestart-lease.json").is_file()
                )
                start_input = {
                    "_normalized_event": "subagentstart",
                    "agent_id": "AGENT-001", "agent_type": "requirements_auditor",
                    "cwd": str(root), "hook_event_name": "SubagentStart",
                    "model": "gpt-selftest", "permission_mode": "default",
                    "session_id": "SESSION-001", "transcript_path": None,
                    "turn_id": "TURN-001",
                }
                start_response, start_exit = hook_response(start_input, before)
                reservation_consumed = (
                    not list((guard_root / "prestart").glob("*.json"))
                    and not (guard_root / "prestart-lease.json").exists()
                )
                during = safe_verify(root, strict=True)
                summary = "\n".join((
                    "TASK_STATUS: PASS",
                    "FILES: src/feature.py",
                    "EVIDENCE: %s" % artifact.relative_to(root).as_posix(),
                    "FAILURES: NONE", "BLOCKERS: NONE", "NOT_RUN: NONE",
                ))
                stop_input = {
                    "_normalized_event": "subagentstop",
                    "agent_id": "AGENT-001", "agent_transcript_path": None,
                    "agent_type": "requirements_auditor", "cwd": str(root),
                    "hook_event_name": "SubagentStop", "last_assistant_message": summary,
                    "model": "gpt-selftest", "permission_mode": "default",
                    "session_id": "SESSION-001", "stop_hook_active": False,
                    "transcript_path": None, "turn_id": "TURN-001",
                }
                stop_response, stop_exit = hook_response(stop_input, during)
                receipt_paths = sorted((guard_root / "receipts").glob("*.json"))
                protected_receipts = sorted(Path(codex_dir).rglob("receipts/*.json"))
                receipt = strict_json_load(receipt_paths[0]) if len(receipt_paths) == 1 else {}
                mirrors_match = (
                    len(receipt_paths) == 1 and len(protected_receipts) == 1
                    and receipt_paths[0].read_bytes() == protected_receipts[0].read_bytes()
                )
                passed = (
                    authorize["exit_code"] == 0 and authorization.get("status") == "PASS"
                    and before.get("overall_status") == "PASS"
                    and before.get("release_allowed") is True
                    and pretool_exit == 0
                    and pretool_response.get("hookSpecificOutput", {}).get("permissionDecision") == "allow"
                    and reservation_created
                    and start_exit == 0 and isinstance(start_response.get("hookSpecificOutput"), dict)
                    and reservation_consumed
                    and during.get("overall_status") == "PASS"
                    and stop_exit == 0 and "systemMessage" in stop_response
                    and "decision" not in stop_response and "continue" not in stop_response
                    and receipt.get("outcome") == "PASS" and receipt.get("task_status") == "PASS"
                    and receipt.get("changed") == {} and mirrors_match
                    and not any((guard_root / "active").iterdir())
                    and not any((guard_root / "pending").iterdir())
                    and not any((guard_root / "prestart").iterdir())
                    and not (guard_root / "lease.json").exists()
                )
                return {
                    "passed": passed, "outcome": "PASS" if passed else "FAIL",
                    "expected": {
                        "authorization": "PASS", "pretool": "allowed_once", "start": "allowed",
                        "stop": "allowed_with_mirrored_receipt", "task_status": "PASS",
                    },
                    "observed": {
                        "authorization_status": authorization.get("status"),
                        "before_status": before.get("overall_status"),
                        "pretool_response": pretool_response,
                        "reservation_created": reservation_created,
                        "start_response": start_response,
                        "reservation_consumed": reservation_consumed,
                        "during_status": during.get("overall_status"),
                        "stop_response": stop_response,
                        "receipt_outcome": receipt.get("outcome"),
                        "receipt_task_status": receipt.get("task_status"),
                        "mirrors_match": mirrors_match,
                        "active_remaining": len(list((guard_root / "active").iterdir())),
                        "lease_exists": (guard_root / "lease.json").exists(),
                    },
                    "command_result": authorize,
                }
            finally:
                if previous_codex_home is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = previous_codex_home
    return _with_project(run_case)


def case_43_truthful_root_stop_report():
    def run_case(root, bundle, head, artifact):
        started = utc_now()
        verification = safe_verify(root, strict=True)
        blocked = copy.deepcopy(verification)
        blocked["overall_status"] = "BLOCKED"
        blocked["release_allowed"] = False
        blocked["issues"] = [
            {
                "code": "FIXTURE_BLOCKER", "status": "BLOCKED",
                "message": "fixture service is unavailable",
            },
            {
                "code": "FIXTURE_NOT_RUN", "status": "NOT_RUN",
                "message": "dependent fixture scope was not run",
            },
        ]
        truthful_message = "\n".join((
            "Harness terminal report follows.",
            "HARNESS_STATUS: BLOCKED",
            "RELEASE_ALLOWED: false",
            "HARNESS_RUN: LATEST",
            "FAILURES: NONE",
            "BLOCKERS: FIXTURE_BLOCKER - fixture service is unavailable",
            "NOT_RUN: FIXTURE_NOT_RUN - dependent fixture scope was not run",
        ))
        wire = {
            "_normalized_event": "stop", "cwd": str(root),
            "hook_event_name": "Stop", "last_assistant_message": truthful_message,
            "model": "selftest-model", "permission_mode": "default",
            "session_id": "SELFTEST-TRUTHFUL-SESSION", "stop_hook_active": False,
            "transcript_path": None, "turn_id": "SELFTEST-TRUTHFUL-TURN-1",
        }
        truthful, truthful_exit = hook_response(copy.deepcopy(wire), blocked)

        false_claim_wire = copy.deepcopy(wire)
        false_claim_wire["last_assistant_message"] = truthful_message + "\n软件交付已经完成，可以发布。"
        false_claim, false_claim_exit = hook_response(false_claim_wire, blocked)

        missing_field_wire = copy.deepcopy(wire)
        missing_field_wire["turn_id"] = "SELFTEST-TRUTHFUL-TURN-2"
        missing_field_wire["last_assistant_message"] = truthful_message.replace(
            "NOT_RUN: FIXTURE_NOT_RUN - dependent fixture scope was not run\n", ""
        ).replace(
            "\nNOT_RUN: FIXTURE_NOT_RUN - dependent fixture scope was not run", ""
        )
        missing_field, missing_field_exit = hook_response(missing_field_wire, blocked)
        passed = (
            truthful_exit == 0
            and truthful.get("continue") is False
            and "Truthful non-PASS Harness report accepted" in truthful.get("stopReason", "")
            and false_claim_exit == 0 and false_claim.get("decision") == "block"
            and missing_field_exit == 0 and missing_field.get("decision") == "block"
            and "attempt 2 of 3" in missing_field.get("reason", "")
        )
        observed = {
            "truthful": truthful,
            "false_completion_claim": false_claim,
            "missing_field_cross_turn": missing_field,
        }
        return {
            "passed": passed, "outcome": "PASS" if passed else "FAIL",
            "expected": {
                "truthful_nonpass_stops_without_retry": True,
                "false_completion_claim_blocks": True,
                "missing_field_blocks": True,
                "unchanged_fingerprint_continues_at_attempt": 2,
            },
            "observed": observed,
            "command_result": {
                "command": ["in-process", "truthful root Stop report protocol"],
                "cwd": str(root), "started_at": started, "completed_at": utc_now(),
                "exit_code": 0 if passed else 1,
                "stdout": json.dumps(observed, ensure_ascii=False), "stderr": "",
            },
        }
    return _with_project(run_case)


def case_44_prestop_collection_guard_lifecycle():
    def run_case(root, bundle, head, artifact):
        policy_dir = root / ".harness" / "policy"
        guard_root = root / ".harness" / "state" / "subagent-guard"
        policy_dir.mkdir(parents=True)
        for name in ("pending", "prestart", "active", "receipts"):
            (guard_root / name).mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            policy_dir / "subagent-roles.json",
            harness_core._canonical_subagent_role_policy(),
        )
        _git(root, "add", ".harness/policy/subagent-roles.json")
        _git(root, "commit", "-q", "-m", "install subagent role policy")
        current_head = _git(root, "rev-parse", "HEAD^{commit}")
        artifact = _write_runtime(root, bundle, current_head, "PASS", True)
        evidence_run_id = "PROC-RUNTIME-SELFTEST-COLLECTION"
        evidence_relative = ".harness/runs/%s/browser-acceptance.json" % evidence_run_id
        with tempfile.TemporaryDirectory(prefix="harness-subagent-collector-home-") as codex_dir:
            os.chmod(codex_dir, 0o700)
            previous_codex_home = os.environ.get("CODEX_HOME")
            os.environ["CODEX_HOME"] = codex_dir
            try:
                authorize = _run([
                    sys.executable, str(root / "scripts" / "harness_authorize_subagent.py"),
                    "--repo", str(root), "--agent-type", "runtime_acceptance",
                    "--allow-path", ".harness/runs/%s/**" % evidence_run_id,
                    "--required-file", evidence_relative,
                    "--requirement-id", "REQ-L5", "--test-case-id", "TC-001",
                    "--evidence-id", "EV-001", "--run-id", evidence_run_id,
                    "--expected-session-id", "SESSION-COLLECTOR",
                    "--expected-turn-id", "TURN-COLLECTOR",
                    "--authorization-id", "AUTH-SELFTEST-PRESTOP-COLLECTOR",
                ], root, timeout=60)
                before = safe_verify(root, strict=True)
                pretool_input = {
                    "_normalized_event": "pretooluse", "cwd": str(root),
                    "hook_event_name": "PreToolUse", "model": "gpt-selftest",
                    "permission_mode": "default", "session_id": "SESSION-COLLECTOR",
                    "tool_input": {"agent_type": "runtime_acceptance"},
                    "tool_name": "Agent", "tool_use_id": "TOOL-COLLECTOR",
                    "transcript_path": None, "turn_id": "TURN-COLLECTOR",
                }
                pretool_response, pretool_exit = hook_response(pretool_input, before)
                start_input = {
                    "_normalized_event": "subagentstart",
                    "agent_id": "AGENT-COLLECTOR", "agent_type": "runtime_acceptance",
                    "cwd": str(root), "hook_event_name": "SubagentStart",
                    "model": "gpt-selftest", "permission_mode": "default",
                    "session_id": "SESSION-COLLECTOR", "transcript_path": None,
                    "turn_id": "TURN-COLLECTOR",
                }
                start_response, start_exit = hook_response(start_input, before)
                evidence_path = root / evidence_relative
                _write_text(evidence_path, "independent browser fixture\n")
                collector = harness_core.collect_runtime(root)
                during = safe_verify(root, strict=True)
                runtime_prefixes = harness_core._guard_runtime_prefixes(root, during)
                collected_evidence = strict_json_load(root / ".harness/state/evidence.json")
                collected_artifact = collected_evidence["evidence"][0]["path"]
                collection_prefix = ".harness/runs/%s" % collector.get("run_id")
                unexpected_path = collection_prefix + "/unexpected-agent-file.bin"
                precision_diff = harness_core._subagent_snapshot_diff(
                    {"entries": {}, "git_index": {}},
                    {
                        "entries": {
                            collected_artifact: {"type": "file", "sha256": "a", "size": 1},
                            unexpected_path: {"type": "file", "sha256": "b", "size": 1},
                        },
                        "git_index": {},
                    },
                    runtime_prefixes=runtime_prefixes,
                )
                summary = "\n".join((
                    "TASK_STATUS: PASS",
                    "FILES: %s" % evidence_relative,
                    "EVIDENCE: %s" % collected_artifact,
                    "FAILURES: NONE", "BLOCKERS: NONE", "NOT_RUN: NONE",
                ))
                stop_input = {
                    "_normalized_event": "subagentstop",
                    "agent_id": "AGENT-COLLECTOR", "agent_transcript_path": None,
                    "agent_type": "runtime_acceptance", "cwd": str(root),
                    "hook_event_name": "SubagentStop", "last_assistant_message": summary,
                    "model": "gpt-selftest", "permission_mode": "default",
                    "session_id": "SESSION-COLLECTOR", "stop_hook_active": False,
                    "transcript_path": None, "turn_id": "TURN-COLLECTOR",
                }
                stop_response, stop_exit = hook_response(stop_input, during)
                receipts = sorted((guard_root / "receipts").glob("*.json"))
                receipt = strict_json_load(receipts[0]) if len(receipts) == 1 else {}
                passed = (
                    authorize["exit_code"] == 0
                    and pretool_exit == 0
                    and pretool_response.get("hookSpecificOutput", {}).get("permissionDecision") == "allow"
                    and start_exit == 0 and isinstance(start_response.get("hookSpecificOutput"), dict)
                    and during.get("overall_status") == "PASS"
                    and during.get("validated_collection_run_id") == collector.get("run_id")
                    and collected_artifact in runtime_prefixes
                    and collection_prefix not in runtime_prefixes
                    and collected_artifact not in precision_diff
                    and unexpected_path in precision_diff
                    and stop_exit == 0 and "systemMessage" in stop_response
                    and receipt.get("outcome") == "PASS"
                    and receipt.get("task_status") == "PASS"
                    and set(receipt.get("changed", {})) == {evidence_relative}
                )
                return {
                    "passed": passed, "outcome": "PASS" if passed else "FAIL",
                    "expected": {
                        "prestop_collection_excluded": True,
                        "stop_receipt": "PASS",
                        "changed": [evidence_relative],
                    },
                    "observed": {
                        "collector_run_id": collector.get("run_id"),
                        "pretool_response": pretool_response,
                        "validated_collection_run_id": during.get("validated_collection_run_id"),
                        "runtime_prefixes": runtime_prefixes,
                        "precision_diff": sorted(precision_diff),
                        "during_status": during.get("overall_status"),
                        "stop_response": stop_response,
                        "receipt_outcome": receipt.get("outcome"),
                        "receipt_task_status": receipt.get("task_status"),
                        "receipt_changed": sorted((receipt.get("changed") or {}).keys()),
                    },
                    "command_result": authorize,
                }
            finally:
                if previous_codex_home is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = previous_codex_home
    return _with_project(run_case)


def case_45_automatic_agent_orchestration_config():
    started = utc_now()
    with tempfile.TemporaryDirectory(prefix="harness-agent-config-") as directory:
        root = Path(directory)
        (root / ".codex").mkdir(parents=True)
        _write_text(
            root / ".codex" / "config.toml",
            "[unrelated]\nvalue = \"preserved\"\n\n"
            "[agents]\nenabled = false\n"
            "max_concurrent_threads_per_session = 1\n",
        )
        installer = harness_init.Installer(root)
        installer.merge_config_toml()
        installer.merge_agents()
        installer.merge_gitignore()
        installer.merge_gitignore()
        installer.merge_harness_config(installer.detect_checks())
        config = (root / ".codex" / "config.toml").read_text(encoding="utf-8")
        policy = (root / "AGENTS.md").read_text(encoding="utf-8")
        gitignore = (root / ".gitignore").read_text(encoding="utf-8")
        harness_config = strict_json_load(root / ".harness" / "config.json")
        broken_root = root / "broken-marker"
        broken_root.mkdir()
        broken_ignore = broken_root / ".gitignore"
        _write_text(broken_ignore, harness_init.GITIGNORE_START_MARKER + "\n")
        broken_before = broken_ignore.read_bytes()
        broken_installer = harness_init.Installer(broken_root)
        broken_installer.merge_gitignore()
        robot_issues = harness_core._robot_orchestration_issues(
            Path.cwd(),
            {"automatic_subagent_orchestration": True},
            {"requirements": {"REQ-ROBOT-CONFIG": {}}},
        )
        expected = {
            "preserved_unrelated_config": '[unrelated]\nvalue = "preserved"' in config,
            "agents_enabled": "enabled = true" in config,
            "bounded_concurrency": "max_concurrent_threads_per_session = 3" in config,
            "interrupt_messages": "interrupt_message = true" in config,
            "hooks_enabled": "hooks = true" in config,
            "native_multi_agent_disabled": "multi_agent = false" in config,
            "orchestration_gate_enabled": harness_config.get("automatic_subagent_orchestration") is True,
            "trusted_user_robot_installation": robot_issues == [],
            "automatic_authorizer": "harness_authorize_subagent.py" in policy,
            "guarded_process_runner": "harness_run_subagent.py" in policy,
            "single_pending_spawn": "exactly one matching specialist" in policy,
            "runtime_robot_pointer_ignored": ".harness/state/latest-robot-run.json" in gitignore,
            "gitignore_merge_idempotent": (
                gitignore.count(harness_init.GITIGNORE_START_MARKER) == 1
                and gitignore.count(harness_init.GITIGNORE_END_MARKER) == 1
            ),
            "malformed_gitignore_blocked_without_write": (
                broken_ignore.read_bytes() == broken_before
                and bool(broken_installer.blockers)
            ),
            "no_blockers": installer.blockers == [],
        }
        passed = all(expected.values())
        return {
            "passed": passed,
            "outcome": "PASS" if passed else "FAIL",
            "expected": {key: True for key in expected},
            "observed": expected,
            "command_result": {
                "command": ["in-process", "Installer.merge_config_toml", "Installer.merge_agents", "Installer.merge_gitignore", "Installer.merge_harness_config"],
                "cwd": str(root),
                "started_at": started,
                "completed_at": utc_now(),
                "exit_code": 0 if passed else 1,
                "stdout": json.dumps(expected, sort_keys=True),
                "stderr": "\n".join(installer.blockers),
            },
        }


def case_46_group_release_report_descendant_links():
    started = utc_now()
    indexes = {
        "requirements": {
            "REQ-GROUP": {"requirement_id": "REQ-GROUP", "criticality": "critical", "test_case_ids": []},
            "REQ-LEAF": {"requirement_id": "REQ-LEAF", "criticality": "critical", "test_case_ids": ["TC-LEAF"]},
        },
        "acceptance": {
            "REQ-GROUP": {"test_case_ids": ["TC-LEAF"], "evidence_ids": ["EV-LEAF"]},
            "REQ-LEAF": {"test_case_ids": ["TC-LEAF"], "evidence_ids": ["EV-LEAF"]},
        },
        "changes": {}, "tests": {"TC-LEAF": {}}, "executions": {},
        "evidence": {}, "traces": {},
    }
    report = harness_core.build_report(
        Path.cwd(), "SELFTEST-GROUP-REPORT", started, utc_now(),
        {"git_commit": "a" * 40}, {"project_name": "selftest"}, [], indexes,
        {}, {"TC-LEAF": "PASS"}, {"REQ-GROUP": "PASS", "REQ-LEAF": "PASS"},
        [], [],
    )
    rows = {item["requirement_id"]: item for item in report["requirements"]}
    observed = rows["REQ-GROUP"]["test_case_ids"]
    passed = observed == ["TC-LEAF"]
    return {
        "passed": passed,
        "outcome": "PASS" if passed else "FAIL",
        "expected": {"group_test_case_ids": ["TC-LEAF"]},
        "observed": {"group_test_case_ids": observed},
        "command_result": {
            "command": ["in-process", "build_report"], "cwd": str(Path.cwd()),
            "started_at": started, "completed_at": utc_now(),
            "exit_code": 0 if passed else 1,
            "stdout": json.dumps(report, sort_keys=True), "stderr": "",
        },
    }


def case_47_subagent_retry_runtime_prefix_history():
    started = utc_now()
    previous = ".harness/runs/RUN-SELFTEST-PREVIOUS"
    current = ".harness/runs/RUN-SELFTEST-CURRENT/release-report.json"
    active = {
        "kind": "subagent_guard_active",
        "trusted_runtime_prefixes": [previous],
    }
    active["integrity_sha256"] = harness_core._active_integrity(active)
    with tempfile.TemporaryDirectory(prefix="harness-runtime-prefix-history-") as directory:
        protected = Path(directory) / "protected.json"
        mirror = Path(directory) / "mirror.json"
        atomic_write_json(protected, active)
        atomic_write_json(mirror, active)
        prefixes = harness_core._guard_accumulated_runtime_prefixes(active, [current])
        persisted = harness_core._persist_guard_runtime_prefixes(
            active, protected, mirror, prefixes
        )
        stop = {
            "entries": {
                previous + "/release-report.json": {"type": "file", "sha256": "a", "size": 1},
                current: {"type": "file", "sha256": "b", "size": 1},
                "src/agent-change.py": {"type": "file", "sha256": "c", "size": 1},
            },
            "git_index": {},
        }
        changed = harness_core._subagent_snapshot_diff(
            {"entries": {}, "git_index": {}}, stop, runtime_prefixes=prefixes
        )
        unsafe_rejected = False
        try:
            harness_core._guard_accumulated_runtime_prefixes(
                persisted, [".harness/runs/../escaped"]
            )
        except HarnessError:
            unsafe_rejected = True
        mirrors_match = protected.read_bytes() == mirror.read_bytes()
    passed = (
        prefixes == [current, previous]
        and persisted.get("trusted_runtime_prefixes") == prefixes
        and sorted(changed) == ["src/agent-change.py"]
        and mirrors_match
        and unsafe_rejected
    )
    observed = {
        "prefixes": prefixes,
        "changed": sorted(changed),
        "mirrors_match": mirrors_match,
        "unsafe_rejected": unsafe_rejected,
    }
    return {
        "passed": passed,
        "outcome": "PASS" if passed else "FAIL",
        "expected": {
            "runtime_prefixes_accumulate_across_retries": True,
            "only_agent_change_remains": ["src/agent-change.py"],
            "unsafe_prefix_rejected": True,
        },
        "observed": observed,
        "command_result": {
            "command": ["in-process", "guard runtime prefix history"],
            "cwd": str(Path.cwd()), "started_at": started, "completed_at": utc_now(),
            "exit_code": 0 if passed else 1,
            "stdout": json.dumps(observed, sort_keys=True), "stderr": "",
        },
    }


def case_48_independent_nonpass_downgrade():
    started = utc_now()
    summary = "\n".join((
        "TASK_STATUS: FAIL", "FILES: tests/security-review.txt",
        "EVIDENCE: .harness/runs/RUN-SELFTEST-EVIDENCE/report.json",
        "FAILURES: independent reviewer found a release defect",
        "BLOCKERS: NONE", "NOT_RUN: NONE",
    ))
    effective, details = harness_core._effective_subagent_task_status(summary, "PASS")
    not_weakened, _ = harness_core._effective_subagent_task_status(
        summary.replace("TASK_STATUS: FAIL", "TASK_STATUS: PASS"), "FAIL"
    )
    original_evaluate = harness_core._evaluate_subagent_stop
    original_finalize = harness_core._finalize_subagent_guard
    try:
        harness_core._evaluate_subagent_stop = lambda _wire, _verification: {
            "active": {"authorization": {"authorization_id": "AUTH-SELFTEST-DOWNGRADE"}},
            "changed": {}, "errors": [], "snapshot_sha256": "a" * 64,
            "task_status": "FAIL", "task_status_details": details,
        }
        harness_core._finalize_subagent_guard = (
            lambda _wire, _verification, _guard, outcome:
            ".harness/state/subagent-guard/receipts/%s.json" % outcome
        )
        wire = {
            "_normalized_event": "subagentstop", "agent_id": "AGENT-DOWNGRADE",
            "agent_transcript_path": None, "agent_type": "security_reviewer",
            "cwd": str(Path.cwd()), "hook_event_name": "SubagentStop",
            "last_assistant_message": summary, "model": "gpt-selftest",
            "permission_mode": "default", "session_id": "SESSION-DOWNGRADE",
            "stop_hook_active": False, "transcript_path": None,
            "turn_id": "TURN-DOWNGRADE",
        }
        response, exit_code = hook_response(
            wire,
            {
                "repo": str(Path.cwd()), "run_id": "RUN-SELFTEST-DOWNGRADE",
                "run_dir": str(Path.cwd() / ".harness/runs/RUN-SELFTEST-DOWNGRADE"),
                "overall_status": "PASS", "release_allowed": True,
            },
        )
    finally:
        harness_core._evaluate_subagent_stop = original_evaluate
        harness_core._finalize_subagent_guard = original_finalize
    passed = (
        effective == "FAIL"
        and not_weakened == "FAIL"
        and exit_code == 0
        and response.get("continue") is False
        and "accepted without a redundant repair retry" in response.get("stopReason", "")
        and "Receipt:" in response.get("stopReason", "")
        and "decision" not in response
    )
    observed = {
        "effective_status": effective, "details": details,
        "computed_fail_not_weakened": not_weakened,
        "response": response, "exit_code": exit_code,
    }
    return {
        "passed": passed,
        "outcome": "PASS" if passed else "FAIL",
        "expected": {
            "independent_pass_to_fail_downgrade": "FAIL",
            "computed_fail_cannot_be_weakened": "FAIL",
            "truthful_nonpass_finalizes_without_retry": True,
        },
        "observed": observed,
        "command_result": {
            "command": ["in-process", "independent non-PASS downgrade protocol"],
            "cwd": str(Path.cwd()), "started_at": started, "completed_at": utc_now(),
            "exit_code": 0 if passed else 1,
            "stdout": json.dumps(observed, ensure_ascii=False, sort_keys=True), "stderr": "",
        },
    }


def case_49_pretool_agent_single_reservation():
    def run_case(root, bundle, head, artifact):
        policy_dir = root / ".harness" / "policy"
        guard_root = root / ".harness" / "state" / "subagent-guard"
        policy_dir.mkdir(parents=True)
        for name in ("pending", "prestart", "active", "receipts"):
            (guard_root / name).mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            policy_dir / "subagent-roles.json",
            harness_core._canonical_subagent_role_policy(),
        )
        _git(root, "add", ".harness/policy/subagent-roles.json")
        _git(root, "commit", "-q", "-m", "install pretool role policy")
        authorize = _run([
            sys.executable, str(root / "scripts" / "harness_authorize_subagent.py"),
            "--repo", str(root), "--agent-type", "requirements_auditor",
            "--review-path", "src/feature.py",
            "--requirement-id", "REQ-L5", "--test-case-id", "TC-001",
            "--evidence-id", "EV-001",
            "--expected-session-id", "SESSION-PRETOOL",
            "--expected-turn-id", "TURN-PRETOOL",
            "--authorization-id", "AUTH-SELFTEST-PRETOOL-SINGLE",
        ], root, timeout=60)
        verification = {
            "repo": str(root), "overall_status": "BLOCKED", "release_allowed": False,
        }
        first_wire = {
            "_normalized_event": "pretooluse", "cwd": str(root),
            "hook_event_name": "PreToolUse", "model": "gpt-selftest",
            "permission_mode": "default", "session_id": "SESSION-PRETOOL",
            "tool_input": {"agent_type": "requirements_auditor"},
            "tool_name": "spawn_agent", "tool_use_id": "TOOL-PRETOOL-ONE",
            "transcript_path": None, "turn_id": "TURN-PRETOOL",
        }
        first, first_exit = hook_response(copy.deepcopy(first_wire), verification)
        second_wire = copy.deepcopy(first_wire)
        second_wire["tool_use_id"] = "TOOL-PRETOOL-TWO"
        second, second_exit = hook_response(second_wire, verification)
        reservation_files = list((guard_root / "prestart").glob("*.json"))
        passed = (
            authorize["exit_code"] == 0
            and first_exit == 0
            and first.get("hookSpecificOutput", {}).get("permissionDecision") == "allow"
            and second_exit == 0
            and second.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
            and len(reservation_files) == 1
            and (guard_root / "prestart-lease.json").is_file()
            and not list((guard_root / "pending").glob("*.json"))
        )
        observed = {
            "first": first,
            "second": second,
            "reservations": [path.name for path in reservation_files],
            "prestart_lease": (guard_root / "prestart-lease.json").is_file(),
        }
        return {
            "passed": passed, "outcome": "PASS" if passed else "FAIL",
            "expected": {
                "first_agent_call": "allow",
                "concurrent_second_agent_call": "deny",
                "single_atomic_reservation": True,
            },
            "observed": observed,
            "command_result": {
                "command": ["in-process", "PreToolUse Agent single reservation"],
                "cwd": str(root), "started_at": utc_now(), "completed_at": utc_now(),
                "exit_code": 0 if passed else 1,
                "stdout": json.dumps(observed, ensure_ascii=False),
                "stderr": authorize.get("stderr", ""),
            },
        }
    return _with_project(run_case)


EXTRA_CASES = (
    ("16_missing_project_release_schema", case_16_missing_project_release_schema),
    ("17_required_baseline_missing", case_17_required_baseline_missing),
    ("18_forged_change_approval", case_18_forged_change_approval),
    ("19_early_leaf_depth_bypass", case_19_early_leaf_depth_bypass),
    ("20_trace_contract_path_verdict_bypass", case_20_trace_contract_path_and_verdict_bypass),
    ("21_handwritten_pass_report", case_21_handwritten_pass_report),
    ("22_owner_leaf_link_bypass", case_22_owner_leaf_and_link_bypass),
    ("23_config_weakening_default_scan", case_23_config_weakening_and_default_scan),
    ("24_hook_protocol_matrix", case_24_hook_protocol_matrix),
    ("25_archive_manifest_tamper", case_25_archive_manifest_tamper),
    ("26_contract_test_type_coverage", case_26_contract_test_type_coverage),
    ("27_config_missing", case_27_config_missing),
    ("28_archive_read_failure_report_shape", case_28_archive_read_failure_report_shape),
    ("29_safe_verify_archive_error_report_shape", case_29_safe_verify_archive_error_report_shape),
    ("30_unsupported_test_runner", case_30_unsupported_test_runner),
    ("31_pass_trace_links_old_fail_execution", case_31_pass_trace_links_old_fail_execution),
    ("32_baseline_simultaneous_rewrite", case_32_baseline_simultaneous_rewrite),
    ("33_disguised_reducing_change_type", case_33_disguised_reducing_change_type),
    ("34_unapproved_requirement_addition", case_34_unapproved_requirement_addition),
    ("35_scan_configuration_cannot_hide_sources", case_35_scan_configuration_cannot_hide_sources),
    ("36_applicable_check_requires_trusted_receipt", case_36_applicable_check_fresh_failure),
    ("37_missing_check_matrix", case_37_missing_check_matrix),
    ("38_run_id_path_escape", case_38_run_id_path_escape),
    ("40_handwritten_pass_rejected_by_fresh_failure", case_40_handwritten_pass_rejected_by_fresh_failure),
    ("41_collector_end_to_end", case_41_collector_end_to_end),
    ("42_authorized_subagent_lifecycle", case_42_authorized_subagent_lifecycle),
    ("43_truthful_root_stop_report", case_43_truthful_root_stop_report),
    ("44_prestop_collection_guard_lifecycle", case_44_prestop_collection_guard_lifecycle),
    ("45_automatic_agent_orchestration_config", case_45_automatic_agent_orchestration_config),
    ("46_group_release_report_descendant_links", case_46_group_release_report_descendant_links),
    ("47_subagent_retry_runtime_prefix_history", case_47_subagent_retry_runtime_prefix_history),
    ("48_independent_nonpass_downgrade", case_48_independent_nonpass_downgrade),
    ("49_pretool_agent_single_reservation", case_49_pretool_agent_single_reservation),
)


CASES = (
    ("01_complete_pass", case_01_pass),
    ("02_executed_fail", case_02_fail),
    ("03_blocked_release", case_03_blocked),
    ("04_not_run_release", case_04_not_run),
    ("05_missing_evidence", case_05_missing_evidence),
    ("06_tampered_evidence_hash", case_06_tampered_evidence),
    ("07_stale_git_commit", case_07_stale_commit),
    ("08_missing_level_five", case_08_missing_level_five),
    ("09_parent_cycle", case_09_parent_cycle),
    ("10_unapproved_deletion", case_10_unapproved_deletion),
    ("11_skip_only_placeholder", case_11_skip_only_placeholder),
    ("12_verifier_internal_error", case_12_internal_exception),
    ("13_derived_statistics", case_13_derived_statistics),
    ("14_release_allowed_invariant", case_14_release_invariant),
)


def _artifact_digest(path):
    data = Path(path).read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def main(argv=None):
    global SELFTEST_SCHEMA_ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="installed project whose Hooks are checked in case 15")
    parser.add_argument("--output-dir", help="self-test result directory")
    parser.add_argument("--json", action="store_true", help="emit compact JSON")
    args = parser.parse_args(argv)
    target_repo = find_repo(args.repo)
    SELFTEST_SCHEMA_ROOT = _select_schema_root(target_repo)
    selftest_run_id = new_run_id("SELFTEST")
    output_dir = Path(args.output_dir).resolve() if args.output_dir else target_repo / ".harness" / "runs" / selftest_run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    started_at = utc_now()
    results = []
    all_cases = list(CASES) + [("15_hook_protocol_and_recognition", lambda: case_15_hook_protocol_and_recognition(target_repo))] + list(EXTRA_CASES)
    for name, function in all_cases:
        case_started = utc_now()
        try:
            result = function()
        except BaseException as exc:
            result = {
                "passed": False, "outcome": "FAIL",
                "expected": {}, "observed": {"exception": "%s: %s" % (type(exc).__name__, exc)},
                "command_result": {
                    "command": ["selftest-case", name], "cwd": str(target_repo),
                    "started_at": case_started, "completed_at": utc_now(), "exit_code": 1,
                    "stdout": "", "stderr": traceback.format_exc(),
                },
            }
        case_dir = output_dir / name
        case_dir.mkdir(parents=True, exist_ok=True)
        command_result = result.pop("command_result", {})
        stdout_path = case_dir / "stdout.txt"
        stderr_path = case_dir / "stderr.txt"
        _write_text(stdout_path, str(command_result.pop("stdout", "")))
        _write_text(stderr_path, str(command_result.pop("stderr", "")))
        result.update({
            "case": name,
            "started_at": case_started,
            "completed_at": utc_now(),
            "command": command_result,
            "stdout_path": stdout_path.relative_to(output_dir).as_posix(),
            "stderr_path": stderr_path.relative_to(output_dir).as_posix(),
            "stdout": stdout_path.relative_to(output_dir).as_posix(),
            "stderr": stderr_path.relative_to(output_dir).as_posix(),
            "generated_files": [
                stdout_path.relative_to(output_dir).as_posix(),
                stderr_path.relative_to(output_dir).as_posix(),
                (case_dir / "result.json").relative_to(output_dir).as_posix(),
            ],
            "failures": [] if result.get("outcome") == "PASS" else [result.get("observed", {"message": "self-test case failed"})],
        })
        atomic_write_json(case_dir / "result.json", result)
        results.append(result)
    passed_count = sum(1 for result in results if result.get("outcome") == "PASS")
    blocked_count = sum(1 for result in results if result.get("outcome") == "BLOCKED")
    failed_count = len(results) - passed_count - blocked_count
    overall = "PASS" if passed_count == len(results) else ("FAIL" if failed_count else "BLOCKED")
    summary = {
        "standard_version": STANDARD_VERSION,
        "kind": "harness_selftest_result",
        "run_id": selftest_run_id,
        "repo": str(target_repo),
        "started_at": started_at,
        "completed_at": utc_now(),
        "overall_status": overall,
        "release_allowed": overall == "PASS",
        "total": len(results),
        "passed": passed_count,
        "failed": failed_count,
        "blocked": blocked_count,
        "cases": results,
        "artifacts": [],
    }
    atomic_write_json(output_dir / "selftest-result.json", summary)
    for path in sorted(output_dir.rglob("*")):
        if path.is_file() and path.name != "selftest-result.json":
            summary["artifacts"].append(_artifact_digest(path))
    atomic_write_json(output_dir / "selftest-result.json", summary)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return exit_code_for_status(overall)


if __name__ == "__main__":
    sys.exit(main())
