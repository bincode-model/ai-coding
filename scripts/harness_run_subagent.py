#!/usr/bin/env python3
"""Run one authorized specialist in an independently guarded Codex process."""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from _harness_core import (
    HarnessError,
    _evaluate_subagent_stop,
    _finalize_subagent_guard,
    _guard_directories,
    _guard_external_directories,
    _guard_key,
    _load_subagent_policy,
    atomic_write_json,
    canonical_sha256,
    find_repo,
    hook_response,
    load_latest_report,
    safe_verify,
    strict_json_load,
)


VERSION = "1.0.0"
MAX_TASK_BYTES = 256 * 1024
MAX_FINAL_MESSAGE_BYTES = 2 * 1024 * 1024
MAX_CAPTURE_BYTES = 64 * 1024 * 1024
MAX_RECEIPT_BYTES = 4 * 1024 * 1024
MAX_RECEIPT_FILES = 10000
SAFE_ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9_.-]{2,127}$")
SUMMARY_LABELS = (
    "TASK_STATUS", "FILES", "EVIDENCE", "FAILURES", "BLOCKERS", "NOT_RUN",
)


class RunnerError(RuntimeError):
    pass


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def format_utc(value):
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def read_limited(path, limit, label):
    path = Path(path)
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise RunnerError("%s is unavailable: %s" % (label, exc))
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise RunnerError("%s is not a safe regular file" % label)
    size = path.stat().st_size
    if size > limit:
        raise RunnerError("%s exceeds %d bytes" % (label, limit))
    return path.read_bytes()


def receipt_directory_snapshot(directory, protected=False):
    """Hash every directory entry so additions cannot hide receipt tampering."""
    directory = Path(directory)
    try:
        info = directory.lstat()
    except OSError as exc:
        raise RunnerError("receipt directory is unavailable: %s" % exc)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RunnerError("receipt directory is not a safe real directory")
    if protected:
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise RunnerError("protected receipt directory must have mode 0700")
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise RunnerError("protected receipt directory is not owned by the current user")
    try:
        children = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise RunnerError("cannot enumerate receipt directory: %s" % exc)
    if len(children) > MAX_RECEIPT_FILES:
        raise RunnerError("receipt directory exceeds the bounded file count")
    snapshot = {}
    for child in children:
        if child.name in snapshot or not child.name or child.name in {".", ".."}:
            raise RunnerError("receipt directory contains an unsafe or duplicate name")
        payload = read_limited(child, MAX_RECEIPT_BYTES, "receipt entry %s" % child.name)
        child_info = child.lstat()
        if protected:
            if stat.S_IMODE(child_info.st_mode) != 0o600:
                raise RunnerError("protected receipt file must have mode 0600: %s" % child.name)
            if hasattr(os, "geteuid") and child_info.st_uid != os.geteuid():
                raise RunnerError("protected receipt file is not owned by the current user: %s" % child.name)
        snapshot[child.name] = sha256_bytes(payload)
    return snapshot


def validate_receipt_history_pair(repository_snapshot, protected_snapshot):
    """Require every historical repository receipt to match its protected authority."""
    if not isinstance(repository_snapshot, dict) or not isinstance(protected_snapshot, dict):
        raise RunnerError("receipt history snapshots must be objects")
    repository_history = dict(repository_snapshot)
    repository_history.pop(".gitkeep", None)
    receipt_name = re.compile(r"^[0-9a-f]{64}-[0-9a-f]{32}\.json$")
    unsafe_repository = sorted(
        name for name in repository_history if receipt_name.fullmatch(name) is None
    )
    unsafe_protected = sorted(
        name for name in protected_snapshot if receipt_name.fullmatch(name) is None
    )
    if unsafe_repository or unsafe_protected:
        raise RunnerError(
            "receipt history contains an unsafe entry: repository=%s protected=%s"
            % (unsafe_repository, unsafe_protected)
        )
    missing_repository = sorted(set(protected_snapshot) - set(repository_history))
    missing_protected = sorted(set(repository_history) - set(protected_snapshot))
    mismatched = sorted(
        name
        for name in set(repository_history) & set(protected_snapshot)
        if repository_history[name] != protected_snapshot[name]
    )
    if missing_repository or missing_protected or mismatched:
        raise RunnerError(
            "repository receipt history does not match protected authority: "
            "missing_repository=%s missing_protected=%s digest_mismatch=%s"
            % (missing_repository, missing_protected, mismatched)
        )


def _single_receipt_addition(before, after, label):
    changed = sorted(
        name for name, digest in before.items() if after.get(name) != digest
    )
    if changed:
        raise RunnerError("%s receipt history was removed or modified: %s" % (label, ", ".join(changed)))
    additions = sorted(set(after) - set(before))
    if len(additions) != 1:
        raise RunnerError(
            "%s must contain exactly one new receipt; found %d"
            % (label, len(additions))
        )
    return additions[0]


def select_authoritative_receipt(
    repo,
    repository_receipts,
    protected_receipts,
    before_repository,
    before_protected,
    expected,
):
    """Authenticate one exact protected/repository receipt pair for this run."""
    repo = Path(repo).resolve()
    repository_receipts = Path(repository_receipts)
    protected_receipts = Path(protected_receipts)
    after_repository = receipt_directory_snapshot(repository_receipts)
    after_protected = receipt_directory_snapshot(protected_receipts, protected=True)
    repository_name = _single_receipt_addition(
        before_repository, after_repository, "repository"
    )
    protected_name = _single_receipt_addition(
        before_protected, after_protected, "protected"
    )
    if repository_name != protected_name:
        raise RunnerError("repository and protected receipt names do not match")
    validate_receipt_history_pair(after_repository, after_protected)
    guard_key = expected.get("guard_key")
    if not isinstance(guard_key, str) or re.fullmatch(r"[0-9a-f]{64}", guard_key) is None:
        raise RunnerError("expected guard key is invalid")
    if re.fullmatch(re.escape(guard_key) + r"-[0-9a-f]{32}\.json", repository_name) is None:
        raise RunnerError("receipt filename is not bound to the current guard key")
    repository_path = repository_receipts / repository_name
    protected_path = protected_receipts / protected_name
    repository_payload = read_limited(
        repository_path, MAX_RECEIPT_BYTES, "repository receipt"
    )
    protected_payload = read_limited(
        protected_path, MAX_RECEIPT_BYTES, "protected receipt"
    )
    if repository_payload != protected_payload:
        raise RunnerError("repository receipt differs from its protected authority")
    receipt = strict_json_load(protected_path)
    if not isinstance(receipt, dict):
        raise RunnerError("authoritative receipt is not an object")
    required = {
        "standard_version": VERSION,
        "kind": "subagent_guard_receipt",
        "guard_key": guard_key,
        "authorization_id": expected.get("authorization_id"),
        "authorization_sha256": expected.get("authorization_sha256"),
        "policy_sha256": expected.get("policy_sha256"),
        "correlation": expected.get("correlation"),
        "agent_type": expected.get("agent_type"),
        "summary_sha256": expected.get("summary_sha256"),
    }
    mismatches = sorted(
        name for name, value in required.items() if receipt.get(name) != value
    )
    if mismatches:
        raise RunnerError(
            "authoritative receipt does not match the current run: %s"
            % ", ".join(mismatches)
        )
    for name in ("start_snapshot_sha256", "stop_snapshot_sha256"):
        if re.fullmatch(r"[0-9a-f]{64}", str(receipt.get(name) or "")) is None:
            raise RunnerError("authoritative receipt has an invalid %s" % name)
    if receipt.get("outcome") not in {"PASS", "FAIL", "BLOCKED", "NOT_RUN"}:
        raise RunnerError("authoritative receipt outcome is invalid")
    if receipt.get("task_status") not in {"PASS", "FAIL", "BLOCKED", "NOT_RUN"}:
        raise RunnerError("authoritative receipt task_status is invalid")
    if not isinstance(receipt.get("changed"), dict):
        raise RunnerError("authoritative receipt changed map is invalid")
    if not isinstance(receipt.get("guard_errors"), list) or any(
        not isinstance(item, str) for item in receipt.get("guard_errors", [])
    ):
        raise RunnerError("authoritative receipt guard_errors are invalid")
    verification_run_id = receipt.get("verification_run_id")
    try:
        report, _report_path = load_latest_report(repo, verification_run_id)
    except (HarnessError, OSError, ValueError) as exc:
        raise RunnerError("authoritative receipt verification archive is invalid: %s" % exc)
    if report.get("overall_status") != receipt.get("verification_status"):
        raise RunnerError("authoritative receipt verification status does not match its archive")
    return repository_path, receipt, sha256_bytes(repository_payload)


def load_task(value):
    if value == "-":
        raw = sys.stdin.buffer.read(MAX_TASK_BYTES + 1)
    else:
        raw = value.encode("utf-8")
    if not raw or len(raw) > MAX_TASK_BYTES:
        raise RunnerError("task must contain 1 to %d UTF-8 bytes" % MAX_TASK_BYTES)
    try:
        task = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RunnerError("task is not UTF-8: %s" % exc)
    if not task.strip():
        raise RunnerError("task must not be blank")
    return task


def pending_authorization(repo, authorization_id, agent_type):
    directories = _guard_directories(repo, create=False)
    matches = []
    for path in sorted(directories["pending"].glob("*.json")):
        try:
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise RunnerError("pending authorization is not a safe regular file")
            document = strict_json_load(path)
        except (HarnessError, OSError, RunnerError) as exc:
            raise RunnerError("unsafe pending authorization %s: %s" % (path.name, exc))
        if document.get("authorization_id") == authorization_id:
            matches.append((path, document))
    if len(matches) != 1:
        raise RunnerError(
            "expected exactly one pending authorization %s; found %d"
            % (authorization_id, len(matches))
        )
    path, document = matches[0]
    if document.get("expected_agent_type") != agent_type:
        raise RunnerError("pending authorization agent type does not match --agent-type")
    return path, document


def installed_role_instructions(agent_type):
    configured_home = os.environ.get("CODEX_HOME")
    codex_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
    path = Path(os.path.abspath(str(codex_home))) / "agents" / (agent_type + ".toml")
    raw = read_limited(path, 1024 * 1024, "installed agent definition").decode("utf-8")
    name = re.search(r'(?m)^name\s*=\s*"([^"]+)"\s*$', raw)
    instructions = re.search(
        r'(?ms)^developer_instructions\s*=\s*"""(.+?)"""\s*$', raw
    )
    if not name or name.group(1) != agent_type or not instructions:
        raise RunnerError("installed agent definition is incomplete or has the wrong name")
    if re.search(r"(?m)^model(?:_reasoning_effort)?\s*=", raw):
        raise RunnerError("installed agent definition hard-codes model selection")
    return instructions.group(1).strip()


def evidence_paths(repo, authorization):
    expected = set(authorization.get("expected_evidence_ids", []))
    if not expected:
        return []
    document = strict_json_load(repo / ".harness/state/evidence.json")
    rows = document.get("evidence") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise RunnerError("current evidence state is not an evidence envelope")
    by_id = {
        item.get("evidence_id"): item.get("path")
        for item in rows if isinstance(item, dict)
    }
    missing = sorted(item for item in expected if not isinstance(by_id.get(item), str))
    if missing:
        raise RunnerError("expected evidence IDs lack current paths: %s" % ", ".join(missing))
    return [by_id[item] for item in sorted(expected)]


def review_files(repo, authorization, evidence):
    values = []
    for relative in authorization.get("review_paths", []):
        if not isinstance(relative, str) or any(token in relative for token in ("*", "?", "[")):
            continue
        path = repo / relative
        if path.is_file() and not path.is_symlink():
            values.append(relative)
    values.extend(evidence)
    return sorted(set(values))


def handoff_files(repo, authorization, evidence, role_mode):
    """Return exact FILES claims without conflating evidence inputs and writes."""
    if role_mode == "read_only":
        return review_files(repo, authorization, evidence)
    required = authorization.get("required_files", [])
    if not isinstance(required, list) or not required:
        raise RunnerError(
            "write/evidence authorization lacks concrete required output files"
        )
    unsafe = [
        item for item in required
        if not isinstance(item, str)
        or not item
        or "\\" in item
        or Path(item).is_absolute()
        or any(part in ("", ".", "..") for part in Path(item).parts)
        or any(token in item for token in ("*", "?", "["))
    ]
    if unsafe:
        raise RunnerError(
            "write/evidence required output files must be concrete safe paths: %s"
            % ", ".join(str(item) for item in unsafe)
        )
    return sorted(set(required))


def hook_wire(event, repo, correlation, agent_type, final_message=None, transcript_path=None):
    base = {
        "_normalized_event": event.lower(),
        "cwd": str(repo),
        "hook_event_name": event,
        "model": "inherited-user-selection",
        "permission_mode": "default",
        "session_id": correlation[0],
        "transcript_path": transcript_path,
        "turn_id": correlation[1],
    }
    if event == "PreToolUse":
        base.update({
            "tool_input": {"agent_type": agent_type, "runner": "guarded_codex_exec"},
            "tool_name": "spawn_agent",
            "tool_use_id": correlation[3],
        })
    else:
        base.update({"agent_id": correlation[2], "agent_type": agent_type})
    if event == "SubagentStop":
        base.update({
            "agent_transcript_path": transcript_path,
            "last_assistant_message": final_message or "",
            "stop_hook_active": False,
        })
    return base


def summary_is_complete(message):
    if not isinstance(message, str):
        return False
    for label in SUMMARY_LABELS:
        if len(re.findall(r"(?m)^%s:\s*.+$" % re.escape(label), message)) != 1:
            return False
    return True


def fail_closed_summary(reason):
    compact = " ".join(str(reason).split())[:2000] or "guarded Codex process did not complete"
    return "\n".join((
        "TASK_STATUS: BLOCKED",
        "FILES: NONE",
        "EVIDENCE: NONE",
        "FAILURES: NONE",
        "BLOCKERS: %s" % compact,
        "NOT_RUN: authorized specialist task did not complete",
    ))


def locate_session_transcript(session_id):
    if not session_id:
        return None
    configured_home = os.environ.get("CODEX_HOME")
    codex_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
    candidates = list((codex_home / "sessions").glob("**/*%s*.jsonl" % session_id))
    if len(candidates) != 1:
        return None
    path = candidates[0]
    try:
        mode = path.lstat().st_mode
    except OSError:
        return None
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        return None
    return str(path.resolve())


def run_codex(repo, role_mode, prompt, timeout, model=None):
    executable = shutil.which("codex")
    if not executable:
        raise RunnerError("codex executable is unavailable")
    sandbox = "read-only" if role_mode == "read_only" else "workspace-write"
    temporary_root = Path(tempfile.mkdtemp(prefix="harness-robot."))
    os.chmod(str(temporary_root), 0o700)
    stdout_path = temporary_root / "stdout.txt"
    stderr_path = temporary_root / "stderr.txt"
    final_path = temporary_root / "final-message.txt"
    command = [
        executable, "exec", "--disable", "hooks", "--disable", "multi_agent",
        "--sandbox", sandbox, "--color", "never", "-C", str(repo),
        "--output-last-message", str(final_path),
    ]
    if model:
        command.extend(("--model", model))
    command.append("-")
    started = utc_now()
    timed_out = False
    exit_code = None
    try:
        with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
            os.chmod(str(stdout_path), 0o600)
            os.chmod(str(stderr_path), 0o600)
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(repo),
                    input=prompt.encode("utf-8"),
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    timeout=timeout,
                    check=False,
                    env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                )
                exit_code = completed.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
    finally:
        completed_at = utc_now()
    stdout = read_limited(stdout_path, MAX_CAPTURE_BYTES, "Codex stdout")
    stderr = read_limited(stderr_path, MAX_CAPTURE_BYTES, "Codex stderr")
    final = b""
    if final_path.exists():
        final = read_limited(final_path, MAX_FINAL_MESSAGE_BYTES, "Codex final message")
    try:
        final_text = final.decode("utf-8")
    except UnicodeDecodeError:
        final_text = ""
    combined = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
    match = re.search(r"(?mi)^session id:\s*([0-9a-f-]{36})\s*$", combined)
    session_id = match.group(1) if match else None
    transcript = locate_session_transcript(session_id)
    transcript_bytes = 0
    transcript_sha256 = None
    if transcript:
        transcript_payload = read_limited(
            transcript, 128 * 1024 * 1024, "Codex session transcript"
        )
        transcript_bytes = len(transcript_payload)
        transcript_sha256 = sha256_bytes(transcript_payload)
    return {
        "argv": command,
        "started_at": format_utc(started),
        "completed_at": format_utc(completed_at),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout_bytes": len(stdout),
        "stdout_sha256": sha256_bytes(stdout),
        "stderr_bytes": len(stderr),
        "stderr_sha256": sha256_bytes(stderr),
        "final_message": final_text,
        "final_message_sha256": sha256_bytes(final),
        "session_id": session_id,
        "transcript_path": transcript,
        "transcript_bytes": transcript_bytes,
        "transcript_sha256": transcript_sha256,
        "temporary_root": str(temporary_root),
    }


def write_result(repo, result):
    run_id = "ROBOT-%s-%s" % (
        utc_now().strftime("%Y%m%dT%H%M%S%fZ"), uuid.uuid4().hex[:10]
    )
    run_dir = repo / ".harness/runs" / run_id
    run_dir.mkdir(mode=0o700)
    result_path = run_dir / "robot-run.json"
    public = dict(result)
    public["run_id"] = run_id
    public["result_path"] = result_path.relative_to(repo).as_posix()
    atomic_write_json(result_path, public)
    latest = {
        "standard_version": VERSION,
        "kind": "latest_guarded_robot_run",
        "run_id": run_id,
        "result_path": public["result_path"],
        "status": public["status"],
        "authorization_id": public["authorization_id"],
        "receipt_path": public.get("receipt_path"),
    }
    atomic_write_json(repo / ".harness/state/latest-robot-run.json", latest)
    return public


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--agent-type", required=True)
    parser.add_argument("--task", default="-", help="task text, or - to read UTF-8 task from stdin")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--model", help="optional current user-selected model; omit to inherit Codex config")
    return parser


def run(args):
    repo = find_repo(args.repo)
    if not SAFE_ID_RE.fullmatch(args.authorization_id):
        raise RunnerError("authorization ID is unsafe")
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", args.agent_type):
        raise RunnerError("agent type is unsafe")
    if args.timeout_seconds < 30 or args.timeout_seconds > 7200:
        raise RunnerError("timeout must be between 30 and 7200 seconds")
    task = load_task(args.task)
    _pending_path, authorization = pending_authorization(
        repo, args.authorization_id, args.agent_type
    )
    policy = _load_subagent_policy(repo)
    role = policy.get("roles", {}).get(args.agent_type)
    if not isinstance(role, dict):
        raise RunnerError("agent type is absent from the canonical role policy")
    instructions = installed_role_instructions(args.agent_type)
    evidence = evidence_paths(repo, authorization)
    files = handoff_files(repo, authorization, evidence, role.get("mode"))
    repository_receipts = _guard_directories(repo, create=False)["receipts"]
    protected_receipts = _guard_external_directories(repo, create=False)["receipts"]
    before_repository_receipts = receipt_directory_snapshot(repository_receipts)
    before_protected_receipts = receipt_directory_snapshot(
        protected_receipts, protected=True
    )
    validate_receipt_history_pair(
        before_repository_receipts, before_protected_receipts
    )
    correlation = (
        "RUNNER-SESSION-%s" % uuid.uuid4().hex,
        "RUNNER-TURN-%s" % uuid.uuid4().hex,
        "RUNNER-AGENT-%s" % uuid.uuid4().hex,
        "RUNNER-TOOL-%s" % uuid.uuid4().hex,
    )
    verification = safe_verify(repo, strict=True)
    pretool, pretool_exit = hook_response(
        hook_wire("PreToolUse", repo, correlation, args.agent_type), verification
    )
    output = pretool.get("hookSpecificOutput", {}) if isinstance(pretool, dict) else {}
    if pretool_exit != 0 or output.get("permissionDecision") != "allow":
        raise RunnerError("authorization reservation was denied: %s" % json.dumps(pretool))
    if args.authorization_id not in str(output.get("additionalContext", "")):
        raise RunnerError("reserved authorization does not match the requested ID")
    start_wire = hook_wire("SubagentStart", repo, correlation, args.agent_type)
    start, start_exit = hook_response(start_wire, verification)
    if start_exit != 0 or (isinstance(start, dict) and start.get("continue") is False):
        raise RunnerError("guarded specialist start was denied: %s" % json.dumps(start))
    guard_key = _guard_key(start_wire, repo)
    pass_template = "\n".join((
        "TASK_STATUS: PASS",
        "FILES: %s" % (",".join(files) if files else "NONE"),
        "EVIDENCE: %s" % (",".join(evidence) if evidence else "NONE"),
        "FAILURES: NONE", "BLOCKERS: NONE", "NOT_RUN: NONE",
    ))
    prompt = "\n\n".join((
        "You are the installed specialist role %s." % args.agent_type,
        instructions,
        "Authorized task:\n%s" % task,
        "Make no subagent calls. Do not change acceptance criteria. Stay inside the authorized paths and operations embedded below.",
        "Authorization (machine-issued):\n%s" % json.dumps(authorization, ensure_ascii=False, sort_keys=True),
        "If and only if the authorized task fully passes, end with exactly these six final lines:\n%s" % pass_template,
        "Otherwise end with the same six labels and the stricter truthful FAIL, BLOCKED, or NOT_RUN details. Add no text after NOT_RUN.",
    ))
    process = None
    runner_error = None
    try:
        process = run_codex(
            repo, role.get("mode"), prompt, args.timeout_seconds, model=args.model
        )
    except (OSError, RunnerError, subprocess.SubprocessError) as exc:
        runner_error = "%s: %s" % (type(exc).__name__, exc)
    if process is None:
        final_message = fail_closed_summary(runner_error)
        process_public = {
            "argv": [], "started_at": None, "completed_at": format_utc(utc_now()),
            "exit_code": None, "timed_out": False, "stdout_bytes": 0,
            "stdout_sha256": sha256_bytes(b""), "stderr_bytes": 0,
            "stderr_sha256": sha256_bytes(b""), "final_message_sha256": sha256_bytes(b""),
            "session_id": None, "transcript_path": None,
        }
    else:
        process_public = {key: value for key, value in process.items() if key not in {"final_message", "temporary_root"}}
        if process["timed_out"]:
            final_message = fail_closed_summary("Codex specialist process timed out")
        elif process["exit_code"] != 0:
            final_message = fail_closed_summary(
                "Codex specialist process exited %s" % process["exit_code"]
            )
        elif not summary_is_complete(process["final_message"]):
            final_message = fail_closed_summary("Codex specialist omitted the required six-field summary")
        else:
            final_message = process["final_message"]
        shutil.rmtree(process["temporary_root"], ignore_errors=True)
    verification = safe_verify(repo, strict=True)
    stop_wire = hook_wire(
        "SubagentStop", repo, correlation, args.agent_type,
        final_message=final_message,
        transcript_path=process_public.get("transcript_path"),
    )
    responses = []
    finalized = False
    for attempt in range(3):
        stop_wire["stop_hook_active"] = attempt > 0
        response, _exit = hook_response(stop_wire, verification)
        responses.append(response)
        after_protected_receipts = receipt_directory_snapshot(
            protected_receipts, protected=True
        )
        if set(after_protected_receipts) - set(before_protected_receipts):
            finalized = True
            break
        if attempt < 2:
            verification = safe_verify(repo, strict=True)
    if not finalized:
        recovery_message = fail_closed_summary(
            "guard did not finalize a receipt after bounded stop attempts"
        )
        final_message = recovery_message
        recovery_wire = dict(stop_wire)
        recovery_wire["last_assistant_message"] = recovery_message
        recovery_wire["stop_hook_active"] = True
        recovery_guard = _evaluate_subagent_stop(recovery_wire, verification)
        recovery_receipt = _finalize_subagent_guard(
            recovery_wire, verification, recovery_guard, "BLOCKED"
        )
        if not recovery_receipt:
            raise RunnerError(
                "guard did not finalize a receipt after bounded stop attempts"
            )
        responses.append({
            "continue": False,
            "stopReason": (
                "The guarded runner closed the stale lifecycle with a truthful "
                "BLOCKED receipt after bounded stop attempts."
            ),
        })
    expected_receipt = {
        "guard_key": guard_key,
        "authorization_id": args.authorization_id,
        "authorization_sha256": canonical_sha256(authorization),
        "policy_sha256": canonical_sha256(policy),
        "correlation": [
            str(repo.resolve()), correlation[0], correlation[1], correlation[2]
        ],
        "agent_type": args.agent_type,
        "summary_sha256": sha256_bytes(final_message.encode("utf-8")),
    }
    receipt_error = None
    receipt_path = None
    receipt = None
    receipt_sha256 = None
    try:
        receipt_path, receipt, receipt_sha256 = select_authoritative_receipt(
            repo,
            repository_receipts,
            protected_receipts,
            before_repository_receipts,
            before_protected_receipts,
            expected_receipt,
        )
    except (HarnessError, RunnerError, OSError, ValueError) as exc:
        receipt_error = "%s: %s" % (type(exc).__name__, exc)
    status = "PASS" if (
        receipt_error is None
        and isinstance(receipt, dict)
        and receipt.get("outcome") == "PASS"
        and receipt.get("task_status") == "PASS"
        and process_public.get("exit_code") == 0
        and process_public.get("session_id")
        and process_public.get("transcript_path")
    ) else "BLOCKED"
    result = {
        "standard_version": VERSION,
        "kind": "guarded_codex_subagent_run",
        "status": status,
        "authorization_id": args.authorization_id,
        "agent_type": args.agent_type,
        "guard_correlation": [str(repo.resolve()), correlation[0], correlation[1], correlation[2]],
        "process": process_public,
        "receipt_path": receipt_path.relative_to(repo).as_posix() if receipt_path else None,
        "receipt_sha256": receipt_sha256,
        "receipt_outcome": receipt.get("outcome") if isinstance(receipt, dict) else None,
        "receipt_task_status": receipt.get("task_status") if isinstance(receipt, dict) else None,
        "verification_run_id": receipt.get("verification_run_id") if isinstance(receipt, dict) else None,
        "stop_responses": responses,
        "runner_error": receipt_error or runner_error,
        "completed_at": format_utc(utc_now()),
    }
    return write_result(repo, result)


def main(argv=None):
    try:
        result = run(build_parser().parse_args(argv))
    except (HarnessError, RunnerError, OSError, ValueError, subprocess.SubprocessError) as exc:
        result = {
            "standard_version": VERSION,
            "kind": "guarded_codex_subagent_run",
            "status": "BLOCKED",
            "error": "%s: %s" % (type(exc).__name__, exc),
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
