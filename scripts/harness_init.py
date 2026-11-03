#!/usr/bin/env python3
"""Safely initialize Software Delivery Harness 1.0.0 in a repository."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import platform
import queue
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 is reported honestly.
    tomllib = None


VERSION = "1.0.0"
SKILL_ROOT = Path(__file__).resolve().parent.parent
CANONICAL_SKILL_ROOT = Path.home() / ".agents" / "skills" / "software-delivery-harness"
START_MARKER = "<!-- software-delivery-harness:start -->"
END_MARKER = "<!-- software-delivery-harness:end -->"
WORKFLOW_MARKER = "# software-delivery-harness managed workflow v1.0.0"
GITIGNORE_START_MARKER = "# software-delivery-harness:runtime-start"
GITIGNORE_END_MARKER = "# software-delivery-harness:runtime-end"
RUNTIME_GITIGNORE_LINES = (
    ".harness/backups/",
    ".harness/release-report.json",
    ".harness/runs/*",
    "!.harness/runs/.gitkeep",
    ".harness/state/collection-manifest.json",
    ".harness/state/hook-state.json",
    ".harness/state/latest-run.json",
    ".harness/state/latest-robot-run.json",
    ".harness/state/test-executions.json",
    ".harness/state/evidence.json",
    ".harness/state/acceptance-results.json",
    ".harness/state/traceability.json",
    ".harness/state/subagent-guard/lease.json",
    ".harness/state/subagent-guard/prestart-lease.json",
    ".harness/state/subagent-guard/pending/*",
    "!.harness/state/subagent-guard/pending/.gitkeep",
    ".harness/state/subagent-guard/prestart/*",
    "!.harness/state/subagent-guard/prestart/.gitkeep",
    ".harness/state/subagent-guard/active/*",
    "!.harness/state/subagent-guard/active/.gitkeep",
    ".harness/state/subagent-guard/receipts/*",
    "!.harness/state/subagent-guard/receipts/.gitkeep",
)
PRETOOL_AGENT_MATCHER = (
    r"^(Agent|spawn_agent|collaborationspawn_agent|collaboration\.spawn_agent)$"
)
REQUIRED_SCHEMA_FILES = (
    "requirement.schema.json",
    "change-ledger.schema.json",
    "test-case.schema.json",
    "test-execution.schema.json",
    "evidence.schema.json",
    "traceability.schema.json",
    "acceptance-result.schema.json",
    "release-report.schema.json",
    "software-delivery-harness-installation-report.schema.json",
)
REQUIRED_RUNTIME_SCRIPTS = (
    "_harness_core.py",
    "harness_authorize_subagent.py",
    "harness_run_subagent.py",
    "harness_init.py",
    "harness_validate.py",
    "harness_verify.py",
    "harness_report.py",
    "harness_selftest.py",
    "harness_install_report.py",
)


def canonical_subagent_role_policy() -> dict[str, Any]:
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
    roles: dict[str, Any] = {}
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
        "snapshot_limits": {"max_files": 50000, "max_total_bytes": 536870912, "max_file_bytes": 67108864},
        "protected_acceptance_paths": protected,
        "roles": roles,
    }


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def stamp() -> str:
    return dt.datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")


def strict_json_load(path: Path) -> Any:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r} in {path}")
            result[key] = value
        return result

    def no_constant(value: str) -> Any:
        raise ValueError(f"non-finite JSON number {value!r} in {path}")

    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=no_duplicates,
        parse_constant=no_constant,
    )


def conservative_toml_tables(text: str) -> dict[str, set[str]]:
    """Parse the small, unambiguous TOML subset that this installer may edit.

    Python 3.9 has no stdlib TOML reader.  In that environment we deliberately
    reject quoted/dotted keys, array tables, multiline values, duplicate tables,
    and every other construct that cannot be merged without a real TOML parser.
    This is a safety gate, not a general TOML implementation.
    """
    tables: dict[str, set[str]] = {"": set()}
    current = ""
    table_pattern = re.compile(
        r"^\s*\[([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)\]\s*(?:#.*)?$"
    )
    key_pattern = re.compile(
        r'''^\s*([A-Za-z0-9_-]+)\s*=\s*'''
        r'''(?:"(?:[^"\\]|\\.)*"|'[^']*'|true|false|'''
        r'''[+-]?(?:\d(?:_?\d)*)(?:\.\d(?:_?\d)*)?)\s*(?:#.*)?$'''
    )
    for number, raw_line in enumerate(text.splitlines(), 1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        table_match = table_pattern.match(raw_line)
        if table_match:
            current = table_match.group(1)
            if current in tables:
                raise ValueError(f"duplicate or reopened TOML table [{current}] at line {number}")
            tables[current] = set()
            continue
        key_match = key_pattern.match(raw_line)
        if not key_match:
            raise ValueError(
                f"unsupported or ambiguous TOML syntax at line {number}; "
                "Python 3.11+ is required to merge this file safely"
            )
        key = key_match.group(1)
        if key in tables[current]:
            label = current or "<root>"
            raise ValueError(f"duplicate TOML key {key!r} in [{label}] at line {number}")
        tables[current].add(key)
    return tables


def atomic_write(path: Path, data: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
    ) as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    if mode is not None:
        temporary.chmod(mode)
    os.replace(temporary, path)


def _wait_json_response(
    messages: queue.Queue[str], request_id: int, timeout: int = 20
) -> tuple[dict[str, Any], list[str]]:
    deadline = time.monotonic() + timeout
    captured: list[str] = []
    while time.monotonic() < deadline:
        remaining = max(0.01, deadline - time.monotonic())
        try:
            raw = messages.get(timeout=remaining)
        except queue.Empty:
            break
        captured.append(raw)
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("id") == request_id:
            return message, captured
    raise RuntimeError(
        "timed out waiting for Codex app-server response id=%s; received=%s"
        % (request_id, captured[-5:])
    )


def codex_hooks_list(repo: Path) -> dict[str, Any]:
    """Ask the installed Codex app-server how it recognizes this repo's Hooks."""
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("codex executable is unavailable; Hook recognition cannot be verified")
    process = subprocess.Popen(
        [codex, "app-server", "--listen", "stdio://"],
        cwd=str(repo),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    messages: queue.Queue[str] = queue.Queue()
    stderr_lines: list[str] = []

    def read_stdout() -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            messages.put(line.rstrip("\n"))

    def read_stderr() -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            stderr_lines.append(line.rstrip("\n"))

    threading.Thread(target=read_stdout, daemon=True).start()
    threading.Thread(target=read_stderr, daemon=True).start()
    captured: list[str] = []
    try:
        if process.stdin is None:
            raise RuntimeError("Codex app-server stdin is unavailable")
        initialize = {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "software-delivery-harness-init",
                    "version": VERSION,
                },
                "capabilities": {"experimentalApi": True},
            },
        }
        process.stdin.write(json.dumps(initialize, separators=(",", ":")) + "\n")
        process.stdin.flush()
        initialized, lines = _wait_json_response(messages, 1)
        captured.extend(lines)
        if not isinstance(initialized.get("result"), dict):
            raise RuntimeError("Codex app-server initialize returned no result")
        process.stdin.write('{"method":"initialized","params":{}}\n')
        request = {
            "id": 2,
            "method": "hooks/list",
            "params": {"cwds": [str(repo.resolve())]},
        }
        process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        process.stdin.flush()
        response, lines = _wait_json_response(messages, 2)
        captured.extend(lines)
        if not isinstance(response.get("result"), dict):
            raise RuntimeError("Codex hooks/list returned no result")
        return {"response": response, "stdout": captured, "stderr": stderr_lines}
    finally:
        try:
            if process.stdin is not None:
                process.stdin.close()
        except Exception:
            pass
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=5)
            except Exception:
                pass
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass


def codex_config_batch_write(repo: Path, edits: list[dict[str, Any]]) -> dict[str, Any]:
    """Persist already-vetted config edits through the installed app-server."""
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("codex executable is unavailable; Hook trust cannot be persisted")
    process = subprocess.Popen(
        [codex, "app-server", "--listen", "stdio://"],
        cwd=str(repo),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    messages: queue.Queue[str] = queue.Queue()
    stderr_lines: list[str] = []

    def read_stdout() -> None:
        if process.stdout is not None:
            for line in process.stdout:
                messages.put(line.rstrip("\n"))

    def read_stderr() -> None:
        if process.stderr is not None:
            for line in process.stderr:
                stderr_lines.append(line.rstrip("\n"))

    threading.Thread(target=read_stdout, daemon=True).start()
    threading.Thread(target=read_stderr, daemon=True).start()
    captured: list[str] = []
    try:
        if process.stdin is None:
            raise RuntimeError("Codex app-server stdin is unavailable")
        initialize = {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "software-delivery-harness-init",
                    "version": VERSION,
                },
                "capabilities": {"experimentalApi": True},
            },
        }
        process.stdin.write(json.dumps(initialize, separators=(",", ":")) + "\n")
        process.stdin.flush()
        initialized, lines = _wait_json_response(messages, 1)
        captured.extend(lines)
        if not isinstance(initialized.get("result"), dict):
            raise RuntimeError("Codex app-server initialize returned no result")
        process.stdin.write('{"method":"initialized","params":{}}\n')
        request = {
            "id": 2,
            "method": "config/batchWrite",
            "params": {
                "edits": edits,
                "expectedVersion": None,
                "reloadUserConfig": True,
            },
        }
        process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        process.stdin.flush()
        response, lines = _wait_json_response(messages, 2)
        captured.extend(lines)
        if not isinstance(response.get("result"), dict):
            raise RuntimeError(
                "Codex config/batchWrite failed: %s" % response.get("error", response)
            )
        return {"response": response, "stdout": captured, "stderr": stderr_lines}
    finally:
        try:
            if process.stdin is not None:
                process.stdin.close()
        except Exception:
            pass
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=5)
            except Exception:
                pass
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass


class Installer:
    def __init__(self, repo: Path, *, auto_trust_canonical_hooks: bool = False) -> None:
        self.repo = repo.resolve()
        self.auto_trust_canonical_hooks = auto_trust_canonical_hooks
        self.run_stamp = stamp()
        self.backup_root = self.repo / ".harness" / "backups" / self.run_stamp
        self.created: list[str] = []
        self.modified: list[str] = []
        self.unchanged: list[str] = []
        self.backups: list[str] = []
        self.warnings: list[str] = []
        self.blockers: list[str] = []
        self.hook_verification: dict[str, Any] = {
            "status": "NOT_RUN",
            "events": [],
            "warnings": [],
            "errors": [],
            "remediation": None,
        }

    def safe_managed_path(self, path: Path) -> bool:
        """Reject lexical escapes and every existing symlink in a write path."""
        candidate = Path(os.path.abspath(str(path)))
        try:
            relative = candidate.relative_to(self.repo)
        except ValueError:
            self.blockers.append(f"managed path escapes repository: {candidate}")
            return False
        current = self.repo
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                self.blockers.append(
                    f"managed path contains a symbolic link and was not modified: {current}"
                )
                return False
        try:
            if candidate.exists():
                candidate.resolve().relative_to(self.repo)
        except (OSError, ValueError):
            self.blockers.append(f"managed path resolves outside repository: {candidate}")
            return False
        return True

    def preflight_managed_paths(self) -> None:
        managed = [
            self.repo / "AGENTS.md",
            self.repo / ".gitignore",
            self.repo / ".codex",
            self.repo / ".harness",
            self.repo / ".github",
            self.repo / "scripts",
        ]
        for root in managed:
            if not self.safe_managed_path(root):
                continue
            if not root.exists() or not root.is_dir():
                continue
            for directory, directories, filenames in os.walk(root, followlinks=False):
                base = Path(directory)
                for name in list(directories) + list(filenames):
                    item = base / name
                    if item.is_symlink():
                        self.blockers.append(
                            f"managed tree contains a symbolic link and initialization was blocked: {item}"
                        )

    def relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.repo).as_posix()
        except ValueError:
            return str(path.resolve())

    def backup(self, path: Path) -> None:
        if not path.exists():
            return
        if not self.safe_managed_path(path):
            return
        relative = path.resolve().relative_to(self.repo)
        destination = self.backup_root / relative
        if not self.safe_managed_path(destination):
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        self.backups.append(self.relative(destination))

    def write(self, path: Path, data: str, *, executable: bool = False) -> None:
        if not self.safe_managed_path(path):
            return
        new_mode = 0o755 if executable else 0o644
        if path.exists():
            try:
                existing = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                self.blockers.append(f"cannot read existing {self.relative(path)}: {exc}")
                return
            if existing == data:
                current_mode = stat.S_IMODE(path.stat().st_mode)
                desired_mode = current_mode | 0o111 if executable else current_mode
                if desired_mode != current_mode:
                    self.backup(path)
                    path.chmod(desired_mode)
                    self.modified.append(self.relative(path))
                else:
                    self.unchanged.append(self.relative(path))
                return
            self.backup(path)
            current_mode = stat.S_IMODE(path.stat().st_mode)
            desired_mode = current_mode | 0o111 if executable else current_mode
            atomic_write(path, data, desired_mode)
            self.modified.append(self.relative(path))
        else:
            atomic_write(path, data, new_mode)
            self.created.append(self.relative(path))

    def create_only(self, path: Path, data: str) -> None:
        if not self.safe_managed_path(path):
            return
        if path.exists():
            self.unchanged.append(self.relative(path))
            return
        atomic_write(path, data, 0o644)
        self.created.append(self.relative(path))

    def merge_agents(self) -> None:
        path = self.repo / "AGENTS.md"
        section = f"""{START_MARKER}
## Software Delivery Harness

- Apply `$software-delivery-harness` to software changes in this repository.
- Before product edits, maintain `.harness/spec/requirements.json`, test cases,
  and criterion-level traceability. Never silently reduce scope.
- Separate implementation, testing, independent runtime acceptance, security
  review, and release judgment. A worker cannot make its own final verdict.
- Use only `PASS`, `FAIL`, `BLOCKED`, and `NOT_RUN`; missing machine evidence is
  never `PASS`.
- Run `python3 scripts/harness_verify.py --strict --json` before release or a
  completion claim. Delivery is complete only when `release_allowed` is true.
- Subagents must end with labeled `TASK_STATUS`, `FILES`, `EVIDENCE`, `FAILURES`,
  `BLOCKERS`, and `NOT_RUN` lines.
- Before every subagent spawn, the primary agent must automatically run
  `python3 scripts/harness_authorize_subagent.py` with the exact role, requirement
  or test IDs, and narrowest allowed or reviewed paths. Every write or evidence
  role must name every intended changed path as a concrete `--required-file`;
  evidence-role outputs must finish as non-empty regular files.
  Do not delegate this routine authorization or validation step back to the user.
- Run exactly one matching specialist per pending authorization through
  `python3 scripts/harness_run_subagent.py`. This trusted launcher atomically
  claims the authorization, holds the exclusive repository lease, disables
  nested agents, applies the role sandbox, and must verify the correlated stop
  receipt. Never call native `Agent`, `spawn_agent`, or
  `collaboration.spawn_agent` tools in a Harness repository: project
  `features.multi_agent` is disabled because desktop-specialized paths can bypass
  project Hooks. Native `PreToolUse`, `SubagentStart`, and `SubagentStop` Hooks
  remain defense in depth only.
{END_MARKER}
"""
        if not path.exists():
            self.write(path, section)
            return
        existing = path.read_text(encoding="utf-8")
        start_count = existing.count(START_MARKER)
        end_count = existing.count(END_MARKER)
        if start_count == 1 and end_count == 1 and existing.index(START_MARKER) < existing.index(END_MARKER):
            pattern = re.compile(
                re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER), re.DOTALL
            )
            merged = pattern.sub(section.strip(), existing, count=1).rstrip() + "\n"
        elif start_count or end_count:
            self.blockers.append(
                "AGENTS.md contains missing, duplicated, or out-of-order Harness markers"
            )
            return
        else:
            merged = existing.rstrip() + "\n\n" + section
        self.write(path, merged)

    def merge_gitignore(self) -> None:
        path = self.repo / ".gitignore"
        section = "\n".join(
            (GITIGNORE_START_MARKER,) + RUNTIME_GITIGNORE_LINES + (GITIGNORE_END_MARKER,)
        )
        section += "\n"
        if not path.exists():
            self.write(path, section)
            return
        try:
            existing = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            self.blockers.append(f"cannot safely merge existing .gitignore: {exc}")
            return
        start_count = existing.count(GITIGNORE_START_MARKER)
        end_count = existing.count(GITIGNORE_END_MARKER)
        if (
            start_count == 1
            and end_count == 1
            and existing.index(GITIGNORE_START_MARKER) < existing.index(GITIGNORE_END_MARKER)
        ):
            pattern = re.compile(
                re.escape(GITIGNORE_START_MARKER)
                + r".*?"
                + re.escape(GITIGNORE_END_MARKER),
                re.DOTALL,
            )
            merged = pattern.sub(section.strip(), existing, count=1).rstrip() + "\n"
        elif start_count or end_count:
            self.blockers.append(
                ".gitignore contains missing, duplicated, or out-of-order Harness runtime markers"
            )
            return
        else:
            merged = existing.rstrip() + "\n\n" + section
        self.write(path, merged)

    def merge_config_toml(self) -> None:
        path = self.repo / ".codex" / "config.toml"
        if not path.exists():
            self.write(
                path,
                "[features]\nhooks = true\nmulti_agent = false\n\n"
                "[agents]\nenabled = true\n"
                "max_concurrent_threads_per_session = 3\n"
                "interrupt_message = true\n",
            )
            return
        original = path.read_text(encoding="utf-8")
        if tomllib is not None:
            try:
                parsed = tomllib.loads(original)
            except Exception as exc:
                self.blockers.append(f"existing .codex/config.toml is invalid TOML: {exc}")
                return
            features = parsed.get("features")
            if features is not None and not isinstance(features, dict):
                self.blockers.append("existing config.toml [features] value is not a table")
                return
        else:
            try:
                conservative_toml_tables(original)
            except ValueError as exc:
                self.blockers.append(f"cannot safely merge existing .codex/config.toml: {exc}")
                return
        lines = original.splitlines()
        section_start: int | None = None
        section_end = len(lines)
        for index, line in enumerate(lines):
            if re.match(r"^\s*\[features\]\s*(?:#.*)?$", line):
                section_start = index
                for later in range(index + 1, len(lines)):
                    if re.match(r"^\s*\[", lines[later]):
                        section_end = later
                        break
                break
        if section_start is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend(["[features]", "hooks = true", "multi_agent = false"])
        else:
            hook_index: int | None = None
            for index in range(section_start + 1, section_end):
                if re.match(r"^\s*hooks\s*=", lines[index]):
                    hook_index = index
                    break
            if hook_index is None:
                lines.insert(section_start + 1, "hooks = true")
            else:
                prefix = re.match(r"^(\s*)", lines[hook_index]).group(1)
                lines[hook_index] = f"{prefix}hooks = true"
            section_end = len(lines)
            for later in range(section_start + 1, len(lines)):
                if re.match(r"^\s*\[", lines[later]):
                    section_end = later
                    break
            multi_agent_matches = [
                index for index in range(section_start + 1, section_end)
                if re.match(r"^\s*multi_agent\s*=", lines[index])
            ]
            if len(multi_agent_matches) > 1:
                self.blockers.append("existing config.toml [features] repeats multi_agent")
            elif multi_agent_matches:
                index = multi_agent_matches[0]
                prefix = re.match(r"^(\s*)", lines[index]).group(1)
                lines[index] = f"{prefix}multi_agent = false"
            else:
                lines.insert(section_start + 1, "multi_agent = false")
        def table_bounds(name: str) -> tuple[int | None, int]:
            start: int | None = None
            end = len(lines)
            table_pattern = re.compile(
                r"^\s*\[" + re.escape(name) + r"\]\s*(?:#.*)?$"
            )
            for candidate, current in enumerate(lines):
                if table_pattern.match(current):
                    start = candidate
                    for later in range(candidate + 1, len(lines)):
                        if re.match(r"^\s*\[", lines[later]):
                            end = later
                            break
                    break
            return start, end

        def upsert_agent_value(key: str, rendered: str, minimum: int | None = None) -> None:
            start, end = table_bounds("agents")
            if start is None:
                if lines and lines[-1].strip():
                    lines.append("")
                lines.extend(["[agents]", f"{key} = {rendered}"])
                return
            matches = [
                index for index in range(start + 1, end)
                if re.match(r"^\s*" + re.escape(key) + r"\s*=", lines[index])
            ]
            if len(matches) > 1:
                self.blockers.append(f"existing config.toml [agents] repeats {key}")
                return
            if matches:
                index = matches[0]
                prefix = re.match(r"^(\s*)", lines[index]).group(1)
                if minimum is not None:
                    value_match = re.match(
                        r"^\s*" + re.escape(key) + r"\s*=\s*([0-9]+)\s*(?:#.*)?$",
                        lines[index],
                    )
                    if value_match is None:
                        self.blockers.append(
                            f"existing config.toml [agents].{key} must be an integer"
                        )
                        return
                    rendered = str(max(minimum, int(value_match.group(1))))
                lines[index] = f"{prefix}{key} = {rendered}"
            else:
                lines.insert(start + 1, f"{key} = {rendered}")

        upsert_agent_value("enabled", "true")
        upsert_agent_value("max_concurrent_threads_per_session", "3", minimum=3)
        upsert_agent_value("interrupt_message", "true")
        if self.blockers:
            return
        merged = "\n".join(lines).rstrip() + "\n"
        if tomllib is not None:
            try:
                tomllib.loads(merged)
            except Exception as exc:
                self.blockers.append(f"merged .codex/config.toml would be invalid: {exc}")
                return
        else:
            try:
                conservative_toml_tables(merged)
            except ValueError as exc:
                self.blockers.append(f"merged .codex/config.toml is ambiguous: {exc}")
                return
        self.write(path, merged)

    def wanted_hook_handlers(self) -> dict[str, dict[str, Any]]:
        verifier = CANONICAL_SKILL_ROOT / "scripts" / "harness_verify.py"
        base_command = (
            f"python3 {shlex.quote(str(verifier))} --repo "
            f"{shlex.quote(str(self.repo))} --strict --json"
        )
        base_windows = f'py -3 "{verifier}" --repo "{self.repo}" --strict --json'

        def handler(event: str, timeout: int, message: str) -> dict[str, Any]:
            return {
                "type": "command",
                "command": f"{base_command} --expected-hook {event}",
                "commandWindows": f"{base_windows} --expected-hook {event}",
                "timeout": timeout,
                "statusMessage": message,
            }

        return {
            "PreToolUse": handler(
                "PreToolUse", 120, "Checking Agent authorization before spawn"
            ),
            "Stop": handler("Stop", 600, "Verifying delivery evidence"),
            "SubagentStart": handler(
                "SubagentStart", 120, "Capturing authorized subagent baseline"
            ),
            "SubagentStop": handler(
                "SubagentStop", 120, "Checking subagent authorization and evidence"
            ),
        }

    @staticmethod
    def is_harness_hook_command(value: Any) -> bool:
        command = str(value or "")
        return "harness_verify.py" in command and "--strict" in command and "--json" in command

    def merge_hooks(self) -> None:
        path = self.repo / ".codex" / "hooks.json"
        if path.exists():
            try:
                document = strict_json_load(path)
            except Exception as exc:
                self.blockers.append(f"existing .codex/hooks.json is invalid: {exc}")
                return
            if not isinstance(document, dict):
                self.blockers.append("existing .codex/hooks.json root is not an object")
                return
        else:
            document = {}
        hooks = document.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            self.blockers.append("existing .codex/hooks.json hooks is not an object")
            return
        document.setdefault("description", "Software delivery lifecycle gates for this project.")
        verifier = CANONICAL_SKILL_ROOT / "scripts" / "harness_verify.py"
        if not verifier.is_file():
            self.blockers.append(
                "canonical user Skill verifier is missing; cannot install a tamper-resistant "
                f"project Hook: {verifier}"
            )
            return
        wanted = self.wanted_hook_handlers()
        for event, handler in wanted.items():
            groups = hooks.setdefault(event, [])
            if not isinstance(groups, list):
                self.blockers.append(f"existing hooks.{event} is not an array")
                continue
            retained_groups: list[Any] = []
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    retained_groups.append(group)
                    continue
                remaining = [
                    existing for existing in group["hooks"]
                    if not (
                        isinstance(existing, dict)
                        and self.is_harness_hook_command(existing.get("command"))
                    )
                ]
                if remaining:
                    group["hooks"] = remaining
                    retained_groups.append(group)
            canonical_group: dict[str, Any] = {"hooks": [handler]}
            if event == "PreToolUse":
                canonical_group["matcher"] = PRETOOL_AGENT_MATCHER
            retained_groups.append(canonical_group)
            hooks[event] = retained_groups
        if self.blockers:
            return
        rendered = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        self.write(path, rendered)

    def auto_trust_hooks(self) -> None:
        """Trust only exact canonical Harness Hooks after explicit installer opt-in."""
        if not self.auto_trust_canonical_hooks:
            return
        source_path = (self.repo / ".codex" / "hooks.json").resolve()
        event_names = {
            "PreToolUse": "preToolUse",
            "Stop": "stop",
            "SubagentStart": "subagentStart",
            "SubagentStop": "subagentStop",
        }
        wanted = self.wanted_hook_handlers()
        try:
            observed = codex_hooks_list(self.repo)
            rows = observed["response"].get("result", {}).get("data", [])
            row = next(
                (
                    item for item in rows
                    if isinstance(item, dict)
                    and Path(str(item.get("cwd", ""))).resolve() == self.repo
                ),
                None,
            )
            if not isinstance(row, dict):
                raise RuntimeError("hooks/list returned no entry for the target repository")
            if row.get("warnings") or row.get("errors"):
                raise RuntimeError(
                    "hooks/list reported warnings or errors: %s / %s"
                    % (row.get("warnings"), row.get("errors"))
                )
            hooks = row.get("hooks") if isinstance(row.get("hooks"), list) else []
            edits: list[dict[str, Any]] = []
            trusted_events: list[str] = []
            for source_event, event_name in event_names.items():
                expected = wanted[source_event]
                matches = []
                for item in hooks:
                    if not isinstance(item, dict):
                        continue
                    try:
                        item_source = Path(str(item.get("sourcePath", ""))).resolve()
                    except (OSError, ValueError):
                        continue
                    if (
                        item_source == source_path
                        and item.get("source") == "project"
                        and item.get("eventName") == event_name
                        and item.get("handlerType") == "command"
                        and item.get("command") in {
                            expected["command"], expected["commandWindows"]
                        }
                        and item.get("timeoutSec") == expected["timeout"]
                        and item.get("enabled") is True
                    ):
                        matches.append(item)
                if len(matches) != 1:
                    raise RuntimeError(
                        "%s does not have exactly one enabled canonical Hook" % source_event
                    )
                item = matches[0]
                key = item.get("key")
                current_hash = item.get("currentHash")
                trust_status = item.get("trustStatus")
                if not isinstance(key, str) or not key:
                    raise RuntimeError("%s Hook key is missing" % source_event)
                if not isinstance(current_hash, str) or re.fullmatch(
                    r"sha256:[a-f0-9]{64}", current_hash
                ) is None:
                    raise RuntimeError("%s Hook hash is invalid" % source_event)
                if trust_status == "trusted":
                    trusted_events.append(event_name)
                    continue
                if trust_status not in {"untrusted", "modified"}:
                    raise RuntimeError(
                        "%s Hook has unsupported trust status %s"
                        % (source_event, trust_status)
                    )
                edits.append({
                    "keyPath": "hooks.state.%s.trusted_hash" % json.dumps(key),
                    "value": current_hash,
                    "mergeStrategy": "upsert",
                })
            if not edits:
                return
            codex_home = Path(
                os.path.expanduser(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
            ).resolve()
            config_path = codex_home / "config.toml"
            if config_path.is_symlink() or not config_path.is_file():
                raise RuntimeError("Codex user config is missing or unsafe: %s" % config_path)
            backup_dir = (
                codex_home / "backups" / "software-delivery-harness"
                / (self.run_stamp + "-before-auto-hook-trust")
            )
            backup_dir.mkdir(parents=True, exist_ok=False)
            backup_path = backup_dir / "config.toml"
            shutil.copy2(config_path, backup_path)
            self.backups.append(str(backup_path))
            written = codex_config_batch_write(self.repo, edits)
            result = written["response"].get("result", {})
            if result.get("status") != "ok":
                raise RuntimeError("config/batchWrite did not return status=ok")
            self.warnings.append(
                "automatically trusted %d exact canonical project Hook(s); pre-write "
                "Codex config backup: %s" % (len(edits), backup_path)
            )
        except Exception as exc:
            self.blockers.append(
                "automatic canonical Hook trust failed closed: %s: %s"
                % (type(exc).__name__, exc)
            )

    def verify_hooks(self) -> None:
        """Fail closed unless current Codex recognizes, enables, and trusts all Hooks."""
        remediation = (
            "Open `/hooks` in Codex, review and trust all four project Hooks, "
            "then rerun harness_init.py."
        )
        source_path = (self.repo / ".codex" / "hooks.json").resolve()
        event_names = {
            "PreToolUse": "preToolUse",
            "Stop": "stop",
            "SubagentStart": "subagentStart",
            "SubagentStop": "subagentStop",
        }
        wanted = self.wanted_hook_handlers()
        expected_events = set(event_names.values())
        expected_commands = {
            event_names[event]: {handler["command"], handler["commandWindows"]}
            for event, handler in wanted.items()
        }
        expected_timeouts = {
            event_names[event]: handler["timeout"] for event, handler in wanted.items()
        }
        try:
            configured = strict_json_load(source_path)
            configured_hooks = configured.get("hooks") if isinstance(configured, dict) else None
            if not isinstance(configured_hooks, dict):
                raise RuntimeError("installed hooks.json hooks is not an object")
            configuration_errors: list[str] = []
            for source_event, expected in wanted.items():
                groups = configured_hooks.get(source_event)
                candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
                if isinstance(groups, list):
                    for group in groups:
                        handlers = group.get("hooks") if isinstance(group, dict) else None
                        if isinstance(handlers, list):
                            candidates.extend(
                                (group, item) for item in handlers
                                if isinstance(item, dict)
                                and self.is_harness_hook_command(item.get("command"))
                            )
                matcher_valid = (
                    source_event != "PreToolUse"
                    or (
                        len(candidates) == 1
                        and candidates[0][0].get("matcher") == PRETOOL_AGENT_MATCHER
                    )
                )
                if len(candidates) != 1 or candidates[0][1] != expected or not matcher_valid:
                    configuration_errors.append(
                        f"hooks.{source_event} must contain exactly one canonical Harness handler"
                    )
            observed = codex_hooks_list(self.repo)
            response = observed["response"]
            rows = response.get("result", {}).get("data", [])
            row = next(
                (
                    item
                    for item in rows
                    if isinstance(item, dict)
                    and Path(str(item.get("cwd", ""))).resolve() == self.repo
                ),
                None,
            )
            if not isinstance(row, dict):
                raise RuntimeError("hooks/list returned no entry for the target repository")
            warnings = (
                row.get("warnings", [])
                if isinstance(row.get("warnings"), list)
                else ["invalid warnings shape"]
            )
            errors = (
                row.get("errors", [])
                if isinstance(row.get("errors"), list)
                else ["invalid errors shape"]
            )
            hooks = row.get("hooks", []) if isinstance(row.get("hooks"), list) else []
            candidates_by_event: dict[str, list[dict[str, Any]]] = {}
            for item in hooks:
                if not isinstance(item, dict):
                    continue
                try:
                    item_source = Path(str(item.get("sourcePath", ""))).resolve()
                except (OSError, ValueError):
                    continue
                if (
                    item_source == source_path
                    and item.get("eventName") in expected_events
                    and item.get("handlerType") == "command"
                    and self.is_harness_hook_command(item.get("command"))
                ):
                    candidates_by_event.setdefault(str(item.get("eventName")), []).append(item)
            event_results: list[dict[str, Any]] = []
            for event in sorted(expected_events):
                matches = candidates_by_event.get(event, [])
                exact = [
                    item for item in matches
                    if item.get("command") in expected_commands[event]
                    and item.get("timeoutSec") == expected_timeouts[event]
                ]
                if len(matches) != 1 or len(exact) != 1:
                    event_results.append(
                        {
                            "event": event,
                            "recognized": False,
                            "enabled": False,
                            "trust_status": "unknown",
                            "current_hash": None,
                        }
                    )
                    continue
                item = exact[0]
                event_results.append(
                    {
                        "event": event,
                        "recognized": True,
                        "enabled": item.get("enabled") is True,
                        "trust_status": item.get("trustStatus"),
                        "current_hash": item.get("currentHash"),
                        "handler_type": item.get("handlerType"),
                        "command": item.get("command"),
                        "source_path": item.get("sourcePath"),
                        "timeout_seconds": item.get("timeoutSec"),
                    }
                )
            passed = (
                not configuration_errors
                and not warnings
                and not errors
                and len(event_results) == 4
                and all(
                    item.get("recognized") is True
                    and item.get("enabled") is True
                    and item.get("trust_status") == "trusted"
                    and item.get("handler_type") == "command"
                    and item.get("timeout_seconds") == expected_timeouts[item["event"]]
                    for item in event_results
                )
            )
            self.hook_verification = {
                "status": "PASS" if passed else "BLOCKED",
                "events": event_results,
                "warnings": warnings,
                "errors": configuration_errors + errors,
                "remediation": None if passed else remediation,
            }
            if not passed:
                self.blockers.append(
                    "project Hooks are not all recognized, enabled, canonical, and trusted by "
                    "the current Codex; " + remediation
                )
        except Exception as exc:
            self.hook_verification = {
                "status": "BLOCKED",
                "events": [],
                "warnings": [],
                "errors": [f"{type(exc).__name__}: {exc}"],
                "remediation": remediation,
            }
            self.blockers.append(
                "cannot verify project Hook recognition/trust: "
                f"{type(exc).__name__}: {exc}; {remediation}"
            )

    def detect_checks(self) -> dict[str, dict[str, Any]]:
        categories = [
            "dependency_install", "lint", "type_check", "build", "unit", "api",
            "database", "integration", "e2e", "file", "permission", "security"
        ]
        checks: dict[str, dict[str, Any]] = {
            key: {
                "applicable": False,
                "command": None,
                "reason": "No project manifest or matching test command was detected during initialization."
            }
            for key in categories
        }

        def merge_detected_check(category: str, candidate: dict[str, Any]) -> None:
            existing = checks[category]
            if (
                existing.get("applicable") is True
                and existing.get("command")
                and candidate.get("applicable") is True
                and candidate.get("command")
                and existing.get("command") != candidate.get("command")
            ):
                checks[category] = {
                    "applicable": True,
                    "command": "%s && %s" % (existing["command"], candidate["command"]),
                    "reason": "%s %s" % (existing.get("reason", ""), candidate.get("reason", "")),
                }
            else:
                checks[category] = candidate

        package_json = self.repo / "package.json"
        if package_json.exists():
            try:
                package = strict_json_load(package_json)
                scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
            except Exception as exc:
                self.blockers.append(f"cannot inspect package.json: {exc}")
                scripts = {}
            manager = "npm"
            run_prefix = "npm run"
            install = "npm ci" if (self.repo / "package-lock.json").exists() else "npm install"
            if (self.repo / "pnpm-lock.yaml").exists():
                manager, run_prefix, install = "pnpm", "pnpm", "corepack enable && pnpm install --frozen-lockfile"
            elif (self.repo / "yarn.lock").exists():
                manager, run_prefix, install = "yarn", "yarn", "corepack enable && yarn install --immutable"
            elif (self.repo / "bun.lockb").exists() or (self.repo / "bun.lock").exists():
                manager, run_prefix, install = "bun", "bun run", "bun install --frozen-lockfile"
            checks["dependency_install"] = {"applicable": True, "command": install, "reason": f"Detected {manager} project."}
            script_names = {
                "lint": ["lint"],
                "type_check": ["typecheck", "type-check", "types"],
                "build": ["build"],
                "unit": ["test:unit", "unit", "test"],
                "api": ["test:api", "api:test"],
                "database": ["test:database", "test:db"],
                "integration": ["test:integration", "integration"],
                "e2e": ["test:e2e", "e2e"],
                "file": ["test:file", "test:files"],
                "permission": ["test:permission", "test:permissions"],
                "security": ["test:security", "security:test"]
            }
            for category, names in script_names.items():
                selected = next((name for name in names if isinstance(scripts, dict) and name in scripts), None)
                if selected:
                    checks[category] = {
                        "applicable": True,
                        "command": f"{run_prefix} {selected}",
                        "reason": f"Detected package.json script {selected}."
                    }
            playwright = any(self.repo.glob("playwright.config.*"))
            cypress = any(self.repo.glob("cypress.config.*"))
            if playwright and not checks["e2e"]["applicable"]:
                checks["e2e"] = {"applicable": True, "command": "npx playwright test", "reason": "Detected Playwright config."}
            if cypress and not checks["e2e"]["applicable"]:
                checks["e2e"] = {"applicable": True, "command": "npx cypress run", "reason": "Detected Cypress config."}
        requirements = self.repo / "requirements.txt"
        pyproject = self.repo / "pyproject.toml"
        if requirements.exists() or pyproject.exists():
            install = "python3 -m pip install -r requirements.txt" if requirements.exists() else "python3 -m pip install ."
            merge_detected_check("dependency_install", {"applicable": True, "command": install, "reason": "Detected Python project manifest."})
            config_text = ""
            if pyproject.exists():
                config_text = pyproject.read_text(encoding="utf-8", errors="replace")
            if "pytest" in config_text or any(self.repo.glob("pytest.ini")) or any(self.repo.glob("conftest.py")):
                merge_detected_check("unit", {"applicable": True, "command": "python3 -m pytest", "reason": "Detected pytest configuration."})
            if "ruff" in config_text:
                merge_detected_check("lint", {"applicable": True, "command": "python3 -m ruff check .", "reason": "Detected Ruff configuration."})
            if "mypy" in config_text:
                merge_detected_check("type_check", {"applicable": True, "command": "python3 -m mypy .", "reason": "Detected mypy configuration."})
            if pyproject.exists():
                merge_detected_check("build", {"applicable": True, "command": "python3 -m build", "reason": "Detected pyproject.toml; build dependency must be declared by the project."})
        return checks

    def merge_harness_config(self, checks: dict[str, dict[str, Any]]) -> Any:
        path = self.repo / ".harness" / "config.json"
        defaults: dict[str, Any] = {
            "standard_version": VERSION,
            "harness_name": "software-delivery-harness",
            "harness_version": VERSION,
            "project_name": self.repo.name,
            "strict": True,
            "git_required": True,
            "baseline_required": True,
            "automatic_subagent_orchestration": True,
            "required_hierarchy_depth": 5,
            "group_nodes_require_children": True,
            "allowed_statuses": ["PASS", "FAIL", "BLOCKED", "NOT_RUN"],
            "source_scan_paths": ["."],
            "scan_excludes": [],
            "checks": checks,
            "heuristics": {
                "strict_unresolved_critical_findings": True,
                "exclude_paths": [
                    ".git", ".harness/runs", ".harness/backups", "node_modules",
                    "vendor", "dist", "build", ".venv", "venv", "__pycache__"
                ]
            }
        }
        if path.exists():
            try:
                existing = strict_json_load(path)
            except Exception as exc:
                self.blockers.append(f"existing .harness/config.json is invalid: {exc}")
                return None
            if not isinstance(existing, dict):
                self.blockers.append("existing .harness/config.json root is not an object")
                return None
            merged = defaults | existing
            merged["standard_version"] = VERSION
            merged["harness_name"] = "software-delivery-harness"
            merged["harness_version"] = VERSION
            merged["strict"] = True
            merged["git_required"] = True
            merged["baseline_required"] = True
            merged["automatic_subagent_orchestration"] = True
            merged["group_nodes_require_children"] = True
            existing_depth = existing.get("required_hierarchy_depth")
            merged["required_hierarchy_depth"] = max(
                5,
                existing_depth
                if isinstance(existing_depth, int) and not isinstance(existing_depth, bool)
                else 5,
            )
            merged["allowed_statuses"] = ["PASS", "FAIL", "BLOCKED", "NOT_RUN"]
            existing_scan_paths = existing.get("source_scan_paths", [])
            if not isinstance(existing_scan_paths, list) or any(
                not isinstance(item, str) or not item.strip()
                for item in existing_scan_paths
            ):
                self.blockers.append(
                    "existing .harness/config.json source_scan_paths must be a list "
                    "of non-empty repository-relative paths"
                )
                return None
            merged["source_scan_paths"] = list(dict.fromkeys(
                defaults["source_scan_paths"] + existing_scan_paths
            ))
            existing_scan_excludes = existing.get("scan_excludes", [])
            if not isinstance(existing_scan_excludes, list) or any(
                not isinstance(item, str) or not item.strip()
                for item in existing_scan_excludes
            ):
                self.blockers.append(
                    "existing .harness/config.json scan_excludes must be a list "
                    "of non-empty repository-relative paths"
                )
                return None
            merged["scan_excludes"] = list(dict.fromkeys(existing_scan_excludes))
            existing_checks = existing.get("checks", {})
            if not isinstance(existing_checks, dict):
                self.blockers.append("existing .harness/config.json checks is not an object")
                return None
            merged_checks = dict(existing_checks)
            for category, detected in checks.items():
                configured = existing_checks.get(category)
                if category in existing_checks and not isinstance(configured, dict):
                    self.blockers.append(
                        f"existing .harness/config.json checks.{category} is not an object"
                    )
                    return None
                configured = configured if isinstance(configured, dict) else {}
                combined = detected | configured
                if configured.get("applicable") is True:
                    configured_command = configured.get("command")
                    if not isinstance(configured_command, str) or not configured_command.strip():
                        self.blockers.append(
                            f"existing applicable check {category} lacks a non-empty command; "
                            "refusing to replace a project-defined gate"
                        )
                        return None
                    combined["applicable"] = True
                    combined["command"] = configured_command
                    combined["reason"] = configured.get("reason")
                    if (
                        detected.get("applicable") is True
                        and detected.get("command") != configured_command
                    ):
                        self.warnings.append(
                            f"preserved existing checks.{category}.command instead of "
                            "overwriting it with auto-detection"
                        )
                elif detected.get("applicable"):
                    combined["applicable"] = True
                    combined["command"] = detected.get("command")
                    combined["reason"] = detected.get("reason")
                merged_checks[category] = combined
            merged["checks"] = merged_checks
            existing_heuristics = existing.get("heuristics")
            heuristics = dict(defaults["heuristics"])
            if isinstance(existing_heuristics, dict):
                heuristics.update(existing_heuristics)
                configured_excludes = existing_heuristics.get("exclude_paths")
                if isinstance(configured_excludes, list):
                    heuristics["exclude_paths"] = list(dict.fromkeys(
                        defaults["heuristics"]["exclude_paths"]
                        + [item for item in configured_excludes if isinstance(item, str) and item]
                    ))
            heuristics["strict_unresolved_critical_findings"] = True
            merged["heuristics"] = heuristics
        else:
            merged = defaults
        self.write(path, json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return merged["checks"]

    def copy_runtime(self) -> None:
        schema_source = SKILL_ROOT / "assets"
        if not schema_source.is_dir():
            schema_source = SKILL_ROOT / ".harness" / "schemas"
        if not schema_source.is_dir():
            self.blockers.append(
                "cannot locate Harness Schema assets beside the Skill or project copy"
            )
            return
        missing_schemas = [name for name in REQUIRED_SCHEMA_FILES if not (schema_source / name).is_file()]
        script_source = SKILL_ROOT / "scripts"
        missing_scripts = [name for name in REQUIRED_RUNTIME_SCRIPTS if not (script_source / name).is_file()]
        if missing_schemas or missing_scripts:
            if missing_schemas:
                self.blockers.append("Harness runtime is missing required Schema assets: %s" % ", ".join(missing_schemas))
            if missing_scripts:
                self.blockers.append("Harness runtime is missing required scripts: %s" % ", ".join(missing_scripts))
            return
        for name in REQUIRED_SCHEMA_FILES:
            source = schema_source / name
            self.write(self.repo / ".harness" / "schemas" / source.name, source.read_text(encoding="utf-8"))
        for name in REQUIRED_RUNTIME_SCRIPTS:
            source = script_source / name
            self.write(self.repo / "scripts" / source.name, source.read_text(encoding="utf-8"), executable=True)

    def create_spec_templates(self) -> None:
        templates: dict[str, Any] = {
            "requirements.json": {"standard_version": VERSION, "requirements": []},
            "change-ledger.json": {"standard_version": VERSION, "change_ledger": []},
            "test-cases.json": {"standard_version": VERSION, "test_cases": []},
            "test-executions.json": {
                "standard_version": VERSION,
                "run_id": "UNASSIGNED",
                "git_commit": None,
                "test_executions": []
            },
            "evidence.json": {
                "standard_version": VERSION,
                "run_id": "UNASSIGNED",
                "git_commit": None,
                "evidence": []
            },
            "traceability.json": {"standard_version": VERSION, "traceability": []},
            "acceptance-results.json": {
                "standard_version": VERSION,
                "run_id": "UNASSIGNED",
                "git_commit": None,
                "acceptance_results": []
            }
        }
        for filename, document in templates.items():
            self.create_only(
                self.repo / ".harness" / "spec" / filename,
                json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            )
        baseline = {
            "standard_version": VERSION,
            "approved_at": None,
            "approved_by": None,
            "git_commit": None,
            "requirements_sha256": None,
            "requirements": []
        }
        self.create_only(
            self.repo / ".harness" / "state" / "requirements-baseline.json",
            json.dumps(baseline, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        for directory in [
            "state", "fixtures", "runs", "policy",
            "state/subagent-guard", "state/subagent-guard/pending",
            "state/subagent-guard/prestart",
            "state/subagent-guard/active", "state/subagent-guard/receipts",
        ]:
            target = self.repo / ".harness" / directory
            if not self.safe_managed_path(target):
                continue
            target.mkdir(parents=True, exist_ok=True)
            keep = target / ".gitkeep"
            if not keep.exists():
                atomic_write(keep, "", 0o644)
                self.created.append(self.relative(keep))
        self.write(
            self.repo / ".harness" / "policy" / "subagent-roles.json",
            json.dumps(
                canonical_subagent_role_policy(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n",
        )

    @staticmethod
    def yaml_quote(value: str) -> str:
        return json.dumps(value)

    def workflow(self, checks: dict[str, dict[str, Any]]) -> str:
        lines = [
            WORKFLOW_MARKER,
            "name: Harness Gate",
            "",
            "on:",
            "  pull_request:",
            "  push:",
            "    branches: [main, master]",
            "  workflow_dispatch:",
            "",
            "permissions:",
            "  contents: read",
            "",
            "jobs:",
            "  harness-gate:",
            "    runs-on: ubuntu-latest",
            "    timeout-minutes: 60",
            "    steps:",
            "      - uses: actions/checkout@v4",
            "        with:",
            "          fetch-depth: 0",
            "      - uses: actions/setup-python@v5",
            "        with:",
            "          python-version: '3.12'",
        ]
        labels = {
            "dependency_install": "Install dependencies",
            "lint": "Lint", "type_check": "Type check", "build": "Build",
            "unit": "Unit tests", "api": "API tests", "database": "Database tests",
            "integration": "Integration tests", "e2e": "E2E tests",
            "file": "File tests", "permission": "Permission tests",
            "security": "Security tests"
        }
        for category in labels:
            entry = checks[category]
            lines.append(f"      - name: {labels[category]}")
            if entry.get("applicable") and entry.get("command"):
                lines.append(f"        run: {self.yaml_quote(str(entry['command']))}")
            else:
                reason = str(entry.get("reason") or "Not applicable by Harness configuration.")
                command = "python3 -c " + shlex.quote(
                    "print(" + repr("NOT_APPLICABLE: " + reason) + ")"
                )
                lines.append(f"        run: {self.yaml_quote(command)}")
        lines.extend([
            "      - name: Harness Schema and semantic validation",
            "        run: python3 scripts/harness_validate.py",
            "      - name: Harness strict release gate",
            "        run: python3 scripts/harness_verify.py --collect --strict --json",
            "      - name: Upload Harness evidence and reports",
            "        if: always()",
            "        uses: actions/upload-artifact@v4",
            "        with:",
            "          name: harness-artifacts",
            "          path: .harness/runs/",
            "          if-no-files-found: error",
            "          retention-days: 30",
            ""
        ])
        return "\n".join(lines)

    def install_workflow(self, checks: dict[str, dict[str, Any]]) -> None:
        path = self.repo / ".github" / "workflows" / "harness-gate.yml"
        generated = self.workflow(checks)
        if path.exists():
            original = path.read_text(encoding="utf-8", errors="replace")
            if not original.startswith(WORKFLOW_MARKER):
                alternate = path.with_name("harness-gate.generated.yml")
                self.write(alternate, generated)
                self.blockers.append(
                    "existing .github/workflows/harness-gate.yml is not Harness-managed; "
                    "preserved it and wrote harness-gate.generated.yml for manual merge"
                )
                return
        self.write(path, generated)

    def git_inventory(self) -> dict[str, Any]:
        probe = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "--show-toplevel"],
            text=True, capture_output=True, check=False
        )
        if probe.returncode != 0:
            self.warnings.append("project is not a Git repository; strict release verification will be BLOCKED")
            return {"repository": False, "commit": None, "status": None}
        commit = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD^{commit}"],
            text=True, capture_output=True, check=False
        )
        status = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--short", "--branch"],
            text=True, capture_output=True, check=False
        )
        return {
            "repository": True,
            "commit": commit.stdout.strip() if commit.returncode == 0 else None,
            "status": status.stdout.strip() if status.returncode == 0 else None
        }

    def run(self) -> dict[str, Any]:
        if not self.repo.exists() or not self.repo.is_dir():
            self.blockers.append(f"repository path is not a directory: {self.repo}")
        elif not os.access(self.repo, os.W_OK):
            self.blockers.append(f"repository is not writable: {self.repo}")
        if self.blockers:
            return self.result({"repository": False, "commit": None, "status": None})
        self.preflight_managed_paths()
        if self.blockers:
            return self.result({"repository": False, "commit": None, "status": None})
        detected_checks = self.detect_checks()
        self.copy_runtime()
        self.create_spec_templates()
        self.merge_gitignore()
        workflow_checks = self.merge_harness_config(detected_checks)
        self.merge_agents()
        self.merge_config_toml()
        self.merge_hooks()
        self.auto_trust_hooks()
        if workflow_checks is not None:
            self.install_workflow(workflow_checks)
        self.verify_hooks()
        return self.result(self.git_inventory())

    def result(self, git: dict[str, Any]) -> dict[str, Any]:
        status = "BLOCKED" if self.blockers else "PASS"
        return {
            "standard_version": VERSION,
            "operation": "harness_init",
            "status": status,
            "repo": str(self.repo),
            "started_or_completed_at": now_utc(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "git": git,
            "created": sorted(set(self.created)),
            "modified": sorted(set(self.modified)),
            "unchanged": sorted(set(self.unchanged)),
            "backups": sorted(set(self.backups)),
            "warnings": self.warnings,
            "blockers": self.blockers,
            "hook_verification": self.hook_verification,
            "next_commands": [
                "python3 scripts/harness_validate.py",
                "python3 scripts/harness_verify.py --collect --strict --json",
                "python3 scripts/harness_report.py",
                "python3 scripts/harness_selftest.py"
            ]
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="repository root; defaults to current directory")
    parser.add_argument(
        "--auto-trust-canonical-hooks",
        action="store_true",
        help="persist trust only for exact canonical Harness Hooks after hash verification",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = Installer(
            Path(args.repo),
            auto_trust_canonical_hooks=args.auto_trust_canonical_hooks,
        ).run()
    except Exception as exc:  # fail closed and keep output machine-readable
        result = {
            "standard_version": VERSION,
            "operation": "harness_init",
            "status": "FAIL",
            "repo": str(Path(args.repo).resolve()),
            "created": [], "modified": [], "unchanged": [], "backups": [],
            "warnings": [], "blockers": [f"installer exception: {type(exc).__name__}: {exc}"],
            "hook_verification": {
                "status": "NOT_RUN", "events": [], "warnings": [], "errors": [],
                "remediation": None,
            },
            "next_commands": []
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
