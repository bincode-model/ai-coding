#!/usr/bin/env python3
"""Create one fail-closed pending authorization for the next Harness subagent."""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional
import uuid


VERSION = "1.0.0"
AUTH_ID_RE = re.compile(r"^[A-Z][A-Z0-9_.-]{2,127}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
OPERATIONS = ("add", "modify", "delete")


class AuthorizationError(Exception):
    """Expected fail-closed authorization error."""


class JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise AuthorizationError(message)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def format_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: Any, field: str) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AuthorizationError("%s must be a UTC RFC 3339 string ending in Z" % field)
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise AuthorizationError("%s is invalid: %s" % (field, exc))
    if parsed.tzinfo is None:
        raise AuthorizationError("%s must include a timezone" % field)
    return parsed.astimezone(dt.timezone.utc)


def no_duplicates(pairs: List[Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AuthorizationError("duplicate JSON key: %s" % key)
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise AuthorizationError("non-finite JSON number is forbidden: %s" % value)


def strict_json_load(path: Path) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AuthorizationError("cannot read %s: %s" % (path, exc))
    if len(raw) > 4 * 1024 * 1024:
        raise AuthorizationError("JSON file exceeds 4 MiB: %s" % path)
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=no_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError, AuthorizationError) as exc:
        raise AuthorizationError("invalid strict JSON in %s: %s" % (path, exc))


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AuthorizationError("value is not canonical JSON: %s" % exc)


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def canonical_subagent_role_policy() -> Dict[str, Any]:
    protected = [
        ".codex/**",
        ".harness/config.json",
        ".harness/schemas/**",
        ".harness/spec/requirements.json",
        ".harness/spec/change-ledger.json",
        ".harness/spec/test-cases.json",
        ".harness/spec/traceability.json",
        ".harness/spec/acceptance-results.json",
        ".harness/state/requirements-baseline.json",
        ".harness/state/subagent-guard/**",
        "AGENTS.md",
        "scripts/_harness_core.py",
        "scripts/harness_authorize_subagent.py",
        "scripts/harness_run_subagent.py",
        "scripts/harness_init.py",
        "scripts/harness_selftest.py",
        "scripts/harness_validate.py",
        "scripts/harness_verify.py",
    ]
    roles: Dict[str, Any] = {}
    for name in (
        "architecture_auditor", "requirements_auditor", "security_reviewer",
        "release_judge",
    ):
        roles[name] = {
            "mode": "read_only", "allow_paths": [], "deny_paths": ["**"],
            "allowed_operations": [], "acceptance_changes": "forbidden",
        }
    for name in (
        "frontend_worker", "backend_worker", "database_worker",
        "file_pipeline_worker", "test_engineer",
    ):
        roles[name] = {
            "mode": "write", "allow_paths": ["**"], "deny_paths": protected,
            "allowed_operations": ["add", "modify", "delete"],
            "acceptance_changes": "explicit_external_grant_only",
        }
    roles["runtime_acceptance"] = {
        "mode": "evidence_only", "allow_paths": [".harness/runs/**"],
        "deny_paths": protected,
        "allowed_operations": ["add", "modify"],
        "acceptance_changes": "forbidden",
    }
    return {
        "standard_version": VERSION,
        "policy_name": "software-delivery-harness-subagent-roles",
        "unknown_agent_type": "BLOCKED",
        "concurrency": "exclusive_repo_lease",
        "snapshot_limits": {
            "max_files": 50000, "max_total_bytes": 536870912,
            "max_file_bytes": 67108864,
        },
        "protected_acceptance_paths": protected,
        "roles": roles,
    }


def normalized_pattern(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise AuthorizationError("%s must be a non-empty repository-relative POSIX pattern" % field)
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise AuthorizationError("%s must not be absolute or contain dot traversal: %s" % (field, value))
    if path.parts and len(path.parts[0]) == 2 and path.parts[0][1] == ":":
        raise AuthorizationError("%s must not use a drive prefix: %s" % (field, value))
    return path.as_posix()


def guard_glob_match(path: str, pattern: str) -> bool:
    path_parts = normalized_pattern(path, "path").split("/")
    pattern_parts = normalized_pattern(pattern, "pattern").split("/")

    def match(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        token = pattern_parts[pattern_index]
        if token == "**":
            return match(path_index, pattern_index + 1) or (
                path_index < len(path_parts) and match(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[path_index], token)
            and match(path_index + 1, pattern_index + 1)
        )

    return match(0, 0)


def validate_required_files(role_mode: str, required_files: List[str]) -> List[str]:
    if role_mode == "read_only":
        if required_files:
            raise AuthorizationError("read-only roles cannot require changed files")
        return required_files
    if not required_files:
        raise AuthorizationError(
            "write/evidence roles require every intended changed path as a concrete --required-file"
        )
    wildcard_required = [
        item for item in required_files if any(token in item for token in ("*", "?", "["))
    ]
    if wildcard_required:
        raise AuthorizationError(
            "write/evidence --required-file values must be concrete paths: %s"
            % ", ".join(wildcard_required)
        )
    return required_files


def ensure_safe_tree(repo: Path, relative: str, create: bool = False) -> Path:
    lexical = PurePosixPath(relative)
    if lexical.is_absolute() or not lexical.parts or any(part in ("", ".", "..") for part in lexical.parts):
        raise AuthorizationError("unsafe managed directory: %s" % relative)
    current = repo
    for part in lexical.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if not create:
                raise AuthorizationError("required directory is missing: %s" % current)
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            mode = current.lstat().st_mode
        if not os.path.isdir(str(current)) or os.path.islink(str(current)):
            raise AuthorizationError("managed directory is not a real directory: %s" % current)
        if not (mode & 0o400):
            raise AuthorizationError("managed directory is not readable: %s" % current)
    try:
        current.resolve().relative_to(repo)
    except ValueError:
        raise AuthorizationError("managed directory escapes repository: %s" % current)
    return current


def resolve_repo(value: str) -> Path:
    lexical = Path(os.path.abspath(os.path.expanduser(value)))
    if not lexical.exists() or not lexical.is_dir():
        raise AuthorizationError("repository is not a directory: %s" % lexical)
    if lexical.is_symlink():
        raise AuthorizationError("repository path must not be a symbolic link: %s" % lexical)
    repo = lexical.resolve()
    git = shutil.which("git")
    if not git:
        raise AuthorizationError("git executable is unavailable")
    top = subprocess.run(
        [git, "-C", str(repo), "rev-parse", "--show-toplevel"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )
    if top.returncode != 0 or not top.stdout.strip():
        raise AuthorizationError("Git repository is required: %s" % (top.stderr.strip() or "rev-parse failed"))
    if Path(top.stdout.strip()).resolve() != repo:
        raise AuthorizationError("--repo must name the Git top level")
    head = subprocess.run(
        [git, "-C", str(repo), "rev-parse", "HEAD^{commit}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )
    if head.returncode != 0 or re.fullmatch(r"[0-9a-fA-F]{40,64}", head.stdout.strip()) is None:
        raise AuthorizationError("repository must have a resolvable committed HEAD")
    return repo


def validate_policy(repo: Path, agent_type: str) -> Any:
    policy_dir = ensure_safe_tree(repo, ".harness/policy", create=False)
    policy_path = policy_dir / "subagent-roles.json"
    if policy_path.is_symlink() or not policy_path.is_file():
        raise AuthorizationError("canonical subagent role policy is missing or unsafe")
    policy = strict_json_load(policy_path)
    if canonical_bytes(policy) != canonical_bytes(canonical_subagent_role_policy()):
        raise AuthorizationError("project subagent role policy differs from the trusted canonical policy")
    roles = policy.get("roles")
    if not isinstance(roles, dict) or agent_type not in roles or not isinstance(roles[agent_type], dict):
        raise AuthorizationError("unknown agent type: %s" % agent_type)
    role = roles[agent_type]
    if role.get("mode") not in ("read_only", "write", "evidence_only"):
        raise AuthorizationError("role mode is invalid for %s" % agent_type)
    for field in ("allow_paths", "deny_paths", "allowed_operations"):
        values = role.get(field)
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise AuthorizationError("role %s.%s must be an array of strings" % (agent_type, field))
    return policy, role, policy_path


def approved_changes(repo: Path, change_ids: List[str], issuer: str) -> List[Dict[str, Any]]:
    if not change_ids:
        return []
    ledger_path = repo / ".harness" / "spec" / "change-ledger.json"
    if ledger_path.is_symlink() or not ledger_path.is_file():
        raise AuthorizationError("change ledger is missing or unsafe")
    document = strict_json_load(ledger_path)
    rows = document.get("change_ledger") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise AuthorizationError("change ledger root is invalid")
    by_id = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("change_id"), str):
            raise AuthorizationError("change ledger contains an invalid row")
        if row["change_id"] in by_id:
            raise AuthorizationError("duplicate change_id in ledger: %s" % row["change_id"])
        by_id[row["change_id"]] = row
    selected = []
    for change_id in change_ids:
        row = by_id.get(change_id)
        if row is None:
            raise AuthorizationError("acceptance change ID is absent from the ledger: %s" % change_id)
        approved_by = row.get("approved_by")
        if not isinstance(approved_by, str) or not approved_by.strip():
            raise AuthorizationError("acceptance change lacks independent approval: %s" % change_id)
        if approved_by.strip() == issuer.strip():
            raise AuthorizationError("acceptance change approver must differ from authorization issuer: %s" % change_id)
        if not isinstance(row.get("requirement_ids"), list) or not row["requirement_ids"]:
            raise AuthorizationError("acceptance change lacks requirement_ids: %s" % change_id)
        timestamp = parse_utc(row.get("timestamp"), "change_ledger.timestamp")
        if timestamp > utc_now() + dt.timedelta(minutes=5):
            raise AuthorizationError("acceptance change timestamp is in the future: %s" % change_id)
        selected.append(row)
    return selected


def scan_pending(pending_dir: Path, repo: Path, now: dt.datetime) -> None:
    for path in sorted(pending_dir.iterdir()):
        if path.name.startswith("."):
            continue
        if path.is_symlink() or not path.is_file() or path.suffix != ".json":
            raise AuthorizationError("pending authorization directory contains an unsafe entry: %s" % path)
        document = strict_json_load(path)
        if not isinstance(document, dict):
            raise AuthorizationError("pending authorization is not an object: %s" % path)
        if document.get("state") != "PENDING":
            raise AuthorizationError("pending authorization has invalid state: %s" % path)
        if Path(str(document.get("repo_realpath", ""))).resolve() != repo:
            continue
        expires = parse_utc(document.get("expires_at"), "expires_at")
        if expires > now:
            raise AuthorizationError(
                "an unexpired pending authorization already exists for this repository: %s" % path.name
            )


def scan_prestart(guard_dir: Path, prestart_dir: Path, repo: Path, now: dt.datetime) -> None:
    lease_path = guard_dir / "prestart-lease.json"
    entries = [path for path in sorted(prestart_dir.iterdir()) if not path.name.startswith(".")]
    if not lease_path.exists():
        if entries:
            raise AuthorizationError("prestart directory contains an orphan reservation: %s" % entries[0].name)
        return
    if lease_path.is_symlink() or not lease_path.is_file():
        raise AuthorizationError("pre-spawn reservation lease is unsafe")
    lease = strict_json_load(lease_path)
    required = {
        "standard_version", "kind", "state", "repo_realpath", "session_id",
        "turn_id", "tool_use_id", "agent_type", "authorization_file",
        "authorization_sha256", "role_policy_sha256", "created_at", "expires_at",
        "integrity_sha256",
    }
    if not isinstance(lease, dict) or set(lease) != required:
        raise AuthorizationError("pre-spawn reservation lease fields are invalid")
    integrity = dict(lease)
    observed_integrity = integrity.pop("integrity_sha256", None)
    if (
        lease.get("standard_version") != VERSION
        or lease.get("kind") != "subagent_prestart_lease"
        or lease.get("state") != "RESERVED"
        or observed_integrity != canonical_sha256(integrity)
        or Path(str(lease.get("repo_realpath", ""))).resolve() != repo
    ):
        raise AuthorizationError("pre-spawn reservation lease integrity is invalid")
    filename = lease.get("authorization_file")
    if not isinstance(filename, str) or re.fullmatch(r"[a-f0-9]{64}\.json", filename) is None:
        raise AuthorizationError("pre-spawn authorization filename is unsafe")
    if len(entries) != 1 or entries[0].name != filename:
        raise AuthorizationError("prestart directory does not exactly match its reservation lease")
    reservation = entries[0]
    if reservation.is_symlink() or not reservation.is_file():
        raise AuthorizationError("pre-spawn authorization reservation is unsafe")
    authorization = strict_json_load(reservation)
    if (
        not isinstance(authorization, dict)
        or authorization.get("state") != "PENDING"
        or Path(str(authorization.get("repo_realpath", ""))).resolve() != repo
        or canonical_sha256(authorization) != lease.get("authorization_sha256")
    ):
        raise AuthorizationError("pre-spawn authorization reservation integrity is invalid")
    expires = parse_utc(lease.get("expires_at"), "prestart expires_at")
    if expires > now:
        raise AuthorizationError(
            "an unexpired Agent tool reservation already exists for this repository: %s" % filename
        )
    reservation.unlink()
    lease_path.unlink()


def atomic_create_json(directory: Path, path: Path, document: Dict[str, Any]) -> None:
    if path.parent != directory or path.exists() or path.is_symlink():
        raise AuthorizationError("authorization target already exists or is unsafe: %s" % path)
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".authorize.", dir=str(directory))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, str(path), follow_symlinks=False)
        except FileExistsError:
            raise AuthorizationError("authorization target appeared concurrently: %s" % path)
        os.unlink(temporary)
        temporary = ""
        try:
            dir_fd = os.open(str(directory), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except BaseException:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = JSONArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="Git repository root (default: current directory)")
    parser.add_argument("--agent-type", required=True, help="installed Harness agent type")
    parser.add_argument("--allow-path", action="append", default=[], help="allowed write path/pattern; repeatable")
    parser.add_argument("--review-path", action="append", default=[], help="allowed reviewed path/pattern; repeatable")
    parser.add_argument("--allow-operation", action="append", choices=OPERATIONS, default=[], help="allowed operation; repeatable")
    parser.add_argument("--required-file", action="append", default=[], help="path that the task must change; repeatable")
    parser.add_argument("--requirement-id", action="append", default=[], help="expected requirement ID; repeatable")
    parser.add_argument("--test-case-id", action="append", default=[], help="expected test case ID; repeatable")
    parser.add_argument("--evidence-id", action="append", default=[], help="expected evidence ID; repeatable")
    parser.add_argument("--acceptance-change-id", action="append", default=[], help="pre-approved change-ledger ID; repeatable")
    parser.add_argument("--run-id", help="authorized evidence run ID for runtime_acceptance")
    parser.add_argument("--expected-session-id")
    parser.add_argument("--expected-turn-id")
    parser.add_argument("--expected-worktree")
    parser.add_argument("--expires-in-seconds", type=int, default=600)
    parser.add_argument("--authorization-id")
    parser.add_argument("--issuer", default="parent-orchestrator")
    return parser


def authorize(args: argparse.Namespace) -> Dict[str, Any]:
    repo = resolve_repo(args.repo)
    if not isinstance(args.agent_type, str) or not SAFE_ID_RE.fullmatch(args.agent_type):
        raise AuthorizationError("--agent-type must be a safe ASCII identifier")
    if not isinstance(args.issuer, str) or not args.issuer.strip() or len(args.issuer) > 256:
        raise AuthorizationError("--issuer must be a non-empty string of at most 256 characters")
    if args.expires_in_seconds < 30 or args.expires_in_seconds > 3600:
        raise AuthorizationError("--expires-in-seconds must be between 30 and 3600")
    policy, role, policy_path = validate_policy(repo, args.agent_type)
    role_mode = role["mode"]
    allowed_paths = sorted(set(normalized_pattern(item, "--allow-path") for item in args.allow_path))
    review_paths = sorted(set(normalized_pattern(item, "--review-path") for item in args.review_path))
    required_files = sorted(set(normalized_pattern(item, "--required-file") for item in args.required_file))
    validate_required_files(role_mode, required_files)
    if role_mode == "read_only":
        if allowed_paths or args.allow_operation:
            raise AuthorizationError("read-only roles cannot receive write paths or operations")
        if not review_paths:
            raise AuthorizationError("read-only roles require at least one --review-path")
        operations: List[str] = []
    else:
        if not allowed_paths:
            raise AuthorizationError("write/evidence roles require at least one --allow-path")
        outside_grant = [
            item for item in required_files
            if not any(guard_glob_match(item, pattern) for pattern in allowed_paths)
        ]
        if outside_grant:
            raise AuthorizationError(
                "required files fall outside --allow-path: %s"
                % ", ".join(outside_grant)
            )
        operations = sorted(set(args.allow_operation or role.get("allowed_operations", [])))
        if not operations or any(item not in OPERATIONS for item in operations):
            raise AuthorizationError("authorization has no valid write operations")
        if not set(operations).issubset(set(role.get("allowed_operations", []))):
            raise AuthorizationError(
                "requested operations exceed the static role: %s"
                % ", ".join(sorted(set(operations) - set(role.get("allowed_operations", []))))
            )
    run_prefix: Optional[str] = None
    if args.run_id is not None:
        if not SAFE_ID_RE.fullmatch(args.run_id):
            raise AuthorizationError("--run-id must be a safe ASCII identifier")
        run_prefix = ".harness/runs/%s" % args.run_id
    if role_mode == "evidence_only":
        if run_prefix is None:
            raise AuthorizationError("evidence-only roles require --run-id")
        if any(not (item == run_prefix or item.startswith(run_prefix + "/")) for item in allowed_paths):
            raise AuthorizationError("evidence-only allowed paths must stay within the authorized run")
    elif run_prefix is not None:
        raise AuthorizationError("--run-id is only valid for evidence-only roles")
    id_fields = {
        "--requirement-id": args.requirement_id,
        "--test-case-id": args.test_case_id,
        "--evidence-id": args.evidence_id,
        "--acceptance-change-id": args.acceptance_change_id,
    }
    for label, values in id_fields.items():
        if any(not isinstance(value, str) or AUTH_ID_RE.fullmatch(value) is None for value in values):
            raise AuthorizationError("%s values must use Harness uppercase IDs" % label)
    if not any((args.requirement_id, args.test_case_id, args.evidence_id)):
        raise AuthorizationError(
            "authorization must name at least one --requirement-id, --test-case-id, or --evidence-id"
        )
    changes = approved_changes(repo, sorted(set(args.acceptance_change_id)), args.issuer)
    expected_worktree = None
    if args.expected_worktree:
        expected_worktree = resolve_repo(args.expected_worktree)
        if expected_worktree != repo:
            raise AuthorizationError("this helper currently authorizes only the selected repository worktree")
    for label, value in (("--expected-session-id", args.expected_session_id), ("--expected-turn-id", args.expected_turn_id)):
        if value is not None and (not SAFE_ID_RE.fullmatch(value)):
            raise AuthorizationError("%s must be a safe ASCII identifier" % label)
    authorization_id = args.authorization_id or (
        "AUTH-%s-%s" % (utc_now().strftime("%Y%m%dT%H%M%SZ"), uuid.uuid4().hex[:12].upper())
    )
    if AUTH_ID_RE.fullmatch(authorization_id) is None:
        raise AuthorizationError("authorization ID must match %s" % AUTH_ID_RE.pattern)
    created = utc_now()
    expires = created + dt.timedelta(seconds=args.expires_in_seconds)
    document: Dict[str, Any] = {
        "schema_version": VERSION,
        "authorization_id": authorization_id,
        "state": "PENDING",
        "repo_realpath": str(repo),
        "created_at": format_utc(created),
        "expires_at": format_utc(expires),
        "expected_agent_type": args.agent_type,
        "expected_session_id": args.expected_session_id,
        "expected_turn_id": args.expected_turn_id,
        "expected_worktree_realpath": str(expected_worktree) if expected_worktree else None,
        "role_policy_sha256": canonical_sha256(policy),
        "allowed_paths": allowed_paths,
        "allowed_operations": operations,
        "review_paths": review_paths,
        "required_files": required_files,
        "expected_requirement_ids": sorted(set(args.requirement_id)),
        "expected_test_case_ids": sorted(set(args.test_case_id)),
        "acceptance_change_ids": sorted(set(args.acceptance_change_id)),
        "approved_change_entries": changes,
        "expected_evidence_ids": sorted(set(args.evidence_id)),
        "evidence_run_prefix": run_prefix,
        "exclusive_repo_lease": True,
        "issuer": args.issuer.strip(),
        "git_head": subprocess.check_output(
            [shutil.which("git") or "git", "-C", str(repo), "rev-parse", "HEAD^{commit}"],
            text=True,
        ).strip().lower(),
    }
    guard_dir = ensure_safe_tree(repo, ".harness/state/subagent-guard", create=False)
    pending_dir = ensure_safe_tree(repo, ".harness/state/subagent-guard/pending", create=False)
    prestart_dir = ensure_safe_tree(repo, ".harness/state/subagent-guard/prestart", create=False)
    ensure_safe_tree(repo, ".harness/state/subagent-guard/active", create=False)
    ensure_safe_tree(repo, ".harness/state/subagent-guard/receipts", create=False)
    lock_path = guard_dir / ".authorize.lock"
    try:
        lock_path.mkdir(mode=0o700)
    except FileExistsError:
        raise AuthorizationError("another authorization operation is active or left an unresolved lock")
    try:
        scan_pending(pending_dir, repo, created)
        scan_prestart(guard_dir, prestart_dir, repo, created)
        target = pending_dir / (hashlib.sha256(authorization_id.encode("utf-8")).hexdigest() + ".json")
        atomic_create_json(pending_dir, target, document)
    finally:
        try:
            lock_path.rmdir()
        except OSError:
            pass
    return {
        "standard_version": VERSION,
        "operation": "authorize_subagent",
        "status": "PASS",
        "repo": str(repo),
        "authorization_id": authorization_id,
        "authorization_path": target.relative_to(repo).as_posix(),
        "authorization_sha256": canonical_sha256(document),
        "policy_path": policy_path.relative_to(repo).as_posix(),
        "policy_sha256": canonical_sha256(policy),
        "expires_at": document["expires_at"],
        "agent_type": args.agent_type,
        "next_action": "run exactly one matching specialist through harness_run_subagent.py; the launcher will atomically claim this authorization",
    }


def main(argv: Optional[List[str]] = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        result = authorize(args)
    except (AuthorizationError, OSError, subprocess.SubprocessError, ValueError) as exc:
        result = {
            "standard_version": VERSION,
            "operation": "authorize_subagent",
            "status": "BLOCKED",
            "errors": ["%s: %s" % (type(exc).__name__, exc)],
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
