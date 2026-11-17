#!/usr/bin/env python3
"""Deterministic core for Software Delivery Harness 1.0.0.

The module intentionally depends only on the Python standard library.  Its JSON
Schema engine implements a strict, finite Draft 2020-12 profile: every keyword
used by the bundled schemas is implemented and an unknown assertion keyword is
an error rather than being silently ignored.
"""

from __future__ import print_function

import datetime as _dt
import fnmatch
import ast
import hashlib
import ipaddress
import json
import math
import os
import platform
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import traceback
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    import resource
except ImportError:  # pragma: no cover - unavailable on Windows, where fresh execution blocks.
    resource = None


STANDARD_VERSION = "1.0.0"
STATUS_VALUES = ("PASS", "FAIL", "BLOCKED", "NOT_RUN")
STATUS_RANK = {"PASS": 0, "NOT_RUN": 1, "BLOCKED": 2, "FAIL": 3}

ARTIFACTS = (
    ("requirements", "requirements.json", "requirement.schema.json", "requirements"),
    ("change_ledger", "change-ledger.json", "change-ledger.schema.json", "change_ledger"),
    ("test_cases", "test-cases.json", "test-case.schema.json", "test_cases"),
    ("test_executions", "test-executions.json", "test-execution.schema.json", "test_executions"),
    ("evidence", "evidence.json", "evidence.schema.json", "evidence"),
    ("traceability", "traceability.json", "traceability.schema.json", "traceability"),
    ("acceptance_results", "acceptance-results.json", "acceptance-result.schema.json", "acceptance_results"),
)

SCHEMA_ANNOTATIONS = {
    "$schema", "$id", "$anchor", "$comment", "title", "description", "default",
    "examples", "deprecated", "readOnly", "writeOnly",
}
SCHEMA_ASSERTIONS = {
    "$ref", "$defs", "type", "enum", "const", "multipleOf", "maximum",
    "exclusiveMaximum", "minimum", "exclusiveMinimum", "maxLength", "minLength",
    "pattern", "format", "maxItems", "minItems", "uniqueItems", "maxContains", "minContains",
    "maxProperties", "minProperties", "required", "dependentRequired", "properties",
    "patternProperties", "additionalProperties", "propertyNames", "items", "prefixItems",
    "contains", "allOf", "anyOf", "oneOf", "not", "if", "then", "else",
}
KNOWN_SCHEMA_KEYS = SCHEMA_ANNOTATIONS | SCHEMA_ASSERTIONS
KNOWN_FORMATS = {
    "date-time", "date", "time", "duration", "email", "idn-email", "hostname",
    "idn-hostname", "ipv4", "ipv6", "uuid", "uri", "uri-reference", "regex",
    "json-pointer", "relative-json-pointer",
}

RUNTIME_MUTABLE_PATHS = {
    ".harness/spec/test-executions.json",
    ".harness/spec/evidence.json",
    ".harness/spec/acceptance-results.json",
    ".harness/state/latest-run.json",
    ".harness/state/latest-robot-run.json",
    ".harness/state/hook-state.json",
    ".harness/state/test-executions.json",
    ".harness/state/evidence.json",
    ".harness/state/acceptance-results.json",
    ".harness/state/traceability.json",
    ".harness/state/collection-manifest.json",
}
COLLECTED_RUNTIME_ARTIFACTS = {
    "test_executions": "test-executions.json",
    "evidence": "evidence.json",
    "acceptance_results": "acceptance-results.json",
    "traceability": "traceability.json",
}

MINIMUM_HIERARCHY_DEPTH = 5
GUARDED_REQUIREMENT_FIELDS = (
    "parent_id", "level", "category_path", "node_type", "title", "description",
    "source", "priority", "criticality", "actors", "permissions", "ui_contract",
    "api_contract", "database_contract", "file_contract", "acceptance_criteria",
    "test_case_ids", "dependencies", "out_of_scope", "assumptions", "created_at",
)
REDUCING_CHANGE_TYPES = {
    "delete", "merge", "scope_reduce", "acceptance_change", "contract_change",
    "criticality_change",
}
DEFAULT_SOURCE_SCAN_PATHS = (
    "src", "app", "lib", "server", "client", "frontend", "backend", "tests", "test",
)
TEST_REPORT_FORMAT = "software-delivery-harness-test-report/1"
SUPPORTED_TEST_REPORT_RUNNERS = frozenset(("unittest",))
SAFE_RUN_ID_PATTERN = re.compile(r"^(?:RUN|SELFTEST)-[A-Za-z0-9][A-Za-z0-9._-]*$")
CANONICAL_CHECK_NAMES = frozenset((
    "dependency_install", "lint", "type_check", "build", "unit", "api",
    "database", "integration", "e2e", "file", "permission", "security",
))
CANONICAL_SCRIPT_NAMES = (
    "_harness_core.py", "harness_authorize_subagent.py", "harness_run_subagent.py", "harness_init.py", "harness_validate.py",
    "harness_verify.py", "harness_report.py", "harness_selftest.py", "harness_install_report.py",
)
CANONICAL_SCHEMA_NAMES = tuple(item[2] for item in ARTIFACTS) + (
    "release-report.schema.json",
    "software-delivery-harness-installation-report.schema.json",
)
CANONICAL_SKILL_ROOT = Path.home() / ".agents" / "skills" / "software-delivery-harness"


class HarnessError(Exception):
    """Expected deterministic harness error."""


class DuplicateKeyError(HarnessError):
    pass


def utc_now():
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def new_run_id(prefix="RUN"):
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    value = "%s-%s-%s" % (prefix, stamp, uuid.uuid4().hex[:10])
    return validate_run_id(value)


def validate_run_id(value):
    if not isinstance(value, str) or len(value) > 128 or SAFE_RUN_ID_PATTERN.fullmatch(value) is None:
        raise HarnessError(
            "run_id must be a safe RUN-/SELFTEST- ASCII identifier of at most 128 characters"
        )
    return value


def _reject_constant(value):
    raise HarnessError("non-finite JSON number is forbidden: %s" % value)


def _no_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError("duplicate JSON object key: %s" % key)
        result[key] = value
    return result


def strict_json_loads(text, source="<memory>"):
    try:
        value = json.loads(
            text,
            object_pairs_hook=_no_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except (ValueError, TypeError, HarnessError) as exc:
        raise HarnessError("invalid strict JSON in %s: %s" % (source, exc))
    _reject_bad_unicode(value, source)
    return value


def _reject_bad_unicode(value, source):
    if isinstance(value, str):
        for char in value:
            if 0xD800 <= ord(char) <= 0xDFFF:
                raise HarnessError("unpaired Unicode surrogate in %s" % source)
    elif isinstance(value, list):
        for item in value:
            _reject_bad_unicode(item, source)
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_bad_unicode(key, source)
            _reject_bad_unicode(item, source)


def strict_json_load(path, required=True):
    path = Path(path)
    if not path.exists():
        if required:
            raise HarnessError("required JSON file is missing: %s" % path)
        return None
    if not path.is_file():
        raise HarnessError("JSON path is not a regular file: %s" % path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise HarnessError("cannot read JSON file %s: %s" % (path, exc))
    if len(raw) > 32 * 1024 * 1024:
        raise HarnessError("JSON file exceeds 32 MiB safety limit: %s" % path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HarnessError("JSON file is not UTF-8 %s: %s" % (path, exc))
    return strict_json_loads(text, str(path))


def canonical_json_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def atomic_write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    payload += "\n"
    fd, temp_name = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, str(path))
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _safe_repo_directory(repo, relative, create=False):
    """Resolve a real repository directory without following symlink components."""
    root = Path(repo).expanduser().resolve()
    lexical = Path(relative)
    if lexical.is_absolute() or not lexical.parts or any(part in ("", ".", "..") for part in lexical.parts):
        raise HarnessError("directory must be a normalized repository-relative path: %s" % relative)
    current = root
    for part in lexical.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if not create:
                raise HarnessError("required Harness directory is missing: %s" % current)
            try:
                current.mkdir(mode=0o755)
            except FileExistsError:
                pass
            mode = current.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise HarnessError("symlink is forbidden in Harness directory path: %s" % current)
        if not stat.S_ISDIR(mode):
            raise HarnessError("Harness directory component is not a directory: %s" % current)
    try:
        common = os.path.commonpath([str(root), str(current.resolve(strict=True))])
    except (OSError, ValueError) as exc:
        raise HarnessError("cannot resolve Harness directory %s: %s" % (current, exc))
    if common != str(root):
        raise HarnessError("Harness directory escapes repository: %s" % current)
    return current


def _assert_harness_layout_safe(repo):
    """Reject critical Harness directories that redirect reads or writes."""
    _safe_repo_directory(repo, ".harness", create=False)
    for relative in (".harness/spec", ".harness/schemas", ".harness/state", ".harness/runs"):
        path = Path(repo) / relative
        if path.exists() or path.is_symlink():
            _safe_repo_directory(repo, relative, create=False)


def _canonical_distribution_root():
    """Return the fixed user-level trust anchor; repository environment cannot replace it."""
    return CANONICAL_SKILL_ROOT


def _canonical_distribution_issues(repo):
    """Require the repository copy to be byte-identical to the user Skill distribution."""
    issues = []
    canonical_root = _canonical_distribution_root()
    try:
        if os.path.commonpath([str(Path(repo).resolve()), str(canonical_root.resolve())]) == str(Path(repo).resolve()):
            return [issue("CANONICAL_SKILL_UNAVAILABLE", "canonical distribution must be external to the target repository", status="BLOCKED")]
    except (OSError, ValueError):
        pass
    if canonical_root.is_symlink():
        return [issue("CANONICAL_SKILL_UNAVAILABLE", "canonical distribution root must not be a symlink", status="BLOCKED")]
    try:
        canonical_scripts = _safe_repo_directory(canonical_root, "scripts", create=False)
        canonical_assets = _safe_repo_directory(canonical_root, "assets", create=False)
    except (HarnessError, OSError) as exc:
        return [issue(
            "CANONICAL_SKILL_UNAVAILABLE",
            "canonical user Skill cannot be integrity checked: %s" % exc,
            status="BLOCKED",
        )]

    pairs = []
    for name in CANONICAL_SCRIPT_NAMES:
        pairs.append(("scripts/%s" % name, canonical_scripts / name))
    for name in CANONICAL_SCHEMA_NAMES:
        pairs.append((".harness/schemas/%s" % name, canonical_assets / name))
    for relative, canonical_path in pairs:
        try:
            if canonical_path.is_symlink():
                raise HarnessError("canonical file is a symlink")
            canonical_digest, canonical_size = _hash_regular_file(canonical_path, 32 * 1024 * 1024)
        except (HarnessError, OSError) as exc:
            issues.append(issue(
                "CANONICAL_SKILL_FILE_INVALID",
                "canonical Skill file %s cannot be trusted: %s" % (canonical_path, exc),
                status="BLOCKED", path=canonical_path,
            ))
            continue
        try:
            project_path = _safe_repo_path(repo, relative, must_exist=True)
            project_digest, project_size = _hash_regular_file(project_path, 32 * 1024 * 1024)
        except (HarnessError, OSError) as exc:
            issues.append(issue(
                "CANONICAL_PROJECT_FILE_MISSING",
                "required canonical project file %s is unavailable: %s" % (relative, exc),
                path=relative,
            ))
            continue
        if project_digest != canonical_digest or project_size != canonical_size:
            issues.append(issue(
                "CANONICAL_PROJECT_FILE_MISMATCH",
                "project file %s differs from the canonical user Skill" % relative,
                path=relative,
                details={"expected_sha256": canonical_digest, "observed_sha256": project_digest},
            ))
    return issues


def _toml_table_scalars(path, table_name):
    """Read only primitive scalar spellings from one TOML table without exposing values."""
    path = Path(path)
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise HarnessError("required TOML file is unavailable: %s" % exc)
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise HarnessError("required TOML path is not a safe regular file")
    raw = path.read_bytes()
    if len(raw) > 4 * 1024 * 1024:
        raise HarnessError("required TOML file exceeds 4 MiB")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HarnessError("required TOML file is not UTF-8: %s" % exc)
    active = False
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        table = re.match(r"^\[([^\]]+)\]\s*(?:#.*)?$", stripped)
        if table:
            active = table.group(1) == table_name
            continue
        if not active:
            continue
        scalar = re.match(r"^([A-Za-z0-9_-]+)\s*=\s*(.*?)\s*(?:#.*)?$", stripped)
        if scalar:
            values[scalar.group(1)] = scalar.group(2).strip()
    return values


def _robot_orchestration_issues(repo, config, indexes):
    """Verify the trusted user-level robot installation for projects that require it."""
    requirement_ids = set((indexes or {}).get("requirements", {}))
    required = bool(config.get("automatic_subagent_orchestration")) or any(
        requirement_id.startswith("REQ-ROBOT-") for requirement_id in requirement_ids
    )
    if not required:
        return []
    issues = []
    try:
        project_config_path = _safe_repo_path(
            repo, ".codex/config.toml", must_exist=True
        )
        project_features = _toml_table_scalars(project_config_path, "features")
        project_agents = _toml_table_scalars(project_config_path, "agents")
        if project_features.get("hooks") != "true":
            raise HarnessError("project [features].hooks is not true")
        if project_features.get("multi_agent") != "false":
            raise HarnessError(
                "project [features].multi_agent must be false because native "
                "desktop collaboration tools can bypass project Hooks"
            )
        if project_agents.get("enabled") != "true":
            raise HarnessError(
                "project [agents].enabled is not true for installed role metadata"
            )
    except (HarnessError, OSError) as exc:
        issues.append(issue(
            "ROBOT_PROJECT_CONFIG_INVALID",
            "project configuration does not force guarded-launcher-only orchestration: %s" % exc,
            status="BLOCKED",
            requirement_ids=["REQ-ROBOT-CONFIG"],
            test_case_ids=["TC-ROBOT-CONFIG"],
        ))
    configured_home = os.environ.get("CODEX_HOME")
    codex_home = (
        Path(configured_home).expanduser()
        if configured_home
        else Path.home() / ".codex"
    )
    codex_home = Path(os.path.abspath(str(codex_home)))
    try:
        values = _toml_table_scalars(codex_home / "config.toml", "agents")
        if values.get("enabled") != "true":
            raise HarnessError("[agents].enabled is not true")
        try:
            concurrency = int(values.get("max_concurrent_threads_per_session", "0"))
        except ValueError:
            concurrency = 0
        if concurrency < 3:
            raise HarnessError("[agents].max_concurrent_threads_per_session is below 3")
        if values.get("interrupt_message") != "true":
            raise HarnessError("[agents].interrupt_message is not true")
    except (HarnessError, OSError) as exc:
        issues.append(issue(
            "ROBOT_USER_CONFIG_INVALID",
            "effective Codex user configuration is not ready for automatic subagents: %s" % exc,
            status="BLOCKED",
            requirement_ids=["REQ-ROBOT-CONFIG"],
            test_case_ids=["TC-ROBOT-CONFIG"],
        ))

    expected_roles = {
        "architecture_auditor", "backend_worker", "database_worker",
        "file_pipeline_worker", "frontend_worker", "release_judge",
        "requirements_auditor", "runtime_acceptance", "security_reviewer",
        "test_engineer",
    }
    read_only_roles = {
        "architecture_auditor", "release_judge", "requirements_auditor",
        "security_reviewer",
    }
    agent_root = codex_home / "agents"
    for role in sorted(expected_roles):
        path = agent_root / (role + ".toml")
        try:
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise HarnessError("definition is not a safe regular file")
            text = path.read_text(encoding="utf-8")
            name = re.search(r'(?m)^name\s*=\s*"([^"]+)"\s*$', text)
            description = re.search(r'(?m)^description\s*=\s*"([^"]+)"\s*$', text)
            instructions = re.search(r'(?ms)^developer_instructions\s*=\s*"""(.+?)"""\s*$', text)
            sandbox = re.search(r'(?m)^sandbox_mode\s*=\s*"([^"]+)"\s*$', text)
            if not name or name.group(1) != role:
                raise HarnessError("name does not match the file role")
            if not description or not description.group(1).strip():
                raise HarnessError("description is missing")
            if not instructions or not instructions.group(1).strip():
                raise HarnessError("developer_instructions is missing")
            if re.search(r"(?m)^model(?:_reasoning_effort)?\s*=", text):
                raise HarnessError("model or reasoning effort is hard-coded")
            if role in read_only_roles and (not sandbox or sandbox.group(1) != "read-only"):
                raise HarnessError("read-only role lacks sandbox_mode=read-only")
            if role == "runtime_acceptance" and (
                not sandbox or sandbox.group(1) != "workspace-write"
            ):
                raise HarnessError("runtime acceptance lacks workspace-write evidence sandbox")
        except (HarnessError, OSError, UnicodeError) as exc:
            issues.append(issue(
                "ROBOT_AGENT_DEFINITION_INVALID",
                "user agent %s is unavailable or invalid: %s" % (role, exc),
                status="BLOCKED",
            ))

    policy_checks = (
        (codex_home / "AGENTS.md", (
            "Automatic subagent orchestration", "harness_authorize_subagent.py",
            "harness_run_subagent.py", "Do not ask the user to press Enter or answer Yes/No", "PreToolUse",
        )),
        (CANONICAL_SKILL_ROOT / "SKILL.md", (
            "Automatic subagent authorization", "harness_authorize_subagent.py",
            "harness_run_subagent.py", "--auto-trust-canonical-hooks", "PreToolUse",
        )),
    )
    for path, phrases in policy_checks:
        try:
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise HarnessError("policy is not a safe regular file")
            text = path.read_text(encoding="utf-8")
            missing = [phrase for phrase in phrases if phrase not in text]
            if missing:
                raise HarnessError("missing required policy text: %s" % ", ".join(missing))
        except (HarnessError, OSError, UnicodeError) as exc:
            issues.append(issue(
                "ROBOT_POLICY_INVALID",
                "user-level automatic orchestration policy is unavailable or invalid: %s" % exc,
                status="BLOCKED",
            ))
    try:
        import harness_init as _harness_init_runtime

        installer = _harness_init_runtime.Installer(Path(repo))
        installer.verify_hooks()
        hook_verification = installer.hook_verification
        if hook_verification.get("status") != "PASS" or installer.blockers:
            raise HarnessError(
                "live hooks/list did not confirm all canonical Hooks as recognized, enabled, "
                "trusted, and error-free: %s"
                % json.dumps(hook_verification, ensure_ascii=False, sort_keys=True)
            )
    except (HarnessError, ImportError, OSError, RuntimeError, ValueError) as exc:
        issues.append(issue(
            "ROBOT_LIVE_HOOKS_INVALID",
            "Codex live Hook readiness is unavailable or invalid: %s" % exc,
            status="BLOCKED",
            requirement_ids=["REQ-ROBOT-HOOKS"],
            test_case_ids=["TC-ROBOT-HOOKS"],
        ))
    return issues


def json_pointer(path_parts):
    if not path_parts:
        return ""
    return "/" + "/".join(str(x).replace("~", "~0").replace("/", "~1") for x in path_parts)


def issue(code, message, status="FAIL", path=None, requirement_ids=None, test_case_ids=None, details=None):
    result = {
        "code": str(code),
        "message": str(message),
        "status": status if status in STATUS_VALUES else "FAIL",
    }
    if path is not None:
        result["path"] = str(path)
    if requirement_ids:
        result["requirement_ids"] = sorted(set(requirement_ids))
    if test_case_ids:
        result["test_case_ids"] = sorted(set(test_case_ids))
    if details is not None:
        result["details"] = details
    return result


def worst_status(statuses):
    values = [status for status in statuses if status in STATUS_RANK]
    if not values:
        return "PASS"
    return max(values, key=lambda item: STATUS_RANK[item])


def is_integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value):
    return (is_integer(value) or isinstance(value, float)) and not (
        isinstance(value, float) and not math.isfinite(value)
    )


def json_equal(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(json_equal(a, b) for a, b in zip(left, right))
    return left == right


def parse_datetime(value):
    if not isinstance(value, str) or not value:
        raise ValueError("not a non-empty date-time string")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = _dt.datetime.fromisoformat(candidate)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("date-time lacks an explicit timezone")
    return parsed


class StrictSchemaValidator(object):
    """Finite strict JSON Schema Draft 2020-12 profile."""

    def __init__(self, schema, source="<schema>", registry=None):
        self.schema = schema
        self.source = str(source)
        self.registry = dict(registry or {})
        self.registry.setdefault(self.source, schema)
        if isinstance(schema, dict) and isinstance(schema.get("$id"), str):
            self.registry[schema["$id"]] = schema
        self.profile_errors = []
        self._lint_schema(schema, [], context="schema")

    def _profile_error(self, parts, message):
        self.profile_errors.append("%s%s: %s" % (self.source, json_pointer(parts), message))

    def _lint_schema(self, schema, parts, context="schema"):
        if isinstance(schema, bool):
            return
        if not isinstance(schema, dict):
            self._profile_error(parts, "schema must be an object or boolean")
            return
        for key in schema:
            if key not in KNOWN_SCHEMA_KEYS:
                self._profile_error(parts + [key], "unsupported schema keyword")
        draft = schema.get("$schema")
        if draft is not None and draft.rstrip("#/") != "https://json-schema.org/draft/2020-12/schema":
            self._profile_error(parts + ["$schema"], "only Draft 2020-12 is accepted")
        schema_maps = ("$defs", "properties", "patternProperties")
        for keyword in schema_maps:
            value = schema.get(keyword)
            if value is not None:
                if not isinstance(value, dict):
                    self._profile_error(parts + [keyword], "%s must be an object" % keyword)
                else:
                    for name, child in value.items():
                        self._lint_schema(child, parts + [keyword, name])
        schema_lists = ("allOf", "anyOf", "oneOf", "prefixItems")
        for keyword in schema_lists:
            value = schema.get(keyword)
            if value is not None:
                if not isinstance(value, list):
                    self._profile_error(parts + [keyword], "%s must be an array" % keyword)
                else:
                    for index, child in enumerate(value):
                        self._lint_schema(child, parts + [keyword, index])
        for keyword in ("additionalProperties", "propertyNames", "items", "contains", "not", "if", "then", "else"):
            if keyword in schema:
                self._lint_schema(schema[keyword], parts + [keyword])
        fmt = schema.get("format")
        if fmt is not None and fmt not in KNOWN_FORMATS:
            self._profile_error(parts + ["format"], "unsupported asserted format: %s" % fmt)
        pattern = schema.get("pattern")
        if pattern is not None:
            if not isinstance(pattern, str):
                self._profile_error(parts + ["pattern"], "pattern must be a string")
            else:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    self._profile_error(parts + ["pattern"], "invalid pattern: %s" % exc)

    def validate(self, instance):
        if self.profile_errors:
            return list(self.profile_errors)
        errors = []
        self._validate(self.schema, instance, [], errors, self.schema, set())
        return errors

    def _resolve_ref(self, ref, root_schema):
        if not isinstance(ref, str) or not ref:
            raise HarnessError("$ref must be a non-empty string")
        base, fragment = (ref.split("#", 1) + [""])[:2] if "#" in ref else (ref, "")
        if base:
            if base not in self.registry:
                basename = Path(unquote(urlparse(base).path)).name
                matches = [value for key, value in self.registry.items() if Path(unquote(urlparse(str(key)).path)).name == basename]
                if len(matches) != 1:
                    raise HarnessError("remote or unknown $ref is forbidden: %s" % ref)
                target = matches[0]
            else:
                target = self.registry[base]
        else:
            target = root_schema
        if not fragment:
            return target
        if not fragment.startswith("/"):
            anchor = fragment
            found = self._find_anchor(target, anchor)
            if found is None:
                raise HarnessError("unresolved schema anchor: %s" % ref)
            return found
        current = target
        for raw_part in fragment[1:].split("/"):
            if re.search(r"~(?![01])", raw_part):
                raise HarnessError("invalid JSON Pointer escape in $ref: %s" % ref)
            part = unquote(raw_part).replace("~1", "/").replace("~0", "~")
            if isinstance(current, list):
                if not part.isdigit() or int(part) >= len(current):
                    raise HarnessError("unresolved array JSON Pointer in $ref: %s" % ref)
                current = current[int(part)]
            elif isinstance(current, dict) and part in current:
                current = current[part]
            else:
                raise HarnessError("unresolved JSON Pointer in $ref: %s" % ref)
        return current

    def _find_anchor(self, schema, anchor):
        if isinstance(schema, dict):
            if schema.get("$anchor") == anchor:
                return schema
            for value in schema.values():
                found = self._find_anchor(value, anchor)
                if found is not None:
                    return found
        elif isinstance(schema, list):
            for value in schema:
                found = self._find_anchor(value, anchor)
                if found is not None:
                    return found
        return None

    def _branch_valid(self, schema, instance, parts, root_schema, ref_stack):
        branch_errors = []
        self._validate(schema, instance, parts, branch_errors, root_schema, set(ref_stack))
        return not branch_errors

    def _error(self, errors, parts, message):
        errors.append("%s: %s" % (json_pointer(parts) or "/", message))

    def _validate(self, schema, instance, parts, errors, root_schema, ref_stack):
        if schema is False:
            self._error(errors, parts, "boolean schema rejects the value")
            return
        if schema is True:
            return
        if not isinstance(schema, dict):
            self._error(errors, parts, "invalid non-object schema")
            return
        if "$ref" in schema:
            try:
                target = self._resolve_ref(schema["$ref"], root_schema)
            except HarnessError as exc:
                self._error(errors, parts, str(exc))
                return
            marker = (id(target), id(instance))
            if marker in ref_stack:
                return
            ref_stack.add(marker)
            self._validate(target, instance, parts, errors, target if schema["$ref"].split("#", 1)[0] else root_schema, ref_stack)
            ref_stack.discard(marker)

        if "type" in schema:
            expected = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            if not any(self._type_matches(name, instance) for name in expected):
                self._error(errors, parts, "expected type %s, got %s" % (expected, self._json_type(instance)))
                return
        if "const" in schema and not json_equal(instance, schema["const"]):
            self._error(errors, parts, "value does not equal const")
        if "enum" in schema and not any(json_equal(instance, option) for option in schema["enum"]):
            self._error(errors, parts, "value is not in enum")

        for keyword in ("allOf", "anyOf", "oneOf"):
            if keyword not in schema:
                continue
            branches = schema[keyword]
            matches = sum(1 for branch in branches if self._branch_valid(branch, instance, parts, root_schema, ref_stack))
            if keyword == "allOf" and matches != len(branches):
                for branch in branches:
                    self._validate(branch, instance, parts, errors, root_schema, set(ref_stack))
            elif keyword == "anyOf" and matches == 0:
                self._error(errors, parts, "value matches no anyOf branch")
            elif keyword == "oneOf" and matches != 1:
                self._error(errors, parts, "value must match exactly one oneOf branch; matched %d" % matches)
        if "not" in schema and self._branch_valid(schema["not"], instance, parts, root_schema, ref_stack):
            self._error(errors, parts, "value matches forbidden not schema")
        if "if" in schema:
            branch = schema.get("then") if self._branch_valid(schema["if"], instance, parts, root_schema, ref_stack) else schema.get("else")
            if branch is not None:
                self._validate(branch, instance, parts, errors, root_schema, set(ref_stack))

        if isinstance(instance, dict):
            self._validate_object(schema, instance, parts, errors, root_schema, ref_stack)
        elif isinstance(instance, list):
            self._validate_array(schema, instance, parts, errors, root_schema, ref_stack)
        elif isinstance(instance, str):
            self._validate_string(schema, instance, parts, errors)
        elif is_number(instance):
            self._validate_number(schema, instance, parts, errors)

    def _type_matches(self, expected, value):
        return {
            "null": value is None,
            "boolean": isinstance(value, bool),
            "integer": is_integer(value),
            "number": is_number(value),
            "string": isinstance(value, str),
            "array": isinstance(value, list),
            "object": isinstance(value, dict),
        }.get(expected, False)

    def _json_type(self, value):
        for name in ("null", "boolean", "integer", "number", "string", "array", "object"):
            if self._type_matches(name, value):
                return name
        return type(value).__name__

    def _validate_object(self, schema, value, parts, errors, root_schema, ref_stack):
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            self._error(errors, parts, "object has fewer than minProperties")
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            self._error(errors, parts, "object has more than maxProperties")
        for name in schema.get("required", []):
            if name not in value:
                self._error(errors, parts, "missing required property %r" % name)
        for name, required_names in schema.get("dependentRequired", {}).items():
            if name in value:
                for required_name in required_names:
                    if required_name not in value:
                        self._error(errors, parts, "property %r requires %r" % (name, required_name))
        properties = schema.get("properties", {})
        patterns = schema.get("patternProperties", {})
        matched = set()
        for name, item in value.items():
            if name in properties:
                matched.add(name)
                self._validate(properties[name], item, parts + [name], errors, root_schema, set(ref_stack))
            for pattern, child_schema in patterns.items():
                if re.search(pattern, name):
                    matched.add(name)
                    self._validate(child_schema, item, parts + [name], errors, root_schema, set(ref_stack))
        additional = schema.get("additionalProperties", True)
        for name in value:
            if name in matched:
                continue
            if additional is False:
                self._error(errors, parts + [name], "additional property is forbidden")
            elif isinstance(additional, (dict, bool)):
                self._validate(additional, value[name], parts + [name], errors, root_schema, set(ref_stack))
        if "propertyNames" in schema:
            for name in value:
                self._validate(schema["propertyNames"], name, parts + [name], errors, root_schema, set(ref_stack))

    def _validate_array(self, schema, value, parts, errors, root_schema, ref_stack):
        if "minItems" in schema and len(value) < schema["minItems"]:
            self._error(errors, parts, "array has fewer than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            self._error(errors, parts, "array has more than maxItems")
        if schema.get("uniqueItems"):
            for index, item in enumerate(value):
                if any(json_equal(item, previous) for previous in value[:index]):
                    self._error(errors, parts + [index], "array item is not unique")
        prefix = schema.get("prefixItems", [])
        for index, child_schema in enumerate(prefix[:len(value)]):
            self._validate(child_schema, value[index], parts + [index], errors, root_schema, set(ref_stack))
        if "items" in schema:
            for index in range(len(prefix), len(value)):
                self._validate(schema["items"], value[index], parts + [index], errors, root_schema, set(ref_stack))
        if "contains" in schema:
            count = sum(1 for item in value if self._branch_valid(schema["contains"], item, parts, root_schema, ref_stack))
            minimum = schema.get("minContains", 1)
            maximum = schema.get("maxContains")
            if count < minimum or (maximum is not None and count > maximum):
                self._error(errors, parts, "contains matched %d items" % count)

    def _validate_string(self, schema, value, parts, errors):
        if "minLength" in schema and len(value) < schema["minLength"]:
            self._error(errors, parts, "string is shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            self._error(errors, parts, "string is longer than maxLength")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            self._error(errors, parts, "string does not match pattern")
        if "format" in schema and not self._format_valid(schema["format"], value):
            self._error(errors, parts, "string is not a valid %s" % schema["format"])

    def _validate_number(self, schema, value, parts, errors):
        if "minimum" in schema and value < schema["minimum"]:
            self._error(errors, parts, "number is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            self._error(errors, parts, "number is above maximum")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            self._error(errors, parts, "number is not above exclusiveMinimum")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            self._error(errors, parts, "number is not below exclusiveMaximum")
        if "multipleOf" in schema:
            divisor = schema["multipleOf"]
            if not is_number(divisor) or divisor <= 0:
                self._error(errors, parts, "schema multipleOf is invalid")
            else:
                quotient = value / divisor
                if abs(quotient - round(quotient)) > 1e-12:
                    self._error(errors, parts, "number is not a multipleOf")

    def _format_valid(self, fmt, value):
        try:
            if fmt == "date-time":
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value) is None:
                    return False
                parse_datetime(value)
                return True
            if fmt == "date":
                _dt.date.fromisoformat(value)
                return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
            if fmt == "time":
                candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
                parsed = _dt.time.fromisoformat(candidate)
                return parsed.tzinfo is not None
            if fmt == "duration":
                return re.fullmatch(r"P(?!$)(?:\d+Y)?(?:\d+M)?(?:\d+D)?(?:T(?=\d)(?:\d+H)?(?:\d+M)?(?:\d+(?:\.\d+)?S)?)?", value) is not None
            if fmt in ("email", "idn-email"):
                return re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value) is not None
            if fmt in ("hostname", "idn-hostname"):
                encoded = value.encode("idna").decode("ascii")
                return len(encoded) <= 253 and all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in encoded.rstrip(".").split("."))
            if fmt in ("ipv4", "ipv6"):
                parsed = ipaddress.ip_address(value)
                return (fmt == "ipv4" and parsed.version == 4) or (fmt == "ipv6" and parsed.version == 6)
            if fmt == "uuid":
                return str(uuid.UUID(value)).lower() == value.lower()
            if fmt == "uri":
                return bool(urlparse(value).scheme) and " " not in value
            if fmt == "uri-reference":
                urlparse(value)
                return " " not in value
            if fmt == "regex":
                re.compile(value)
                return True
            if fmt == "json-pointer":
                return value == "" or (value.startswith("/") and re.search(r"~(?![01])", value) is None)
            if fmt == "relative-json-pointer":
                return re.fullmatch(r"(?:0|[1-9]\d*)(?:#|(?:/(?:[^~/]|~[01])*)*)", value) is not None
        except (ValueError, TypeError, re.error, UnicodeError):
            return False
        return False


def find_repo(start=None):
    candidate = Path(start or os.getcwd()).expanduser().resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for directory in (candidate,) + tuple(candidate.parents):
        harness_path = directory / ".harness"
        if harness_path.is_symlink():
            raise HarnessError("repository .harness directory must not be a symlink: %s" % harness_path)
        if harness_path.is_dir():
            _assert_harness_layout_safe(directory)
            return directory
    git = shutil.which("git")
    if git:
        completed = subprocess.run(
            [git, "-C", str(candidate), "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            resolved = Path(completed.stdout.strip()).resolve()
            harness_path = resolved / ".harness"
            if harness_path.exists() or harness_path.is_symlink():
                _assert_harness_layout_safe(resolved)
            return resolved
    return candidate


def schema_search_paths(repo, schema_name):
    script_root = Path(__file__).resolve().parent.parent
    candidates = [
        Path(repo) / ".harness" / "schemas" / schema_name,
        script_root / ".harness" / "schemas" / schema_name,
        script_root / "assets" / schema_name,
    ]
    configured_root = os.environ.get("HARNESS_SCHEMA_ROOT")
    if configured_root:
        candidates.insert(1, Path(configured_root).expanduser() / schema_name)
    candidates.append(Path.home() / ".agents" / "skills" / "software-delivery-harness" / "assets" / schema_name)
    result = []
    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return tuple(result)


def find_schema(repo, schema_name):
    for path in schema_search_paths(repo, schema_name):
        if path.is_file():
            return path
    raise HarnessError("schema is missing from project and installed skill: %s" % schema_name)


def find_project_schema(repo, schema_name):
    path = Path(repo) / ".harness" / "schemas" / schema_name
    if not path.is_file():
        raise HarnessError("required project schema is missing: %s" % path)
    return path


def load_config(repo):
    path = Path(repo) / ".harness" / "config.json"
    if not path.exists():
        raise HarnessError("required .harness/config.json is missing")
    path = _safe_repo_path(repo, ".harness/config.json", must_exist=True)
    value = strict_json_load(path)
    if not isinstance(value, dict):
        raise HarnessError(".harness/config.json must contain an object")
    return value


def harden_config(config):
    """Return fail-closed effective configuration and deterministic weakening issues."""
    raw = dict(config or {})
    effective = dict(raw)
    issues = []

    project_name = raw.get("project_name")
    if project_name is not None and (
        not isinstance(project_name, str) or not project_name.strip() or len(project_name.strip()) > 256
    ):
        issues.append(issue("CONFIG_PROJECT_NAME_INVALID", "project_name must be a non-empty string of at most 256 characters"))
        effective["project_name"] = "invalid-project-name"

    boolean_truth_fields = ("strict", "git_required", "baseline_required", "group_nodes_require_children")
    for name in boolean_truth_fields:
        if name in raw and raw.get(name) is not True:
            issues.append(issue(
                "CONFIG_TRUTH_WEAKENED",
                "%s must be true and cannot be weakened by project configuration" % name,
                details={"field": name, "observed": raw.get(name)},
            ))
        effective[name] = True

    allowed = raw.get("allowed_statuses")
    if allowed is not None and allowed != list(STATUS_VALUES):
        issues.append(issue(
            "CONFIG_STATUS_VOCABULARY_WEAKENED",
            "allowed_statuses must equal the four Harness truth states in canonical order",
            details={"observed": allowed, "required": list(STATUS_VALUES)},
        ))
    effective["allowed_statuses"] = list(STATUS_VALUES)

    automatic_orchestration = raw.get("automatic_subagent_orchestration", False)
    if "automatic_subagent_orchestration" in raw and automatic_orchestration is not True:
        issues.append(issue(
            "CONFIG_AUTOMATIC_SUBAGENTS_WEAKENED",
            "automatic_subagent_orchestration must be true when declared",
        ))
        automatic_orchestration = True
    effective["automatic_subagent_orchestration"] = automatic_orchestration is True

    depth = raw.get("required_hierarchy_depth", MINIMUM_HIERARCHY_DEPTH)
    if not is_integer(depth) or depth < MINIMUM_HIERARCHY_DEPTH:
        issues.append(issue(
            "CONFIG_HIERARCHY_DEPTH_WEAKENED",
            "required_hierarchy_depth must be an integer of at least %d" % MINIMUM_HIERARCHY_DEPTH,
            details={"observed": depth},
        ))
        depth = MINIMUM_HIERARCHY_DEPTH
    effective["required_hierarchy_depth"] = depth

    for list_name in ("source_scan_paths", "scan_excludes"):
        values = raw.get(list_name, [])
        if not isinstance(values, list) or any(
            not isinstance(item, str) or not item.strip() for item in values
        ):
            issues.append(issue(
                "CONFIG_%s_INVALID" % list_name.upper(),
                "%s must be a list of non-empty repository-relative paths" % list_name,
            ))
            values = []
        effective[list_name] = values
    if "." not in effective["source_scan_paths"]:
        issues.append(issue(
            "CONFIG_SOURCE_SCAN_ROOT_WEAKENED",
            "source_scan_paths must include '.' so committed implementation and tests cannot be hidden",
        ))
        effective["source_scan_paths"] = ["."] + list(effective["source_scan_paths"])

    heuristics = raw.get("heuristics")
    if heuristics is None:
        heuristics = {}
    if not isinstance(heuristics, dict):
        issues.append(issue("CONFIG_HEURISTICS_INVALID", "heuristics must be an object"))
        heuristics = {}
    else:
        heuristics = dict(heuristics)
    if "strict_unresolved_critical_findings" in heuristics and heuristics.get("strict_unresolved_critical_findings") is not True:
        issues.append(issue(
            "CONFIG_HEURISTICS_WEAKENED",
            "heuristics.strict_unresolved_critical_findings must be true",
        ))
    heuristics["strict_unresolved_critical_findings"] = True
    exclude_paths = heuristics.get("exclude_paths", [])
    if not isinstance(exclude_paths, list) or any(not isinstance(item, str) or not item.strip() for item in exclude_paths):
        issues.append(issue("CONFIG_HEURISTIC_EXCLUDES_INVALID", "heuristics.exclude_paths must be a list of non-empty strings"))
        exclude_paths = []
    heuristics["exclude_paths"] = exclude_paths
    effective["heuristics"] = heuristics
    checks = raw.get("checks", {})
    if not isinstance(checks, dict):
        issues.append(issue("CONFIG_CHECKS_INVALID", "checks must be an object"))
        checks = {}
    for check_name in sorted(CANONICAL_CHECK_NAMES - set(checks)):
        issues.append(issue("CONFIG_CHECK_MISSING", "checks.%s must be explicitly declared" % check_name))
    for check_name in sorted(set(checks) - CANONICAL_CHECK_NAMES):
        issues.append(issue("CONFIG_CHECK_UNKNOWN", "unknown configured check: %s" % check_name))
    for check_name, check in checks.items():
        if not isinstance(check, dict) or not isinstance(check.get("applicable"), bool):
            issues.append(issue("CONFIG_CHECK_INVALID", "check %s must declare boolean applicable" % check_name))
            continue
        command = check.get("command")
        reason = check.get("reason")
        if check["applicable"] is True and (not isinstance(command, str) or not command.strip()):
            issues.append(issue("CONFIG_APPLICABLE_CHECK_COMMAND_MISSING", "applicable check %s lacks a command" % check_name))
        if check["applicable"] is False:
            if command is not None:
                issues.append(issue("CONFIG_INAPPLICABLE_CHECK_COMMAND_INVALID", "inapplicable check %s must have command null" % check_name))
            if not isinstance(reason, str) or not reason.strip():
                issues.append(issue("CONFIG_INAPPLICABLE_CHECK_REASON_MISSING", "inapplicable check %s lacks a reason" % check_name))
    effective["checks"] = checks
    return effective, issues


def extract_array(document, preferred_key):
    """Extract an artifact array after schema validation.

    Bare arrays and a small set of legacy key spellings are accepted for semantic
    inspection so a malformed root still produces useful errors.  The schema is
    always evaluated against the original document and remains authoritative.
    """
    if isinstance(document, list):
        return document
    if not isinstance(document, dict):
        return []
    candidates = [
        preferred_key,
        preferred_key.replace("_", "-"),
        preferred_key.replace("_", ""),
        "items",
    ]
    for key in candidates:
        value = document.get(key)
        if isinstance(value, list):
            return value
    return []


def load_and_validate_artifacts(repo, include_report=False):
    repo = Path(repo)
    documents = {}
    arrays = {}
    schemas = {}
    issues = []
    for artifact_name, filename, schema_name, root_key in ARTIFACTS:
        collected_name = COLLECTED_RUNTIME_ARTIFACTS.get(artifact_name)
        collected_path = repo / ".harness" / "state" / collected_name if collected_name else None
        relative_data_path = (
            ".harness/state/%s" % collected_name
            if collected_path is not None and (collected_path.exists() or collected_path.is_symlink())
            else ".harness/spec/%s" % filename
        )
        data_path = repo / relative_data_path
        try:
            data_path = _safe_repo_path(repo, relative_data_path, must_exist=True)
            document = strict_json_load(data_path)
            documents[artifact_name] = document
        except HarnessError as exc:
            documents[artifact_name] = None
            arrays[artifact_name] = []
            issues.append(issue("JSON_READ_ERROR", str(exc), path=data_path))
            continue
        try:
            schema_path = find_project_schema(repo, schema_name)
            schema = strict_json_load(schema_path)
            schemas[artifact_name] = schema
            validator = StrictSchemaValidator(schema, source=str(schema_path))
            for message in validator.validate(document):
                issues.append(issue("SCHEMA_VALIDATION_ERROR", message, path=data_path))
        except HarnessError as exc:
            issues.append(issue("SCHEMA_ENGINE_ERROR", str(exc), path=data_path))
        arrays[artifact_name] = extract_array(document, root_key)
    if include_report:
        report_path = repo / ".harness" / "release-report.json"
        if report_path.exists():
            try:
                document = strict_json_load(report_path)
                schema_path = find_project_schema(repo, "release-report.schema.json")
                schema = strict_json_load(schema_path)
                validator = StrictSchemaValidator(schema, source=str(schema_path))
                for message in validator.validate(document):
                    issues.append(issue("REPORT_SCHEMA_ERROR", message, path=report_path))
            except HarnessError as exc:
                issues.append(issue("REPORT_SCHEMA_ENGINE_ERROR", str(exc), path=report_path))
    all_state_runtime_paths = [".harness/state/%s" % filename for filename in COLLECTED_RUNTIME_ARTIFACTS.values()]
    if any((repo / relative).exists() or (repo / relative).is_symlink() for relative in all_state_runtime_paths):
        state_runtime_paths = all_state_runtime_paths
        try:
            manifest_path = _safe_repo_path(repo, ".harness/state/collection-manifest.json", must_exist=True)
            manifest = strict_json_load(manifest_path)
            if not isinstance(manifest, dict) or not isinstance(manifest.get("artifacts"), list):
                raise HarnessError("collection manifest is malformed")
            if manifest.get("standard_version") != STANDARD_VERSION:
                raise HarnessError("collection manifest standard_version is unsupported")
            validate_run_id(manifest.get("run_id"))
            if re.fullmatch(r"[0-9a-f]{40,64}", str(manifest.get("git_commit") or "")) is None:
                raise HarnessError("collection manifest git_commit is invalid")
            entries = {
                item.get("path"): item for item in manifest["artifacts"]
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            }
            if len(entries) != len(manifest["artifacts"]) or set(entries) != set(state_runtime_paths):
                raise HarnessError("collection manifest does not exactly cover runtime state files")
            for relative in state_runtime_paths:
                target = _safe_repo_path(repo, relative, must_exist=True)
                digest, size = _hash_regular_file(target, 32 * 1024 * 1024)
                if entries[relative].get("sha256") != digest or entries[relative].get("bytes") != size:
                    raise HarnessError("collection manifest hash/size mismatch for %s" % relative)
            documents["_collection_manifest"] = manifest
        except (HarnessError, OSError) as exc:
            issues.append(issue("COLLECTION_MANIFEST_INVALID", str(exc)))
    return {
        "documents": documents,
        "arrays": arrays,
        "schemas": schemas,
        "issues": issues,
    }


def _id_index(items, id_keys, label, issues):
    result = {}
    for position, item_value in enumerate(items):
        if not isinstance(item_value, dict):
            issues.append(issue("INVALID_%s" % label.upper(), "%s item %d is not an object" % (label, position)))
            continue
        item_id = None
        for key in id_keys:
            if isinstance(item_value.get(key), str) and item_value[key]:
                item_id = item_value[key]
                break
        if item_id is None:
            issues.append(issue("MISSING_%s_ID" % label.upper(), "%s item %d has no ID" % (label, position)))
            continue
        if item_id in result:
            issues.append(issue("DUPLICATE_%s_ID" % label.upper(), "duplicate %s ID: %s" % (label, item_id)))
        else:
            result[item_id] = item_value
    return result


def _list_of_strings(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _criterion_ids(requirement):
    result = []
    for criterion in requirement.get("acceptance_criteria", []) if isinstance(requirement, dict) else []:
        if isinstance(criterion, dict) and isinstance(criterion.get("criterion_id"), str):
            result.append(criterion["criterion_id"])
        elif isinstance(criterion, str):
            result.append(criterion)
    return result


def _graph_cycles(graph):
    visiting = set()
    visited = set()
    stack = []
    cycles = []

    def walk(node):
        if node in visiting:
            try:
                start = stack.index(node)
            except ValueError:
                start = 0
            cycle = stack[start:] + [node]
            if cycle not in cycles:
                cycles.append(cycle)
            return
        if node in visited:
            return
        visiting.add(node)
        stack.append(node)
        for child in graph.get(node, []):
            if child in graph:
                walk(child)
        stack.pop()
        visiting.discard(node)
        visited.add(node)

    for graph_node in graph:
        walk(graph_node)
    return cycles


def _safe_repo_path(repo, value, must_exist=False):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise HarnessError("path must be a non-empty string without NUL")
    lexical = Path(value)
    if lexical.is_absolute() or any(part == ".." for part in lexical.parts):
        raise HarnessError("path must be repository-relative without '..': %s" % value)
    candidate = Path(repo) / lexical
    current = Path(repo).resolve()
    for part in lexical.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise HarnessError("symlink is forbidden in evidence/trace path: %s" % value)
    resolved = candidate.resolve(strict=must_exist)
    try:
        common = os.path.commonpath([str(Path(repo).resolve()), str(resolved)])
    except ValueError:
        raise HarnessError("path escapes repository: %s" % value)
    if common != str(Path(repo).resolve()):
        raise HarnessError("path escapes repository: %s" % value)
    return resolved


def _assert_git_head_file(repo, relative_path, max_bytes=32 * 1024 * 1024):
    """Prove a worktree file is the exact regular blob and mode recorded at HEAD.

    This intentionally does not trust ``git status``: skip-worktree,
    assume-unchanged, ignored, and untracked paths must all fail the binding.
    """
    path = _safe_repo_path(repo, relative_path, must_exist=True)
    if not path.is_file() or path.is_symlink():
        raise HarnessError("Git-bound path is not a safe regular file: %s" % relative_path)
    git = shutil.which("git")
    if not git:
        raise HarnessError("Git is unavailable to bind %s to HEAD" % relative_path)
    relative = path.relative_to(Path(repo).resolve()).as_posix()
    flags = subprocess.run(
        [git, "-C", str(repo), "ls-files", "-v", "--", relative],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
    )
    flag_lines = [line for line in flags.stdout.decode("utf-8", "replace").splitlines() if line]
    if flags.returncode != 0 or len(flag_lines) != 1:
        raise HarnessError("path is not uniquely tracked by Git: %s" % relative)
    if not flag_lines[0].startswith("H "):
        raise HarnessError(
            "path has a skip-worktree/assume-unchanged or non-canonical index flag: %s" % relative
        )
    tree = subprocess.run(
        [git, "-C", str(repo), "ls-tree", "-z", "HEAD", "--", relative],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
    )
    if tree.returncode != 0:
        raise HarnessError("cannot read HEAD tree entry for %s" % relative)
    entries = [entry for entry in tree.stdout.split(b"\0") if entry]
    if len(entries) != 1 or b"\t" not in entries[0]:
        raise HarnessError("HEAD does not contain exactly one file entry for %s" % relative)
    metadata, encoded_name = entries[0].split(b"\t", 1)
    try:
        mode, object_type, object_id = metadata.decode("ascii").split()
        stored_name = encoded_name.decode("utf-8")
    except (UnicodeError, ValueError) as exc:
        raise HarnessError("invalid Git tree entry for %s: %s" % (relative, exc))
    if stored_name != relative or object_type != "blob" or mode not in {"100644", "100755"}:
        raise HarnessError("HEAD entry for %s is not a regular file blob" % relative)
    expected_executable = mode == "100755"
    actual_executable = bool(path.stat().st_mode & stat.S_IXUSR)
    if expected_executable != actual_executable:
        raise HarnessError("worktree executable mode differs from HEAD for %s" % relative)
    size_result = subprocess.run(
        [git, "-C", str(repo), "cat-file", "-s", object_id],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
    )
    try:
        blob_size = int(size_result.stdout.decode("ascii").strip())
    except (UnicodeError, ValueError):
        raise HarnessError("cannot determine HEAD blob size for %s" % relative)
    if size_result.returncode != 0 or blob_size > max_bytes:
        raise HarnessError("HEAD blob is unavailable or exceeds the safety limit for %s" % relative)
    blob_result = subprocess.run(
        [git, "-C", str(repo), "cat-file", "blob", object_id],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
    )
    if blob_result.returncode != 0 or len(blob_result.stdout) != blob_size:
        raise HarnessError("cannot read complete HEAD blob for %s" % relative)
    worktree_bytes = path.read_bytes()
    if worktree_bytes != blob_result.stdout:
        raise HarnessError("worktree bytes differ from HEAD for %s" % relative)
    return hashlib.sha256(worktree_bytes).hexdigest(), len(worktree_bytes)


def _python_symbol_spans(text, source_name):
    """Return definition spans for Python symbols; comments/strings never count."""
    try:
        tree = ast.parse(text, filename=source_name)
    except SyntaxError as exc:
        raise HarnessError("cannot parse Python implementation for symbol binding: %s" % exc)
    spans = {}

    def add(name, node):
        if not isinstance(name, str) or not name:
            return
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", start)
        if is_integer(start):
            spans.setdefault(name, []).append((start, end if is_integer(end) else start))

    def assigned_names(target):
        if isinstance(target, ast.Name):
            return [target.id]
        if isinstance(target, (ast.Tuple, ast.List)):
            return [name for item in target.elts for name in assigned_names(item)]
        return []

    def walk(node, prefix=()):
        child_prefix = prefix
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            qualified = ".".join(prefix + (node.name,))
            add(qualified, node)
            add(node.name, node)
            child_prefix = prefix + (node.name,)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                add(alias.asname or alias.name.split(".")[0], node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in assigned_names(target):
                    add(".".join(prefix + (name,)), node)
                    add(name, node)
        for child in ast.iter_child_nodes(node):
            walk(child, child_prefix)

    walk(tree)
    return spans


def validate_semantics(repo, arrays, config=None):
    config = config or {}
    issues = []
    requirements = _id_index(arrays.get("requirements", []), ("requirement_id",), "requirement", issues)
    changes = _id_index(arrays.get("change_ledger", []), ("change_id",), "change", issues)
    tests = _id_index(arrays.get("test_cases", []), ("test_case_id",), "test_case", issues)
    executions = _id_index(arrays.get("test_executions", []), ("execution_id", "test_execution_id"), "execution", issues)
    evidence = _id_index(arrays.get("evidence", []), ("evidence_id",), "evidence", issues)
    traces = _id_index(arrays.get("traceability", []), ("trace_id",), "trace", issues)
    acceptance = _id_index(arrays.get("acceptance_results", []), ("requirement_id",), "acceptance_result", issues)

    parent_graph = {}
    dependency_graph = {}
    children = {requirement_id: [] for requirement_id in requirements}
    all_criteria = {}
    criterion_criticality = {}
    all_contracts = {}
    for requirement_id, requirement in requirements.items():
        parent_id = requirement.get("parent_id")
        parent_graph[requirement_id] = [parent_id] if isinstance(parent_id, str) else []
        dependency_graph[requirement_id] = _list_of_strings(requirement.get("dependencies"))
        if parent_id is None:
            if requirement.get("level") != 1:
                issues.append(issue("ROOT_LEVEL_INVALID", "%s root must have level 1" % requirement_id, requirement_ids=[requirement_id]))
            expected_path = [requirement_id]
        elif parent_id not in requirements:
            issues.append(issue("MISSING_PARENT", "%s references missing parent %s" % (requirement_id, parent_id), requirement_ids=[requirement_id]))
            expected_path = None
        else:
            children[parent_id].append(requirement_id)
            parent = requirements[parent_id]
            expected_level = parent.get("level") + 1 if is_integer(parent.get("level")) else None
            if expected_level is not None and requirement.get("level") != expected_level:
                issues.append(issue("LEVEL_MISMATCH", "%s level does not equal parent level + 1" % requirement_id, requirement_ids=[requirement_id]))
            parent_path = parent.get("category_path")
            expected_path = list(parent_path) + [requirement_id] if isinstance(parent_path, list) else None
        if expected_path is not None and requirement.get("category_path") != expected_path:
            issues.append(issue("CATEGORY_PATH_MISMATCH", "%s category_path is not reconstructable from parents" % requirement_id, requirement_ids=[requirement_id]))
        if requirement.get("node_type") == "group" and not children.get(requirement_id):
            # Rechecked after all children have been populated below.
            pass
        for dependency in dependency_graph[requirement_id]:
            if dependency not in requirements:
                issues.append(issue("MISSING_DEPENDENCY", "%s references missing dependency %s" % (requirement_id, dependency), requirement_ids=[requirement_id]))
        for criterion_id in _criterion_ids(requirement):
            if criterion_id in all_criteria:
                issues.append(issue("DUPLICATE_CRITERION_ID", "duplicate acceptance criterion ID: %s" % criterion_id, requirement_ids=[requirement_id, all_criteria[criterion_id]]))
            else:
                all_criteria[criterion_id] = requirement_id
        for criterion in requirement.get("acceptance_criteria", []):
            if isinstance(criterion, dict) and isinstance(criterion.get("criterion_id"), str):
                criterion_criticality[criterion["criterion_id"]] = criterion.get("criticality")
        for field_name, id_name in (
            ("permissions", "permission_id"),
            ("ui_contract", "contract_id"),
            ("api_contract", "contract_id"),
            ("database_contract", "contract_id"),
            ("file_contract", "contract_id"),
        ):
            for contract in requirement.get(field_name, []):
                contract_id = contract.get(id_name) if isinstance(contract, dict) else None
                if not isinstance(contract_id, str):
                    continue
                if contract_id in all_contracts:
                    issues.append(issue(
                        "DUPLICATE_CONTRACT_ID",
                        "duplicate contract or permission ID: %s" % contract_id,
                        requirement_ids=[requirement_id, all_contracts[contract_id][0]],
                        details={"field": field_name, "first_field": all_contracts[contract_id][1]},
                    ))
                else:
                    all_contracts[contract_id] = (requirement_id, field_name)
        try:
            created_at = parse_datetime(requirement.get("created_at"))
            updated_at = parse_datetime(requirement.get("updated_at"))
            if updated_at < created_at:
                issues.append(issue("REQUIREMENT_TIME_INVALID", "%s updated_at precedes created_at" % requirement_id, requirement_ids=[requirement_id]))
        except (ValueError, TypeError):
            # The schema validator provides the precise date-time error.
            pass

    for requirement_id, requirement in requirements.items():
        if requirement.get("node_type") == "group" and not children.get(requirement_id):
            issues.append(issue("EMPTY_GROUP", "group requirement has no children: %s" % requirement_id, requirement_ids=[requirement_id]))
        if requirement.get("node_type") == "leaf" and children.get(requirement_id):
            issues.append(issue("LEAF_HAS_CHILDREN", "leaf requirement has children: %s" % requirement_id, requirement_ids=[requirement_id]))
        if requirement.get("node_type") == "leaf" and not requirement.get("acceptance_criteria"):
            issues.append(issue("LEAF_ACCEPTANCE_CRITERIA_MISSING", "leaf requirement must have at least one acceptance criterion: %s" % requirement_id, requirement_ids=[requirement_id]))
    for cycle in _graph_cycles(parent_graph):
        issues.append(issue("PARENT_CYCLE", "requirement parent cycle: %s" % " -> ".join(cycle), requirement_ids=cycle))
    for cycle in _graph_cycles(dependency_graph):
        issues.append(issue("DEPENDENCY_CYCLE", "requirement dependency cycle: %s" % " -> ".join(cycle), requirement_ids=cycle))

    required_depth = config.get("required_hierarchy_depth", MINIMUM_HIERARCHY_DEPTH)
    if not is_integer(required_depth) or required_depth < MINIMUM_HIERARCHY_DEPTH:
        required_depth = MINIMUM_HIERARCHY_DEPTH
    for requirement_id, requirement in requirements.items():
        level = requirement.get("level")
        if is_integer(level) and level < required_depth:
            if requirement.get("node_type") == "leaf":
                issues.append(issue("REQUIRED_DEPTH_EARLY_LEAF", "%s is a leaf before required level %d" % (requirement_id, required_depth), requirement_ids=[requirement_id]))
            if not children.get(requirement_id):
                issues.append(issue("REQUIRED_DEPTH_MISSING", "%s has no child needed to reach level %d" % (requirement_id, required_depth), requirement_ids=[requirement_id]))
    roots = [requirement_id for requirement_id, requirement in requirements.items() if requirement.get("parent_id") is None]
    for root_id in roots:
        stack = [root_id]
        reaches_depth = False
        visited = set()
        while stack:
            candidate = stack.pop()
            if candidate in visited:
                continue
            visited.add(candidate)
            candidate_level = requirements.get(candidate, {}).get("level")
            if is_integer(candidate_level) and candidate_level >= required_depth:
                reaches_depth = True
                break
            stack.extend(children.get(candidate, []))
        if not reaches_depth:
            issues.append(issue("ROOT_REQUIRED_DEPTH_MISSING", "%s has no continuous path to required level %d" % (root_id, required_depth), requirement_ids=[root_id]))

    for test_id, test_case in tests.items():
        requirement_ids = _list_of_strings(test_case.get("requirement_ids"))
        for requirement_id in requirement_ids:
            if requirement_id not in requirements:
                issues.append(issue("TEST_MISSING_REQUIREMENT", "%s references missing requirement %s" % (test_id, requirement_id), test_case_ids=[test_id]))
            elif test_id not in _list_of_strings(requirements[requirement_id].get("test_case_ids")):
                issues.append(issue("ASYMMETRIC_TEST_LINK", "%s links %s but the requirement does not link back" % (test_id, requirement_id), requirement_ids=[requirement_id], test_case_ids=[test_id]))
        for criterion_id in _list_of_strings(test_case.get("acceptance_criterion_ids")):
            if criterion_id not in all_criteria:
                issues.append(issue("TEST_MISSING_CRITERION", "%s references missing criterion %s" % (test_id, criterion_id), test_case_ids=[test_id]))
            elif all_criteria[criterion_id] not in requirement_ids:
                issues.append(issue(
                    "TEST_CRITERION_WRONG_OWNER",
                    "%s criterion %s belongs to requirement %s, which is absent from requirement_ids" % (test_id, criterion_id, all_criteria[criterion_id]),
                    requirement_ids=[all_criteria[criterion_id]],
                    test_case_ids=[test_id],
                ))
        contract_type_by_field = {
            "ui_contract": "e2e", "api_contract": "api",
            "database_contract": "database", "file_contract": "file",
            "permissions": "permission",
        }
        for contract_id in _list_of_strings(test_case.get("contract_ids")):
            owner = all_contracts.get(contract_id)
            if owner is None:
                issues.append(issue("TEST_CONTRACT_MISSING", "%s references missing contract %s" % (test_id, contract_id), test_case_ids=[test_id]))
                continue
            owner_requirement, owner_field = owner
            if owner_requirement not in requirement_ids:
                issues.append(issue(
                    "TEST_CONTRACT_WRONG_OWNER",
                    "%s contract %s belongs to requirement %s" % (test_id, contract_id, owner_requirement),
                    requirement_ids=[owner_requirement], test_case_ids=[test_id],
                ))
            expected_test_type = contract_type_by_field.get(owner_field)
            if expected_test_type and test_case.get("test_type") != expected_test_type:
                issues.append(issue(
                    "TEST_CONTRACT_TYPE_MISMATCH",
                    "%s contract %s requires test_type %s" % (test_id, contract_id, expected_test_type),
                    requirement_ids=[owner_requirement], test_case_ids=[test_id],
                ))
        step_numbers = [item.get("step_no") for item in test_case.get("steps", []) if isinstance(item, dict)]
        expected_numbers = [item.get("step_no") for item in test_case.get("expected_results", []) if isinstance(item, dict)]
        if sorted(step_numbers) != sorted(expected_numbers) or len(step_numbers) != len(set(step_numbers)):
            issues.append(issue("TEST_STEP_EXPECTATION_MISMATCH", "%s steps and expected_results are not one-to-one" % test_id, test_case_ids=[test_id]))
        rank = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        test_rank = rank.get(test_case.get("criticality"), -1)
        for criterion_id in _list_of_strings(test_case.get("acceptance_criterion_ids")):
            criterion_rank = rank.get(criterion_criticality.get(criterion_id), -1)
            if criterion_rank > test_rank:
                issues.append(issue(
                    "TEST_CRITERION_CRITICALITY_TOO_LOW",
                    "%s criticality is lower than acceptance criterion %s" % (test_id, criterion_id),
                    requirement_ids=[all_criteria[criterion_id]] if criterion_id in all_criteria else None,
                    test_case_ids=[test_id],
                ))
        for requirement_id in requirement_ids:
            requirement_rank = rank.get(requirements.get(requirement_id, {}).get("criticality"), -1)
            if requirement_rank > test_rank:
                issues.append(issue("TEST_CRITICALITY_TOO_LOW", "%s criticality is lower than requirement %s" % (test_id, requirement_id), test_case_ids=[test_id], requirement_ids=[requirement_id]))
        automation_path = test_case.get("automation_path")
        if automation_path:
            try:
                path = _safe_repo_path(repo, automation_path, must_exist=True)
                if not path.is_file():
                    raise HarnessError("automation path is not a regular file")
                _assert_git_head_file(repo, automation_path, 2 * 1024 * 1024)
            except (HarnessError, OSError, subprocess.SubprocessError) as exc:
                issues.append(issue("AUTOMATION_PATH_INVALID", "%s: %s" % (test_id, exc), status="FAIL", test_case_ids=[test_id]))

    for requirement_id, requirement in requirements.items():
        for test_id in _list_of_strings(requirement.get("test_case_ids")):
            if test_id not in tests:
                issues.append(issue("REQUIREMENT_MISSING_TEST", "%s references missing test %s" % (requirement_id, test_id), requirement_ids=[requirement_id]))
            elif requirement_id not in _list_of_strings(tests[test_id].get("requirement_ids")):
                issues.append(issue("ASYMMETRIC_REQUIREMENT_LINK", "%s links %s but the test does not link back" % (requirement_id, test_id), requirement_ids=[requirement_id], test_case_ids=[test_id]))
        linked_test_types = {
            tests[test_id].get("test_type") for test_id in _list_of_strings(requirement.get("test_case_ids"))
            if test_id in tests
        }
        required_types = set()
        if requirement.get("ui_contract"):
            required_types.add("e2e")
        if requirement.get("api_contract"):
            required_types.add("api")
        if requirement.get("database_contract"):
            required_types.add("database")
        if requirement.get("file_contract"):
            required_types.add("file")
        if requirement.get("permissions"):
            required_types.add("permission")
        for test_type in sorted(required_types - linked_test_types):
            issues.append(issue(
                "REQUIREMENT_TEST_TYPE_MISSING",
                "%s contract requires a linked %s test" % (requirement_id, test_type),
                status="NOT_RUN",
                requirement_ids=[requirement_id],
            ))
        for contract_id, (owner_requirement, owner_field) in sorted(all_contracts.items()):
            if owner_requirement != requirement_id:
                continue
            expected_test_type = {
                "ui_contract": "e2e", "api_contract": "api",
                "database_contract": "database", "file_contract": "file",
                "permissions": "permission",
            }.get(owner_field)
            covering_tests = [
                test_id for test_id in _list_of_strings(requirement.get("test_case_ids"))
                if test_id in tests
                and contract_id in _list_of_strings(tests[test_id].get("contract_ids"))
                and tests[test_id].get("test_type") == expected_test_type
                and tests[test_id].get("is_mocked") is False
            ]
            if not covering_tests:
                issues.append(issue(
                    "REQUIREMENT_CONTRACT_TEST_MISSING",
                    "%s contract %s lacks a dedicated non-mocked %s test" % (requirement_id, contract_id, expected_test_type),
                    status="NOT_RUN", requirement_ids=[requirement_id],
                ))

    configured_checks = config.get("checks") if isinstance(config.get("checks"), dict) else {}
    configured_test_types = {test.get("test_type") for test in tests.values()}
    for check_name, required_test_type in (
        ("api", "api"), ("database", "database"), ("e2e", "e2e"),
        ("file", "file"), ("integration", "integration"), ("permission", "permission"),
        ("security", "security"), ("unit", "unit"),
    ):
        check = configured_checks.get(check_name)
        if isinstance(check, dict) and check.get("applicable") is True and required_test_type not in configured_test_types:
            issues.append(issue(
                "APPLICABLE_CHECK_TEST_TYPE_MISSING",
                "applicable %s check has no %s test case" % (check_name, required_test_type),
                status="NOT_RUN",
            ))
        if required_test_type in configured_test_types and (
            not isinstance(check, dict) or check.get("applicable") is not True
        ):
            issues.append(issue(
                "CONFIG_TEST_CHECK_FALSE_NOT_APPLICABLE",
                "test type %s exists, so checks.%s cannot be declared inapplicable" % (required_test_type, check_name),
            ))

    for execution_id, execution in executions.items():
        test_id = execution.get("test_case_id")
        if test_id not in tests:
            issues.append(issue("EXECUTION_MISSING_TEST", "%s references missing test %s" % (execution_id, test_id)))
        for evidence_id in _list_of_strings(execution.get("evidence_ids")):
            if evidence_id not in evidence:
                issues.append(issue("EXECUTION_MISSING_EVIDENCE", "%s references missing evidence %s" % (execution_id, evidence_id), status="NOT_RUN", test_case_ids=[test_id] if isinstance(test_id, str) else None))
            elif execution_id not in _list_of_strings(evidence[evidence_id].get("execution_ids")):
                issues.append(issue("ASYMMETRIC_EXECUTION_EVIDENCE_LINK", "%s links %s but evidence does not link back" % (execution_id, evidence_id), test_case_ids=[test_id] if isinstance(test_id, str) else None))

    for evidence_id, evidence_item in evidence.items():
        for execution_id in _list_of_strings(evidence_item.get("execution_ids")):
            if execution_id not in executions:
                issues.append(issue("EVIDENCE_MISSING_EXECUTION", "%s references missing execution %s" % (evidence_id, execution_id)))
            elif evidence_id not in _list_of_strings(executions[execution_id].get("evidence_ids")):
                issues.append(issue("ASYMMETRIC_EVIDENCE_LINK", "%s links %s but execution does not link back" % (evidence_id, execution_id)))
        for requirement_id in _list_of_strings(evidence_item.get("requirement_ids")):
            if requirement_id not in requirements:
                issues.append(issue("EVIDENCE_MISSING_REQUIREMENT", "%s references missing requirement %s" % (evidence_id, requirement_id)))
        for test_id in _list_of_strings(evidence_item.get("test_case_ids")):
            if test_id not in tests:
                issues.append(issue("EVIDENCE_MISSING_TEST", "%s references missing test %s" % (evidence_id, test_id)))
        for criterion_id in _list_of_strings(evidence_item.get("acceptance_criterion_ids")):
            if criterion_id not in all_criteria:
                issues.append(issue("EVIDENCE_MISSING_CRITERION", "%s references missing criterion %s" % (evidence_id, criterion_id)))
            elif all_criteria[criterion_id] not in _list_of_strings(evidence_item.get("requirement_ids")):
                issues.append(issue(
                    "EVIDENCE_CRITERION_WRONG_OWNER",
                    "%s criterion %s belongs to requirement %s, which is absent from requirement_ids" % (evidence_id, criterion_id, all_criteria[criterion_id]),
                    requirement_ids=[all_criteria[criterion_id]],
                ))

    trace_by_requirement_criterion = {}
    for trace_id, trace in traces.items():
        requirement_id = trace.get("requirement_id")
        criterion_id = trace.get("acceptance_criterion_id")
        pair = (requirement_id, criterion_id)
        if pair in trace_by_requirement_criterion:
            issues.append(issue("DUPLICATE_TRACE", "duplicate trace for requirement/criterion %r" % (pair,)))
        trace_by_requirement_criterion[pair] = trace
        if requirement_id not in requirements:
            issues.append(issue("TRACE_MISSING_REQUIREMENT", "%s references missing requirement %s" % (trace_id, requirement_id)))
        if criterion_id not in all_criteria or all_criteria.get(criterion_id) != requirement_id:
                issues.append(issue("TRACE_MISSING_CRITERION", "%s references a criterion not owned by its requirement" % trace_id, requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None))
        requirement = requirements.get(requirement_id, {})
        contract_ids_by_lane = {
            "frontend": {item.get("contract_id") for item in requirement.get("ui_contract", []) if isinstance(item, dict)},
            "backend": {item.get("contract_id") for item in requirement.get("api_contract", []) if isinstance(item, dict)},
            "storage": (
                {item.get("contract_id") for item in requirement.get("database_contract", []) if isinstance(item, dict)}
                | {item.get("contract_id") for item in requirement.get("file_contract", []) if isinstance(item, dict)}
            ),
            "permissions": {item.get("permission_id") for item in requirement.get("permissions", []) if isinstance(item, dict)},
        }
        for lane_name in ("implementation", "frontend", "backend", "storage", "permissions"):
            lane = trace.get(lane_name)
            if not isinstance(lane, dict):
                continue
            state = lane.get("state")
            if state == "PENDING":
                issues.append(issue("TRACE_LANE_PENDING", "%s lane %s is pending" % (trace_id, lane_name), status="NOT_RUN", requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None))
            expected_contract_ids = {item for item in contract_ids_by_lane.get(lane_name, set()) if isinstance(item, str)}
            if expected_contract_ids and state == "NOT_APPLICABLE":
                issues.append(issue("TRACE_FALSE_NOT_APPLICABLE", "%s lane %s is applicable from the requirement contract" % (trace_id, lane_name), requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None))
            if lane_name == "implementation" and state != "LINKED":
                issues.append(issue("TRACE_IMPLEMENTATION_NOT_LINKED", "%s implementation lane must be LINKED" % trace_id, requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None))
            if state == "LINKED":
                references = lane.get("references", []) if isinstance(lane.get("references"), list) else []
                reference_ids = {reference.get("id") for reference in references if isinstance(reference, dict) and isinstance(reference.get("id"), str)}
                unexpected_ids = reference_ids - expected_contract_ids if expected_contract_ids else set()
                missing_ids = expected_contract_ids - reference_ids
                if unexpected_ids:
                    issues.append(issue("TRACE_CONTRACT_ID_MISMATCH", "%s lane %s references IDs not owned by the requirement: %s" % (trace_id, lane_name, ", ".join(sorted(unexpected_ids))), requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None))
                if missing_ids:
                    issues.append(issue("TRACE_CONTRACT_ID_MISSING", "%s lane %s omits requirement contract IDs: %s" % (trace_id, lane_name, ", ".join(sorted(missing_ids))), requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None))
                for reference in references:
                    path_value = reference.get("path") if isinstance(reference, dict) else None
                    if not isinstance(path_value, str) or not path_value.strip():
                        issues.append(issue("TRACE_LINKED_PATH_MISSING", "%s lane %s has a LINKED reference without a path" % (trace_id, lane_name), requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None))
                        continue
                    try:
                        path = _safe_repo_path(repo, path_value, must_exist=True)
                        if not path.is_file():
                            raise HarnessError("linked path is not a regular file")
                        if path.stat().st_size > 2 * 1024 * 1024:
                            raise HarnessError("linked path exceeds 2 MiB semantic inspection limit")
                        _assert_git_head_file(repo, path_value, 2 * 1024 * 1024)
                        text = path.read_text(encoding="utf-8")
                        line_value = reference.get("line")
                        if line_value is not None:
                            line_count = len(text.splitlines()) or 1
                            if not is_integer(line_value) or line_value < 1 or line_value > line_count:
                                issues.append(issue(
                                    "TRACE_LINE_INVALID",
                                    "%s lane %s line %r is outside %s (1..%d)" % (trace_id, lane_name, line_value, path_value, line_count),
                                    requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None,
                                ))
                        symbol = reference.get("symbol")
                        if isinstance(symbol, str) and symbol.strip():
                            if path.suffix.lower() != ".py":
                                issues.append(issue(
                                    "TRACE_SYMBOL_LANGUAGE_UNSUPPORTED",
                                    "%s lane %s symbol %s cannot be definition-bound for %s" % (trace_id, lane_name, symbol, path_value),
                                    status="BLOCKED",
                                    requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None,
                                ))
                            else:
                                spans = _python_symbol_spans(text, path_value)
                                matching_spans = spans.get(symbol.strip(), [])
                                if not matching_spans:
                                    issues.append(issue(
                                        "TRACE_SYMBOL_NOT_FOUND",
                                        "%s lane %s symbol %s is not a Python definition in %s" % (trace_id, lane_name, symbol, path_value),
                                        requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None,
                                    ))
                                elif line_value is not None and not any(start <= line_value <= end for start, end in matching_spans):
                                    issues.append(issue(
                                        "TRACE_SYMBOL_LINE_MISMATCH",
                                        "%s lane %s line %s is outside definition %s in %s" % (trace_id, lane_name, line_value, symbol, path_value),
                                        requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None,
                                    ))
                    except (HarnessError, OSError, UnicodeError, subprocess.SubprocessError) as exc:
                        issues.append(issue("TRACE_PATH_INVALID", "%s lane %s: %s" % (trace_id, lane_name, exc), requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None))
        for test_id in _list_of_strings(trace.get("test_case_ids")):
            if test_id not in tests:
                issues.append(issue("TRACE_MISSING_TEST", "%s references missing test %s" % (trace_id, test_id)))
            else:
                test_case = tests[test_id]
                if requirement_id not in _list_of_strings(test_case.get("requirement_ids")) or criterion_id not in _list_of_strings(test_case.get("acceptance_criterion_ids")):
                    issues.append(issue("TRACE_TEST_NOT_COVERING_CRITERION", "%s test %s does not cover its requirement and criterion" % (trace_id, test_id), requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None, test_case_ids=[test_id]))
        for execution_id in _list_of_strings(trace.get("test_execution_ids")):
            if execution_id not in executions:
                issues.append(issue("TRACE_EXECUTION_NOT_RUN", "%s references unavailable execution %s" % (trace_id, execution_id), status="NOT_RUN", requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None))
            elif executions[execution_id].get("test_case_id") not in _list_of_strings(trace.get("test_case_ids")):
                issues.append(issue("TRACE_EXECUTION_WRONG_TEST", "%s execution %s belongs to a different test" % (trace_id, execution_id), requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None))
            elif not set(_list_of_strings(executions[execution_id].get("evidence_ids"))).intersection(_list_of_strings(trace.get("evidence_ids"))):
                issues.append(issue("TRACE_EXECUTION_WITHOUT_EVIDENCE", "%s execution %s has no evidence in the trace" % (trace_id, execution_id), status="NOT_RUN", requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None))
        for evidence_id in _list_of_strings(trace.get("evidence_ids")):
            if evidence_id not in evidence:
                issues.append(issue("TRACE_EVIDENCE_NOT_AVAILABLE", "%s references unavailable evidence %s" % (trace_id, evidence_id), status="NOT_RUN", requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None))
            else:
                evidence_item = evidence[evidence_id]
                if requirement_id not in _list_of_strings(evidence_item.get("requirement_ids")) or criterion_id not in _list_of_strings(evidence_item.get("acceptance_criterion_ids")):
                    issues.append(issue("TRACE_EVIDENCE_WRONG_SCOPE", "%s evidence %s does not cover its requirement and criterion" % (trace_id, evidence_id), requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None))
                if not set(_list_of_strings(evidence_item.get("test_case_ids"))).intersection(_list_of_strings(trace.get("test_case_ids"))):
                    issues.append(issue("TRACE_EVIDENCE_WRONG_TEST", "%s evidence %s does not cover a linked test" % (trace_id, evidence_id), requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None))
                if not set(_list_of_strings(evidence_item.get("execution_ids"))).intersection(_list_of_strings(trace.get("test_execution_ids"))):
                    issues.append(issue("TRACE_EVIDENCE_WRONG_EXECUTION", "%s evidence %s does not cover a linked execution" % (trace_id, evidence_id), requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None))
        verdict = trace.get("final_verdict")
        if isinstance(verdict, dict):
            if verdict.get("state") == "PENDING":
                issues.append(issue("TRACE_VERDICT_PENDING", "%s final verdict is pending" % trace_id, status="NOT_RUN", requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None))
            linked_requirement = verdict.get("acceptance_result_requirement_id")
            if verdict.get("state") == "LINKED" and linked_requirement != requirement_id:
                issues.append(issue("TRACE_VERDICT_WRONG_REQUIREMENT", "%s final verdict must link to its own requirement %s" % (trace_id, requirement_id), requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None))
            if linked_requirement is not None and linked_requirement not in acceptance:
                issues.append(issue("TRACE_MISSING_ACCEPTANCE", "%s references missing acceptance result" % trace_id, status="NOT_RUN", requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None))
            elif linked_requirement is not None and verdict.get("status") != acceptance[linked_requirement].get("status"):
                issues.append(issue("TRACE_VERDICT_MISMATCH", "%s verdict status differs from acceptance result" % trace_id, requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None))
            if verdict.get("status") == "PASS":
                trace_tests = _list_of_strings(trace.get("test_case_ids"))
                trace_executions = _list_of_strings(trace.get("test_execution_ids"))
                trace_evidence = _list_of_strings(trace.get("evidence_ids"))
                if not trace_tests:
                    issues.append(issue("TRACE_PASS_WITHOUT_TEST", "%s PASS trace has no test case" % trace_id, status="NOT_RUN", requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None))
                if not trace_executions:
                    issues.append(issue("TRACE_PASS_WITHOUT_EXECUTION", "%s PASS trace has no execution" % trace_id, status="NOT_RUN", requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None))
                if not trace_evidence:
                    issues.append(issue("TRACE_PASS_WITHOUT_EVIDENCE", "%s PASS trace has no evidence" % trace_id, status="NOT_RUN", requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None))
                acceptance_item = acceptance.get(requirement_id, {})
                if not set(trace_tests).issubset(set(_list_of_strings(acceptance_item.get("test_case_ids")))):
                    issues.append(issue("TRACE_ACCEPTANCE_TEST_MISMATCH", "%s tests are not linked by the acceptance result" % trace_id, requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None))
                if not set(trace_evidence).issubset(set(_list_of_strings(acceptance_item.get("evidence_ids")))):
                    issues.append(issue("TRACE_ACCEPTANCE_EVIDENCE_MISMATCH", "%s evidence is not linked by the acceptance result" % trace_id, requirement_ids=[requirement_id] if isinstance(requirement_id, str) else None))

    for criterion_id, requirement_id in all_criteria.items():
        if (requirement_id, criterion_id) not in trace_by_requirement_criterion:
            issues.append(issue("MISSING_TRACE", "criterion %s has no traceability record" % criterion_id, requirement_ids=[requirement_id]))

    implementation_paths_by_requirement = {}
    for trace in traces.values():
        requirement_id = trace.get("requirement_id") if isinstance(trace, dict) else None
        lane = trace.get("implementation") if isinstance(trace, dict) else None
        if not isinstance(requirement_id, str) or not isinstance(lane, dict):
            continue
        for reference in lane.get("references", []) if isinstance(lane.get("references"), list) else []:
            path_value = reference.get("path") if isinstance(reference, dict) else None
            if isinstance(path_value, str) and path_value.strip():
                implementation_paths_by_requirement.setdefault(requirement_id, set()).add(
                    Path(path_value).as_posix()
                )

    def descendants_of(root_id):
        found = set()
        pending = list(children.get(root_id, []))
        while pending:
            child_id = pending.pop()
            if child_id in found:
                continue
            found.add(child_id)
            pending.extend(children.get(child_id, []))
        return found

    for requirement_id, result in acceptance.items():
        if requirement_id not in requirements:
            issues.append(issue("ACCEPTANCE_MISSING_REQUIREMENT", "acceptance result references missing requirement %s" % requirement_id))
        aggregate_group = bool(
            requirements.get(requirement_id, {}).get("node_type") == "group"
            and not _list_of_strings(requirements.get(requirement_id, {}).get("test_case_ids"))
        )
        allowed_requirement_ids = {requirement_id}
        if aggregate_group:
            allowed_requirement_ids.update(descendants_of(requirement_id))
        for test_id in _list_of_strings(result.get("test_case_ids")):
            if test_id not in tests:
                issues.append(issue("ACCEPTANCE_MISSING_TEST", "%s references missing test %s" % (requirement_id, test_id)))
            elif aggregate_group and not allowed_requirement_ids.intersection(
                _list_of_strings(tests[test_id].get("requirement_ids"))
            ):
                issues.append(issue(
                    "ACCEPTANCE_GROUP_TEST_OUTSIDE_DESCENDANTS",
                    "%s aggregate acceptance links test %s outside its descendants" % (requirement_id, test_id),
                    requirement_ids=[requirement_id], test_case_ids=[test_id],
                ))
        for evidence_id in _list_of_strings(result.get("evidence_ids")):
            if evidence_id not in evidence:
                issues.append(issue("ACCEPTANCE_MISSING_EVIDENCE", "%s references missing evidence %s" % (requirement_id, evidence_id), status="NOT_RUN", requirement_ids=[requirement_id]))
            elif not allowed_requirement_ids.intersection(
                _list_of_strings(evidence[evidence_id].get("requirement_ids"))
            ):
                issues.append(issue("ACCEPTANCE_EVIDENCE_WRONG_SCOPE", "%s evidence %s does not cover the requirement" % (requirement_id, evidence_id), requirement_ids=[requirement_id]))
        locations = {
            Path(value).as_posix() for value in _list_of_strings(result.get("implementation_locations"))
            if value.strip()
        }
        for location in sorted(locations):
            try:
                implementation_path = _safe_repo_path(repo, location, must_exist=True)
                if not implementation_path.is_file():
                    raise HarnessError("implementation location is not a regular file")
            except (HarnessError, OSError) as exc:
                issues.append(issue(
                    "ACCEPTANCE_IMPLEMENTATION_PATH_INVALID",
                    "%s implementation location %s is invalid: %s" % (requirement_id, location, exc),
                    requirement_ids=[requirement_id],
                ))
        if result.get("status") == "PASS":
            traced_locations = implementation_paths_by_requirement.get(requirement_id, set())
            if aggregate_group:
                traced_locations = set().union(*(
                    implementation_paths_by_requirement.get(child_id, set())
                    for child_id in allowed_requirement_ids
                ))
            if locations != traced_locations:
                issues.append(issue(
                    "ACCEPTANCE_IMPLEMENTATION_TRACE_MISMATCH",
                    "%s PASS implementation locations do not exactly match its trace implementation paths" % requirement_id,
                    requirement_ids=[requirement_id],
                    details={"acceptance": sorted(locations), "trace": sorted(traced_locations)},
                ))

    indexes = {
        "requirements": requirements,
        "changes": changes,
        "tests": tests,
        "executions": executions,
        "evidence": evidence,
        "traces": traces,
        "acceptance": acceptance,
        "criteria": all_criteria,
        "criterion_criticality": criterion_criticality,
        "contracts": all_contracts,
        "children": children,
    }
    return issues, indexes


def _baseline_requirements(document):
    if isinstance(document, list):
        return document
    if isinstance(document, dict):
        for key in ("requirements", "baseline_requirements", "items"):
            if isinstance(document.get(key), list):
                return document[key]
    return []


def guarded_requirement_snapshot(requirement):
    guarded = {name: requirement.get(name) for name in GUARDED_REQUIREMENT_FIELDS}
    payload = {"requirement_id": requirement.get("requirement_id"), "guarded": guarded}
    return {
        "requirement_id": requirement.get("requirement_id"),
        "title": requirement.get("title"),
        "description": requirement.get("description"),
        "criticality": requirement.get("criticality"),
        "category_path": requirement.get("category_path"),
        "acceptance_criterion_ids": _criterion_ids(requirement),
        "test_case_ids": requirement.get("test_case_ids"),
        "normalized_sha256": canonical_sha256(payload),
    }


def requirement_set_sha256(requirements):
    normalized = []
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        normalized.append({
            "requirement_id": requirement.get("requirement_id"),
            "guarded": {name: requirement.get(name) for name in GUARDED_REQUIREMENT_FIELDS},
        })
    normalized.sort(key=lambda item: str(item.get("requirement_id") or ""))
    return canonical_sha256(normalized)


def _approved_identity(value):
    return isinstance(value, str) and value.strip() and value.strip().lower() not in {
        "tbd", "none", "self", "unknown", "unapproved", "pending",
    }


def _canonical_items(value):
    if not isinstance(value, list):
        return set()
    return {canonical_json_bytes(item) for item in value}


def _requirement_delta_is_reducing(old_requirement, new_requirement, changed_fields):
    if old_requirement is not None and new_requirement is None:
        return True
    if old_requirement is None or new_requirement is None:
        return False
    criticality_rank = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    if criticality_rank.get(new_requirement.get("criticality"), -1) < criticality_rank.get(old_requirement.get("criticality"), -1):
        return True
    for field_name in (
        "actors", "permissions", "ui_contract", "api_contract", "database_contract",
        "file_contract", "acceptance_criteria", "test_case_ids", "dependencies",
    ):
        if field_name in changed_fields and not _canonical_items(old_requirement.get(field_name)).issubset(
            _canonical_items(new_requirement.get(field_name))
        ):
            return True
    if "out_of_scope" in changed_fields and not _canonical_items(new_requirement.get("out_of_scope")).issubset(
        _canonical_items(old_requirement.get("out_of_scope"))
    ):
        return True
    # Free-text/source/assumption changes cannot be proven to be additive locally.
    if changed_fields.intersection({"description", "source", "assumptions"}):
        return True
    return False


def _allowed_change_types(old_requirement, new_requirement, changed_fields, reducing):
    if old_requirement is None:
        return {"add", "split", "scope_expand"}
    if new_requirement is None:
        return {"delete", "merge"}
    if changed_fields == {"title"}:
        return {"rename"}
    if changed_fields.issubset({"parent_id", "level", "category_path", "node_type"}):
        return {"move", "reclassify"}
    if changed_fields == {"criticality"}:
        return {"criticality_change"}
    if changed_fields.intersection({"acceptance_criteria", "test_case_ids"}):
        return {"acceptance_change", "scope_reduce" if reducing else "scope_expand"}
    if changed_fields.intersection({"permissions", "ui_contract", "api_contract", "database_contract", "file_contract"}):
        return {"contract_change", "scope_reduce" if reducing else "scope_expand"}
    return {"scope_reduce" if reducing else "scope_expand", "reclassify"}


def _load_git_anchored_requirements(repo, anchor):
    git = shutil.which("git")
    if not git:
        raise HarnessError("Git executable is unavailable for baseline anchor validation")
    ancestor = _run_process([git, "merge-base", "--is-ancestor", anchor, "HEAD"], repo)
    if ancestor["exit_code"] != 0:
        raise HarnessError("baseline git_commit is not an ancestor of current HEAD")
    shown = _run_process([
        git, "show", "%s:.harness/spec/requirements.json" % anchor,
    ], repo)
    if shown["exit_code"] != 0:
        raise HarnessError("cannot read requirements.json from baseline git_commit")
    document = strict_json_loads(shown["stdout"], "git baseline requirements")
    items = _baseline_requirements(document)
    if not isinstance(document, dict) or not isinstance(document.get("requirements"), list):
        raise HarnessError("anchored requirements document has an invalid root")
    return items


def validate_requirement_baseline(repo, indexes, config=None, strict=False):
    path = Path(repo) / ".harness" / "state" / "requirements-baseline.json"
    if not path.exists():
        return [issue(
            "BASELINE_MISSING",
            "required requirement baseline is missing",
            status="FAIL" if strict else "NOT_RUN",
            path=path,
        )]
    try:
        path = _safe_repo_path(repo, ".harness/state/requirements-baseline.json", must_exist=True)
        baseline_document = strict_json_load(path)
    except HarnessError as exc:
        return [issue("BASELINE_READ_ERROR", str(exc), path=path)]
    baseline_issues = []
    baseline_items = _baseline_requirements(baseline_document)
    baseline = _id_index(baseline_items, ("requirement_id",), "baseline_requirement", baseline_issues)
    current = indexes["requirements"]
    changes = indexes["changes"]
    if not baseline and current:
        baseline_issues.append(issue("BASELINE_EMPTY", "requirement baseline is empty while current requirements are non-empty", status="FAIL" if strict else "NOT_RUN", path=path))
    baseline_hash = requirement_set_sha256(baseline.values())
    current_hash = requirement_set_sha256(current.values())
    if baseline or current:
        if not isinstance(baseline_document, dict):
            baseline_issues.append(issue("BASELINE_METADATA_MISSING", "non-empty baseline must use the metadata object form", path=path))
        else:
            if baseline_document.get("standard_version") != STANDARD_VERSION:
                baseline_issues.append(issue("BASELINE_VERSION_INVALID", "baseline standard_version is invalid", path=path))
            if not _approved_identity(baseline_document.get("approved_by")):
                baseline_issues.append(issue("BASELINE_APPROVER_INVALID", "non-empty baseline requires a named non-placeholder approver", path=path))
            try:
                parse_datetime(baseline_document.get("approved_at"))
            except (ValueError, TypeError):
                baseline_issues.append(issue("BASELINE_APPROVED_AT_INVALID", "non-empty baseline requires an approved_at date-time", path=path))
            stored_hash = baseline_document.get("requirements_sha256")
            if stored_hash != baseline_hash:
                baseline_issues.append(issue(
                    "BASELINE_HASH_MISMATCH",
                    "baseline requirements_sha256 does not match its guarded requirements",
                    path=path, details={"expected": baseline_hash, "observed": stored_hash},
                ))
            anchor = baseline_document.get("git_commit")
            if not isinstance(anchor, str) or re.fullmatch(r"[0-9a-f]{40,64}", anchor) is None:
                baseline_issues.append(issue("BASELINE_GIT_COMMIT_INVALID", "non-empty baseline requires a full lowercase Git commit", path=path))
            else:
                try:
                    anchored_items = _load_git_anchored_requirements(repo, anchor)
                    anchored_hash = requirement_set_sha256(anchored_items)
                    if anchored_hash != baseline_hash:
                        baseline_issues.append(issue(
                            "BASELINE_ANCHOR_CONTENT_MISMATCH",
                            "baseline contents differ from requirements.json at baseline git_commit",
                            path=path,
                            details={"anchor_hash": anchored_hash, "baseline_hash": baseline_hash},
                        ))
                except (HarnessError, OSError) as exc:
                    baseline_issues.append(issue("BASELINE_GIT_ANCHOR_INVALID", str(exc), path=path))
    elif isinstance(baseline_document, dict):
        for name in ("approved_at", "approved_by", "git_commit", "requirements_sha256"):
            if baseline_document.get(name) is not None:
                baseline_issues.append(issue("BASELINE_EMPTY_METADATA_INVALID", "empty baseline must keep %s null" % name, path=path))

    delta = {}
    for requirement_id in sorted(set(baseline) | set(current)):
        old_requirement = baseline.get(requirement_id)
        new_requirement = current.get(requirement_id)
        changed_fields = {
            name for name in GUARDED_REQUIREMENT_FIELDS
            if not json_equal(
                old_requirement.get(name) if old_requirement else None,
                new_requirement.get(name) if new_requirement else None,
            )
        }
        if old_requirement is None or new_requirement is None or changed_fields:
            reducing = _requirement_delta_is_reducing(old_requirement, new_requirement, changed_fields)
            delta[requirement_id] = {
                "old": old_requirement, "new": new_requirement,
                "changed_fields": changed_fields,
                "reducing": reducing,
                "allowed_types": _allowed_change_types(old_requirement, new_requirement, changed_fields, reducing),
            }
    authorized = set()
    claimed_by_change = {}

    for change_id, change in changes.items():
        entry_issues = []
        change_type = change.get("change_type")
        affected = set(_list_of_strings(change.get("requirement_ids")))
        before_items = change.get("before", []) if isinstance(change.get("before"), list) else []
        after_items = change.get("after", []) if isinstance(change.get("after"), list) else []
        before = {item.get("requirement_id"): item for item in before_items if isinstance(item, dict) and isinstance(item.get("requirement_id"), str)}
        after = {item.get("requirement_id"): item for item in after_items if isinstance(item, dict) and isinstance(item.get("requirement_id"), str)}
        expected_before_ids = affected.intersection(baseline)
        expected_after_ids = affected.intersection(current)

        def ledger_error(code, message, details=None):
            item = issue(code, "%s: %s" % (change_id, message), details=details)
            baseline_issues.append(item)
            entry_issues.append(item)

        if change.get("base_requirements_sha256") != baseline_hash:
            ledger_error("CHANGE_BASE_HASH_MISMATCH", "base_requirements_sha256 does not match the guarded baseline", {"expected": baseline_hash})
        if change.get("result_requirements_sha256") != current_hash:
            ledger_error("CHANGE_RESULT_HASH_MISMATCH", "result_requirements_sha256 does not match the guarded current requirements", {"expected": current_hash})
        if set(before) != expected_before_ids:
            ledger_error("CHANGE_BEFORE_IDS_MISMATCH", "before snapshots do not exactly cover affected baseline requirements", {"expected": sorted(expected_before_ids), "observed": sorted(before)})
        if set(after) != expected_after_ids:
            ledger_error("CHANGE_AFTER_IDS_MISMATCH", "after snapshots do not exactly cover affected current requirements", {"expected": sorted(expected_after_ids), "observed": sorted(after)})
        for requirement_id in sorted(expected_before_ids):
            expected = guarded_requirement_snapshot(baseline[requirement_id])
            if not json_equal(before.get(requirement_id), expected):
                ledger_error("CHANGE_BEFORE_SNAPSHOT_MISMATCH", "before snapshot does not match baseline requirement %s" % requirement_id, {"expected": expected, "observed": before.get(requirement_id)})
        for requirement_id in sorted(expected_after_ids):
            expected = guarded_requirement_snapshot(current[requirement_id])
            if not json_equal(after.get(requirement_id), expected):
                ledger_error("CHANGE_AFTER_SNAPSHOT_MISMATCH", "after snapshot does not match current requirement %s" % requirement_id, {"expected": expected, "observed": after.get(requirement_id)})
        if not affected:
            ledger_error("CHANGE_REQUIREMENTS_EMPTY", "change does not identify affected requirements")
        unrelated = affected - set(delta)
        if unrelated:
            ledger_error("CHANGE_AFFECTED_NO_DELTA", "ledger claims requirements without a current guarded delta", {"requirement_ids": sorted(unrelated)})
        duplicate_claims = {item for item in affected if item in claimed_by_change}
        if duplicate_claims:
            ledger_error("CHANGE_DELTA_CLAIMED_MULTIPLE_TIMES", "guarded delta is claimed by more than one ledger entry", {"requirement_ids": sorted(duplicate_claims)})
        topology_valid = False
        if change_type == "merge":
            topology_valid = (
                len(expected_before_ids) >= 2 and len(expected_after_ids) == 1
                and expected_before_ids.isdisjoint(expected_after_ids)
            )
            if not topology_valid:
                ledger_error(
                    "CHANGE_MERGE_TOPOLOGY_INVALID",
                    "merge must replace at least two baseline requirements with exactly one new requirement",
                    {"before_ids": sorted(expected_before_ids), "after_ids": sorted(expected_after_ids)},
                )
        elif change_type == "split":
            topology_valid = (
                len(expected_before_ids) == 1 and len(expected_after_ids) >= 2
                and expected_before_ids.isdisjoint(expected_after_ids)
            )
            if not topology_valid:
                ledger_error(
                    "CHANGE_SPLIT_TOPOLOGY_INVALID",
                    "split must replace exactly one baseline requirement with at least two new requirements",
                    {"before_ids": sorted(expected_before_ids), "after_ids": sorted(expected_after_ids)},
                )
        for requirement_id in sorted(affected.intersection(delta)):
            delta_item = delta[requirement_id]
            if not (change_type in {"merge", "split"} and topology_valid) and change_type not in delta_item["allowed_types"]:
                ledger_error(
                    "CHANGE_TYPE_MISMATCH",
                    "declared type %s is inconsistent with guarded delta for %s" % (change_type, requirement_id),
                    {"allowed_types": sorted(delta_item["allowed_types"]), "changed_fields": sorted(delta_item["changed_fields"])},
                )
            needs_approval = change_type in {"merge", "split"} or delta_item["reducing"] or (
                delta_item["old"] is not None and delta_item["new"] is not None
            )
            if needs_approval and not _approved_identity(change.get("approved_by")):
                ledger_error("CHANGE_APPROVER_INVALID", "guarded or reducing change lacks a named external approver")
        for requirement_id in affected:
            claimed_by_change[requirement_id] = change_id
        if not entry_issues:
            authorized.update(affected)

    for requirement_id, old_requirement in baseline.items():
        if requirement_id not in current:
            if requirement_id not in authorized:
                baseline_issues.append(issue("UNAPPROVED_REQUIREMENT_DELETION", "baseline requirement disappeared without approved ledger entry: %s" % requirement_id, requirement_ids=[requirement_id]))
            continue
        new_requirement = current[requirement_id]
        changed = [name for name in GUARDED_REQUIREMENT_FIELDS if not json_equal(old_requirement.get(name), new_requirement.get(name))]
        if changed and requirement_id not in authorized:
            baseline_issues.append(issue("UNAPPROVED_REQUIREMENT_CHANGE", "baseline requirement %s changed guarded fields without approval: %s" % (requirement_id, ", ".join(changed)), requirement_ids=[requirement_id]))
    for requirement_id in sorted(set(current) - set(baseline)):
        if requirement_id not in authorized:
            baseline_issues.append(issue(
                "UNAPPROVED_REQUIREMENT_ADDITION",
                "current requirement was added without a complete ledger entry: %s" % requirement_id,
                requirement_ids=[requirement_id],
            ))
    return baseline_issues


def _run_process(argv, cwd, timeout=20, extra_env=None):
    started = utc_now()
    process_env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", PYTHONDONTWRITEBYTECODE="1")
    if isinstance(extra_env, dict):
        process_env.update({str(key): str(value) for key, value in extra_env.items()})
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env=process_env,
        )
        return {
            "argv": list(argv),
            "cwd": str(cwd),
            "started_at": started,
            "completed_at": utc_now(),
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "argv": list(argv),
            "cwd": str(cwd),
            "started_at": started,
            "completed_at": utc_now(),
            "exit_code": 124,
            "stdout": exc.stdout or "",
            "stderr": (exc.stderr or "") + "\ncommand timed out",
        }
    except OSError as exc:
        return {
            "argv": list(argv),
            "cwd": str(cwd),
            "started_at": started,
            "completed_at": utc_now(),
            "exit_code": 127,
            "stdout": "",
            "stderr": str(exc),
        }


def _git_path_is_runtime(path):
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return (
        normalized.startswith(".harness/runs/")
        or normalized.startswith(".harness/state/subagent-guard/")
        or normalized in RUNTIME_MUTABLE_PATHS
        or normalized in {
        ".harness/release-report.json",
        ".harness/install-report.json",
        }
    )


def git_snapshot(repo):
    git = shutil.which("git")
    commands = []
    if not git:
        return None, [issue("GIT_UNAVAILABLE", "git executable is unavailable", status="BLOCKED")], commands
    head_result = _run_process([git, "rev-parse", "HEAD^{commit}"], repo)
    commands.append(head_result)
    if head_result["exit_code"] != 0:
        return None, [issue("GIT_REPOSITORY_UNAVAILABLE", "cannot resolve a Git commit: %s" % head_result["stderr"].strip(), status="BLOCKED")], commands
    head = head_result["stdout"].strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", head):
        return None, [issue("GIT_COMMIT_INVALID", "git returned an invalid full commit object ID")], commands
    top_result = _run_process([git, "rev-parse", "--show-toplevel"], repo)
    commands.append(top_result)
    if top_result["exit_code"] != 0:
        return None, [issue("GIT_ROOT_ERROR", top_result["stderr"].strip() or "cannot resolve Git root")], commands
    git_root = Path(top_result["stdout"].strip()).resolve()
    if git_root != Path(repo).resolve():
        return None, [issue("GIT_ROOT_MISMATCH", "harness repository root %s differs from Git root %s" % (repo, git_root))], commands
    status_result = _run_process([git, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"], repo)
    commands.append(status_result)
    if status_result["exit_code"] != 0:
        return None, [issue("GIT_STATUS_ERROR", status_result["stderr"].strip() or "git status failed")], commands
    dirty = []
    raw_entries = status_result["stdout"].split("\x00")
    skip_next_rename_path = False
    for entry in raw_entries:
        if not entry:
            continue
        if skip_next_rename_path:
            skip_next_rename_path = False
            if not _git_path_is_runtime(entry):
                dirty.append(entry)
            continue
        status_code = entry[:2]
        path = entry[3:] if len(entry) > 3 else entry
        if "R" in status_code or "C" in status_code:
            skip_next_rename_path = True
        if not _git_path_is_runtime(path):
            dirty.append("%s %s" % (status_code, path))
    snapshot = {"git_commit": head.lower(), "git_root": str(git_root), "dirty_paths": sorted(dirty)}
    issues = []
    if dirty:
        issues.append(issue("GIT_WORKTREE_DIRTY", "release-relevant Git worktree paths are dirty", details=sorted(dirty)))
    return snapshot, issues, commands


def _hash_regular_file(path, max_bytes):
    path = Path(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise HarnessError("evidence path is not a regular file")
    if before.st_size > max_bytes:
        raise HarnessError("evidence exceeds configured size limit")
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        first_stat = os.fstat(handle.fileno())
        if not stat.S_ISREG(first_stat.st_mode):
            raise HarnessError("opened evidence is not a regular file")
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                raise HarnessError("evidence exceeds configured size limit")
            digest.update(block)
        last_stat = os.fstat(handle.fileno())
    after = path.stat()
    identity_before = (first_stat.st_dev, first_stat.st_ino, first_stat.st_size, first_stat.st_mtime_ns)
    identity_after = (last_stat.st_dev, last_stat.st_ino, last_stat.st_size, last_stat.st_mtime_ns)
    identity_path = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or identity_after != identity_path:
        raise HarnessError("evidence changed while it was being hashed")
    return digest.hexdigest(), total


def _derive_runner_status(summary, exit_code):
    tests_run = summary.get("tests_run")
    tests_passed = summary.get("tests_passed")
    tests_failed = summary.get("tests_failed")
    tests_skipped = summary.get("tests_skipped")
    if not all(is_integer(value) and value >= 0 for value in (tests_run, tests_passed, tests_failed, tests_skipped)):
        raise HarnessError("runner summary counts must be non-negative integers")
    if tests_passed + tests_failed + tests_skipped != tests_run:
        raise HarnessError("runner summary counts do not add up")
    if not is_integer(exit_code):
        raise HarnessError("runner exit_code must be an integer")
    if tests_failed > 0 or exit_code != 0:
        return "FAIL"
    if tests_run < 1 or tests_skipped > 0:
        return "NOT_RUN"
    if tests_passed == tests_run:
        return "PASS"
    return "FAIL"


def _unittest_observed_identifiers(raw_output):
    marker_matches = re.findall(r"(?m)^HARNESS_UNITTEST_RESULT_[0-9a-f]{32}=(\{.*\})$", raw_output if isinstance(raw_output, str) else "")
    if marker_matches:
        try:
            payload = strict_json_loads(marker_matches[-1], "fresh unittest adapter marker")
            identifiers = payload.get("test_identifiers") if isinstance(payload, dict) else None
            if isinstance(identifiers, list) and all(isinstance(item, str) for item in identifiers):
                return identifiers
        except HarnessError:
            return []
    observed = []
    pattern = re.compile(
        r"(?m)^([A-Za-z_][A-Za-z0-9_]*)\s+\(([^)]+)\)\s+\.\.\.\s+"
        r"(?:ok|FAIL|ERROR|skipped|expected failure|unexpected success)"
    )
    for match in pattern.finditer(raw_output if isinstance(raw_output, str) else ""):
        context = match.group(2)
        method = match.group(1)
        observed.append(context if context.endswith("." + method) else "%s.%s" % (context, method))
    return observed


UNITTEST_ADAPTER_CODE = r'''import json,os,sys,unittest
# Capture every framework primitive before repository modules are imported.  The
# repository is allowed to define tests, but it must not be able to replace the
# loader/result/runner that decides the receipt.
_BaseCase = unittest.TestCase
_BaseLoader = unittest.TestLoader
_BaseSuite = unittest.TestSuite
_LoaderDiscover = _BaseLoader.discover
_LoaderFindTests = _BaseLoader._find_tests
_LoaderFindPath = _BaseLoader._find_test_path
_CaseRun = _BaseCase.run
_CaseId = _BaseCase.id
_OsWrite = os.write
_RepoCwd = os.getcwd()

class FrozenLoader(_BaseLoader):
    suiteClass = _BaseSuite
    def discover(self, *args, **kwargs):
        return _LoaderDiscover(self, *args, **kwargs)
    def _find_tests(self, *args, **kwargs):
        return _LoaderFindTests(self, *args, **kwargs)
    def _find_test_path(self, *args, **kwargs):
        return _LoaderFindPath(self, *args, **kwargs)
    def getTestCaseNames(self, test_case_class):
        prefix = self.testMethodPrefix
        names = [name for name in dir(test_case_class)
                 if name.startswith(prefix) and callable(getattr(test_case_class, name, None))]
        return sorted(names)
    def loadTestsFromTestCase(self, test_case_class):
        if not isinstance(test_case_class, type) or not issubclass(test_case_class, _BaseCase):
            return self.suiteClass(())
        return self.suiteClass(test_case_class(name) for name in self.getTestCaseNames(test_case_class))
    def loadTestsFromModule(self, module, *args, **kwargs):
        # Deliberately ignore a repository-defined load_tests hook: it is code
        # that could return an invented suite instead of the discovered cases.
        suites = []
        for name in dir(module):
            value = getattr(module, name, None)
            if isinstance(value, type) and issubclass(value, _BaseCase) and value is not _BaseCase:
                suites.append(self.loadTestsFromTestCase(value))
        return self.suiteClass(suites)

def stable_id(test):
    try:
        return _CaseId(test)
    except BaseException:
        return '%s.%s.%s' % (
            getattr(test.__class__, '__module__', '<unknown>'),
            getattr(test.__class__, '__qualname__', getattr(test.__class__, '__name__', '<unknown>')),
            getattr(test, '_testMethodName', '<unknown>'),
        )

class RecordingResult(object):
    def __init__(self):
        self.testsRun = 0
        self.failfast = False
        self.shouldStop = False
        self.buffer = False
        self.tb_locals = False
        self.identifiers = []
        self.outcomes = {}
    def _mark(self, test, outcome):
        test_id = stable_id(test)
        rank = {'PASS': 0, 'SKIP': 1, 'FAIL': 2}
        if rank[outcome] >= rank.get(self.outcomes.get(test_id), -1):
            self.outcomes[test_id] = outcome
    def startTest(self, test):
        self.testsRun += 1
        test_id = stable_id(test)
        self.identifiers.append(test_id)
        self.outcomes.setdefault(test_id, 'PASS')
    def stopTest(self, test):
        return None
    def startTestRun(self):
        return None
    def stopTestRun(self):
        return None
    def stop(self):
        self.shouldStop = True
    def addSuccess(self, test):
        self._mark(test, 'PASS')
    def addError(self, test, err):
        self._mark(test, 'FAIL')
    def addFailure(self, test, err):
        self._mark(test, 'FAIL')
    def addSkip(self, test, reason):
        self._mark(test, 'SKIP')
    def addExpectedFailure(self, test, err):
        self._mark(test, 'SKIP')
    def addUnexpectedSuccess(self, test):
        self._mark(test, 'FAIL')
    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._mark(test, 'FAIL')

def flatten(suite):
    result = []
    for item in list(getattr(suite, '_tests', ())):
        if isinstance(item, _BaseCase):
            result.append(item)
        elif hasattr(item, '_tests'):
            result.extend(flatten(item))
        else:
            raise TypeError('discovery returned an unsupported test object')
    return result

start, pattern = sys.argv[1], sys.argv[2]
sys.path.insert(0, _RepoCwd)
loader = FrozenLoader()
loader.suiteClass = _BaseSuite
discover = loader.discover
suite = discover(start, pattern=pattern)
tests = flatten(suite)
result = RecordingResult()
for test in tests:
    # Invoke the captured stdlib lifecycle, never a repository replacement for
    # TestCase.run or TextTestRunner.run.
    _CaseRun(test, result)
failed = sum(1 for value in result.outcomes.values() if value == 'FAIL')
skipped = sum(1 for value in result.outcomes.values() if value == 'SKIP')
passed = sum(1 for value in result.outcomes.values() if value == 'PASS')
for test_id in result.identifiers:
    label = {'PASS': 'ok', 'SKIP': 'skipped', 'FAIL': 'FAIL'}[result.outcomes.get(test_id, 'FAIL')]
    _OsWrite(2, ('%s ... %s\n' % (test_id, label)).encode('utf-8', 'replace'))
_OsWrite(2, ('\n----------------------------------------------------------------------\nRan %d test%s in 0.000s\n\n%s\n' % (
    result.testsRun, '' if result.testsRun == 1 else 's',
    'OK' if failed == 0 and skipped == 0 and result.testsRun > 0 else 'FAILED',
)).encode('utf-8', 'replace'))
payload = {'test_identifiers': result.identifiers, 'summary': {
    'tests_run': result.testsRun, 'tests_passed': passed,
    'tests_failed': failed, 'tests_skipped': skipped,
}}
_OsWrite(1, ('HARNESS_UNITTEST_RESULT___HARNESS_NONCE__=' + json.dumps(payload, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8'))
raise SystemExit(0 if failed == 0 and skipped == 0 and result.testsRun > 0 else 1)
'''


def _automation_mock_markers(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise HarnessError("cannot inspect automation for mock usage: %s" % exc)
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        raise HarnessError("cannot parse automation AST for mock review: %s" % exc)
    markers = set()
    mock_names = {"mock", "patch", "Mock", "MagicMock", "AsyncMock", "monkeypatch"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "unittest.mock" or alias.name.startswith("unittest.mock."):
                    markers.add("import:%s" % alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "unittest.mock" or (module == "unittest" and any(alias.name == "mock" for alias in node.names)):
                markers.add("from:%s" % module)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = list(node.args.args) + list(node.args.kwonlyargs)
            if node.args.vararg is not None:
                arguments.append(node.args.vararg)
            if node.args.kwarg is not None:
                arguments.append(node.args.kwarg)
            if any(argument.arg == "monkeypatch" for argument in arguments):
                markers.add("fixture:monkeypatch")
        elif isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Name) and function.id in mock_names:
                markers.add("call:%s" % function.id)
            elif isinstance(function, ast.Attribute):
                parts = []
                current = function
                while isinstance(current, ast.Attribute):
                    parts.append(current.attr)
                    current = current.value
                if isinstance(current, ast.Name):
                    parts.append(current.id)
                dotted = ".".join(reversed(parts))
                if any(token in mock_names for token in parts) or dotted.startswith("unittest.mock"):
                    markers.add("call:%s" % dotted)
    return sorted(markers)


def _trusted_hooks_list_projection(repo):
    """Capture a hash-bound live hooks/list result in the trusted parent process."""
    repository = Path(repo).resolve()
    source_path = repository / ".codex" / "hooks.json"
    expected_timeouts = {
        "preToolUse": 120,
        "stop": 600,
        "subagentStart": 120,
        "subagentStop": 120,
    }
    source_sha256 = None
    events = []
    warnings = []
    errors = []
    try:
        before_sha256, _before_size = _hash_regular_file(source_path, 4 * 1024 * 1024)
        source_sha256 = before_sha256
        import harness_init as _harness_init_runtime

        installer = _harness_init_runtime.Installer(repository)
        installer.verify_hooks()
        verification = installer.hook_verification
        if not isinstance(verification, dict):
            raise HarnessError("Installer.verify_hooks returned no projection")
        raw_warnings = verification.get("warnings")
        raw_errors = verification.get("errors")
        if not isinstance(raw_warnings, list) or not all(
            isinstance(item, str) for item in raw_warnings
        ):
            raise HarnessError("live Hook warnings have an invalid shape")
        if not isinstance(raw_errors, list) or not all(
            isinstance(item, str) for item in raw_errors
        ):
            raise HarnessError("live Hook errors have an invalid shape")
        warnings.extend(raw_warnings)
        errors.extend(raw_errors)
        errors.extend(str(item) for item in installer.blockers)
        raw_events = verification.get("events")
        if not isinstance(raw_events, list):
            raise HarnessError("live Hook events have an invalid shape")
        by_name = {}
        for item in raw_events:
            if not isinstance(item, dict) or not isinstance(item.get("event"), str):
                raise HarnessError("live Hook event entry has an invalid shape")
            by_name.setdefault(item["event"], []).append(item)
        for event_name, timeout in sorted(expected_timeouts.items()):
            matches = by_name.get(event_name, [])
            if len(matches) != 1:
                events.append({
                    "event": event_name,
                    "recognized": False,
                    "enabled": False,
                    "trusted": False,
                    "current_hash": None,
                    "timeout_seconds": None,
                })
                errors.append(
                    "hooks/list must contain exactly one canonical %s event" % event_name
                )
                continue
            item = matches[0]
            current_hash = item.get("current_hash")
            normalized = {
                "event": event_name,
                "recognized": item.get("recognized") is True,
                "enabled": item.get("enabled") is True,
                "trusted": item.get("trust_status") == "trusted",
                "current_hash": current_hash,
                "timeout_seconds": item.get("timeout_seconds"),
            }
            events.append(normalized)
            if re.fullmatch(r"sha256:[a-f0-9]{64}", str(current_hash or "")) is None:
                errors.append("hooks/list %s currentHash is invalid" % event_name)
            if normalized["timeout_seconds"] != timeout:
                errors.append("hooks/list %s timeout is not canonical" % event_name)
        unexpected_events = sorted(set(by_name) - set(expected_timeouts))
        if unexpected_events:
            errors.append(
                "hooks/list projection contains unexpected canonical events: %s"
                % ", ".join(unexpected_events)
            )
        after_sha256, _after_size = _hash_regular_file(source_path, 4 * 1024 * 1024)
        if after_sha256 != before_sha256:
            errors.append("project hooks.json changed during live hooks/list verification")
        if verification.get("status") != "PASS":
            errors.append("Installer.verify_hooks did not return PASS")
    except (HarnessError, ImportError, OSError, RuntimeError, UnicodeError, ValueError) as exc:
        errors.append("%s: %s" % (type(exc).__name__, exc))
    errors = list(dict.fromkeys(errors))
    warnings = list(dict.fromkeys(warnings))
    passed = (
        source_sha256 is not None
        and not warnings
        and not errors
        and len(events) == len(expected_timeouts)
        and all(
            item.get("recognized") is True
            and item.get("enabled") is True
            and item.get("trusted") is True
            and re.fullmatch(
                r"sha256:[a-f0-9]{64}", str(item.get("current_hash") or "")
            ) is not None
            and item.get("timeout_seconds") == expected_timeouts[item["event"]]
            for item in events
        )
    )
    projection = {
        "status": "PASS" if passed else "BLOCKED",
        "source_path": str(source_path),
        "source_sha256": source_sha256,
        "events": events,
        "warnings": warnings,
        "errors": errors,
    }
    projection["integrity_sha256"] = canonical_sha256(projection)
    return projection


def _trusted_codex_features_projection(repo):
    """Capture current CLI feature state outside the repository-test sandbox."""
    repository = Path(repo).resolve()
    source_path = repository / ".codex" / "config.toml"
    expected = {"hooks": True, "multi_agent": False, "multi_agent_v2": False}
    errors = []
    features = {}
    source_sha256 = None
    executable = shutil.which("codex")
    command = None
    try:
        before_sha256, _before_size = _hash_regular_file(source_path, 4 * 1024 * 1024)
        source_sha256 = before_sha256
        if not executable:
            raise HarnessError("Codex executable is unavailable")
        command = _run_process(
            [executable, "-C", str(repository), "features", "list"],
            repository,
            timeout=30,
        )
        if command.get("exit_code") != 0:
            raise HarnessError(
                "codex features list failed: %s"
                % (command.get("stderr", "").strip() or "unknown error")
            )
        for raw_line in command.get("stdout", "").splitlines():
            match = re.match(r"^(\S+)\s+\S+\s+(true|false)\s*$", raw_line)
            if not match:
                continue
            name = match.group(1)
            if name in features:
                raise HarnessError("codex features list repeated %s" % name)
            features[name] = match.group(2) == "true"
        for name, value in expected.items():
            if features.get(name) is not value:
                errors.append(
                    "codex feature %s expected %s but observed %s"
                    % (name, str(value).lower(), features.get(name))
                )
        after_sha256, _after_size = _hash_regular_file(source_path, 4 * 1024 * 1024)
        if after_sha256 != before_sha256:
            errors.append("project config.toml changed during feature verification")
    except (HarnessError, OSError, RuntimeError, UnicodeError, ValueError) as exc:
        errors.append("%s: %s" % (type(exc).__name__, exc))
    errors = list(dict.fromkeys(errors))
    projection = {
        "status": "PASS" if source_sha256 is not None and not errors else "BLOCKED",
        "source_path": str(source_path),
        "source_sha256": source_sha256,
        "executable": executable,
        "features": {name: features.get(name) for name in sorted(expected)},
        "exit_code": command.get("exit_code") if isinstance(command, dict) else None,
        "errors": errors,
    }
    projection["integrity_sha256"] = canonical_sha256(projection)
    return projection


def _run_sandboxed_unittest(argv, repo, timeout):
    """Run repository tests in a deny-write/network OS sandbox or fail closed."""
    macos_sandbox = shutil.which("sandbox-exec") if sys.platform == "darwin" else None
    linux_sandbox = shutil.which("bwrap") if sys.platform.startswith("linux") else None
    if not macos_sandbox and not linux_sandbox:
        raise HarnessError("fresh repository test execution requires sandbox-exec (macOS) or bubblewrap (Linux)")
    sandbox_temp = Path(tempfile.mkdtemp(prefix="harness-unittest-sandbox-")).resolve()
    configured_home = os.environ.get("CODEX_HOME")
    codex_home = (
        Path(configured_home).expanduser()
        if configured_home
        else Path.home() / ".codex"
    )
    try:
        user_config_path = Path(os.path.abspath(str(codex_home / "config.toml")))
        user_values = _toml_table_scalars(user_config_path, "agents")
        user_digest, _user_size = _hash_regular_file(user_config_path, 4 * 1024 * 1024)
        user_agent_projection = {
            "status": "PASS",
            "source_sha256": user_digest,
            "enabled": user_values.get("enabled"),
            "max_concurrent_threads_per_session": user_values.get(
                "max_concurrent_threads_per_session"
            ),
            "interrupt_message": user_values.get("interrupt_message"),
        }
    except (HarnessError, OSError, UnicodeError, ValueError) as exc:
        user_agent_projection = {
            "status": "BLOCKED",
            "error": "%s: %s" % (type(exc).__name__, exc),
        }
    try:
        trusted_commit = _guard_git_bytes(
            Path(repo).resolve(), ["rev-parse", "HEAD^{commit}"]
        ).decode("ascii").strip().lower()
        if re.fullmatch(r"[0-9a-f]{40,64}", trusted_commit) is None:
            raise HarnessError("Git returned an invalid current commit")
        git_projection = {"status": "PASS", "commit": trusted_commit}
    except (HarnessError, OSError, UnicodeError, ValueError) as exc:
        git_projection = {
            "status": "BLOCKED",
            "error": "%s: %s" % (type(exc).__name__, exc),
        }
    trusted_environment = {
        "HARNESS_TRUSTED_USER_AGENT_CONFIG": json.dumps(
            user_agent_projection,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "HARNESS_TRUSTED_GIT_SNAPSHOT": json.dumps(
            git_projection,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    trusted_inputs = {
        "user_agent_config": user_agent_projection,
        "git_snapshot": git_projection,
    }
    is_hooks_contract = (
        len(argv) == 9
        and list(argv[1:5]) == ["-m", "unittest", "discover", "-s"]
        and argv[6] == "-p"
        and argv[8] == "-v"
        and Path(argv[7]).name == "test_robot_hooks.py"
    )
    if is_hooks_contract:
        hooks_projection = _trusted_hooks_list_projection(repo)
        trusted_environment["HARNESS_TRUSTED_HOOKS_LIST"] = json.dumps(
            hooks_projection,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        trusted_inputs["hooks_list"] = hooks_projection
    is_configuration_contract = (
        len(argv) == 9
        and list(argv[1:5]) == ["-m", "unittest", "discover", "-s"]
        and argv[6] == "-p"
        and argv[8] == "-v"
        and Path(argv[7]).name == "test_robot_configuration.py"
    )
    if is_configuration_contract:
        features_projection = _trusted_codex_features_projection(repo)
        trusted_environment["HARNESS_TRUSTED_CODEX_FEATURES"] = json.dumps(
            features_projection,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        trusted_inputs["codex_features"] = features_projection

    def quote_profile(value):
        return str(value).replace("\\", "\\\\").replace('"', '\\"')

    actual_argv = list(argv)
    attestation_nonce = uuid.uuid4().hex
    if len(argv) == 9 and list(argv[1:5]) == ["-m", "unittest", "discover", "-s"] and argv[6] == "-p" and argv[8] == "-v":
        actual_argv = [
            argv[0], "-I", "-S", "-c",
            UNITTEST_ADAPTER_CODE.replace("__HARNESS_NONCE__", attestation_nonce),
            argv[5], argv[7],
        ]
    if macos_sandbox:
        profile = "\n".join((
            "(version 1)",
            "(allow default)",
            "(deny network*)",
            "(deny file-write*)",
            "(allow file-write* (subpath \"%s\") (literal \"/dev/null\"))" % quote_profile(sandbox_temp),
            "(deny file-read* (subpath \"%s\"))" % quote_profile(Path.home().resolve()),
            "(allow file-read* (subpath \"%s\") (subpath \"%s\"))" % (
                quote_profile(Path(repo).resolve()), quote_profile(sandbox_temp),
            ),
        ))
        sandbox_argv = [macos_sandbox, "-p", profile] + actual_argv
    else:
        sandbox_argv = [
            linux_sandbox, "--die-with-parent", "--new-session", "--unshare-all",
            "--ro-bind", "/", "/", "--bind", str(sandbox_temp), str(sandbox_temp),
            "--proc", "/proc", "--dev", "/dev", "--chdir", str(Path(repo).resolve()),
            "--",
        ] + actual_argv
    try:
        result = _run_isolated_process(
            sandbox_argv,
            repo,
            timeout,
            sandbox_temp,
            extra_environment=trusted_environment,
        )
        adapter_sha256 = hashlib.sha256(UNITTEST_ADAPTER_CODE.encode("utf-8")).hexdigest()
        safe_actual_argv = list(actual_argv)
        if "-c" in safe_actual_argv:
            code_position = safe_actual_argv.index("-c") + 1
            if code_position < len(safe_actual_argv):
                safe_actual_argv[code_position] = "<canonical-harness-adapter sha256=%s>" % adapter_sha256
        safe_sandbox_argv = list(sandbox_argv)
        for position, value in enumerate(safe_sandbox_argv):
            if value == "-p" and position + 1 < len(safe_sandbox_argv):
                safe_sandbox_argv[position + 1] = "<sandbox-profile sha256=%s>" % hashlib.sha256(
                    safe_sandbox_argv[position + 1].encode("utf-8")
                ).hexdigest()
            if value == "-c" and position + 1 < len(safe_sandbox_argv):
                safe_sandbox_argv[position + 1] = "<canonical-harness-adapter sha256=%s>" % adapter_sha256
        result["sandbox_argv"] = safe_sandbox_argv
        result["actual_argv"] = safe_actual_argv
        result["argv"] = list(argv)
        result["sandboxed"] = True
        result["sandbox_kind"] = "sandbox-exec" if macos_sandbox else "bubblewrap"
        result["adapter_sha256"] = adapter_sha256
        result["attestation_nonce"] = attestation_nonce
        result["trusted_inputs"] = trusted_inputs
        return result
    finally:
        shutil.rmtree(str(sandbox_temp), ignore_errors=True)


def _run_isolated_process(argv, cwd, timeout, output_root, extra_environment=None):
    """Run untrusted code with bounded output/resources and kill its whole process group."""
    started = utc_now()
    stdout_path = Path(output_root) / "stdout.bin"
    stderr_path = Path(output_root) / "stderr.bin"
    environment = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(output_root),
        "TMPDIR": str(output_root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    if extra_environment is not None:
        if not isinstance(extra_environment, dict):
            raise HarnessError("isolated process extra environment must be an object")
        for name, value in extra_environment.items():
            if (
                not isinstance(name, str)
                or re.fullmatch(r"HARNESS_TRUSTED_[A-Z0-9_]{1,96}", name) is None
                or not isinstance(value, str)
                or len(value.encode("utf-8")) > 64 * 1024
                or "\x00" in value
            ):
                raise HarnessError("isolated process extra environment is unsafe")
            environment[name] = value

    def limits():
        if resource is None:
            return
        cpu = max(1, int(timeout) + 5)
        for limit_name, value in (
            (resource.RLIMIT_CPU, (cpu, cpu)),
            (resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024)),
            (resource.RLIMIT_NOFILE, (128, 128)),
            # RLIMIT_NPROC is per user on macOS/Linux, not per sandboxed child.
            # A limit below the already-running Codex process count prevents even
            # one legitimate test subprocess from starting. Keep a finite ceiling
            # while allowing normal multi-agent desktop workloads.
            (resource.RLIMIT_NPROC, (1024, 1024)),
            (resource.RLIMIT_AS, (4 * 1024 * 1024 * 1024, 4 * 1024 * 1024 * 1024)),
        ):
            try:
                resource.setrlimit(limit_name, value)
            except (OSError, ValueError):
                pass

    timed_out = False
    with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
        try:
            process = subprocess.Popen(
                list(argv), cwd=str(cwd), stdin=subprocess.DEVNULL,
                stdout=stdout_handle, stderr=stderr_handle, env=environment,
                start_new_session=True, preexec_fn=limits,
            )
        except OSError as exc:
            return {
                "argv": list(argv), "cwd": str(cwd), "started_at": started,
                "completed_at": utc_now(), "exit_code": 127,
                "stdout": "", "stderr": str(exc), "timed_out": False,
            }
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                process.kill()
            exit_code = process.wait()
    stdout_bytes = stdout_path.read_bytes()
    stderr_bytes = stderr_path.read_bytes()
    truncated = len(stdout_bytes) >= 16 * 1024 * 1024 or len(stderr_bytes) >= 16 * 1024 * 1024
    stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    stderr_text = stderr_bytes.decode("utf-8", errors="replace")
    if timed_out:
        stderr_text += "\nprocess group timed out and was terminated"
    if truncated:
        stderr_text += "\nprocess output reached the 16 MiB safety limit"
    return {
        "argv": list(argv), "cwd": str(cwd), "started_at": started,
        "completed_at": utc_now(), "exit_code": exit_code,
        "stdout": stdout_text, "stderr": stderr_text,
        "timed_out": timed_out, "output_truncated": truncated,
    }


def _fresh_unittest_attestation(repo, automation_path, identifiers):
    automation = _safe_repo_path(repo, automation_path, must_exist=True)
    if not automation.is_file() or automation.suffix != ".py":
        raise HarnessError("unittest automation_path must be a regular .py file")
    before_digest, _before_size = _assert_git_head_file(repo, automation_path, 2 * 1024 * 1024)
    relative = automation.relative_to(Path(repo).resolve())
    start_directory = relative.parent.as_posix() or "."
    argv = [
        sys.executable, "-m", "unittest", "discover", "-s", start_directory,
        "-p", relative.name, "-v",
    ]
    result = _run_sandboxed_unittest(argv, repo, timeout=120)
    after_digest, _after_size = _assert_git_head_file(repo, automation_path, 2 * 1024 * 1024)
    if after_digest != before_digest:
        raise HarnessError("unittest automation changed during fresh execution")
    raw_output = (result.get("stdout") or "") + (result.get("stderr") or "")
    nonce = result.get("attestation_nonce")
    marker_matches = re.findall(
        r"(?m)^HARNESS_UNITTEST_RESULT_%s=(\{.*\})$" % re.escape(str(nonce)), raw_output,
    )
    if len(marker_matches) != 1:
        raise HarnessError("fresh unittest output lacks the trusted adapter result marker")
    marker = strict_json_loads(marker_matches[-1], "fresh unittest adapter marker")
    summary = marker.get("summary") if isinstance(marker, dict) else None
    if not isinstance(summary, dict):
        raise HarnessError("fresh unittest adapter marker lacks summary")
    status = _derive_runner_status(summary, result.get("exit_code"))
    if status == "PASS" and re.search(r"(?:^|\n)OK(?:\s|$)", raw_output) is None:
        raise HarnessError("fresh unittest PASS lacks the OK marker")
    if status == "FAIL" and "FAILED" not in raw_output:
        raise HarnessError("fresh unittest failure lacks the FAILED marker")
    observed_identifiers = marker.get("test_identifiers") if isinstance(marker.get("test_identifiers"), list) else []
    if identifiers is not None and sorted(observed_identifiers) != sorted(identifiers):
        raise HarnessError(
            "fresh unittest identifiers differ from the report: observed=%s expected=%s"
            % (sorted(observed_identifiers), sorted(identifiers))
        )
    return {
        "argv": argv,
        "cwd": str(Path(repo).resolve()),
        "started_at": result.get("started_at"),
        "completed_at": result.get("completed_at"),
        "exit_code": result.get("exit_code"),
        "status": status,
        "summary": summary,
        "test_identifiers": observed_identifiers,
        "sandboxed": result.get("sandboxed") is True,
        "sandbox_argv": result.get("sandbox_argv"),
        "actual_argv": result.get("actual_argv"),
        "sandbox_kind": result.get("sandbox_kind"),
        "adapter_sha256": result.get("adapter_sha256"),
        "trusted_inputs": result.get("trusted_inputs"),
        "stdout": result.get("stdout") or "",
        "stderr": result.get("stderr") or "",
    }


def _parse_test_report(repo, evidence_id, evidence_item, evidence_path, indexes, current_commit, allow_fresh_execution=True):
    report_issues = []

    def report_issue(code, message, status="FAIL"):
        report_issues.append(issue(code, "%s: %s" % (evidence_id, message), status=status))

    try:
        report = strict_json_load(evidence_path)
    except HarnessError as exc:
        report_issue("TEST_REPORT_JSON_INVALID", str(exc))
        return None, report_issues
    if not isinstance(report, dict):
        report_issue("TEST_REPORT_SHAPE_INVALID", "test report root must be an object")
        return None, report_issues
    required = {
        "format", "runner", "tool_version", "command", "cwd", "started_at",
        "completed_at", "exit_code", "status", "git_commit", "execution_id",
        "test_case_id", "summary", "raw_output", "runner_path", "runner_sha256",
        "automation_path", "automation_sha256", "test_identifiers",
    }
    missing = required - set(report)
    extra = set(report) - required
    if missing:
        report_issue("TEST_REPORT_FIELDS_MISSING", "missing fields: %s" % ", ".join(sorted(missing)))
    if extra:
        report_issue("TEST_REPORT_FIELDS_UNKNOWN", "unknown fields: %s" % ", ".join(sorted(extra)))
    if report.get("format") != TEST_REPORT_FORMAT:
        report_issue("TEST_REPORT_FORMAT_UNSUPPORTED", "unsupported report format")
    if report.get("runner") not in SUPPORTED_TEST_REPORT_RUNNERS:
        report_issue(
            "TEST_REPORT_RUNNER_UNSUPPORTED",
            "runner %r has no trusted parser/corroboration adapter" % report.get("runner"),
        )
    for name in (
        "runner", "tool_version", "command", "cwd", "execution_id", "test_case_id",
        "raw_output", "runner_path", "automation_path",
    ):
        if not isinstance(report.get(name), str) or not report[name].strip():
            report_issue("TEST_REPORT_FIELD_INVALID", "%s must be a non-empty string" % name)
    for name in ("runner_sha256", "automation_sha256"):
        if not isinstance(report.get(name), str) or re.fullmatch(r"[0-9a-f]{64}", report[name]) is None:
            report_issue("TEST_REPORT_FIELD_INVALID", "%s must be a lowercase SHA-256" % name)
    identifiers = report.get("test_identifiers")
    if not isinstance(identifiers, list) or not identifiers or any(
        not isinstance(item, str) or not item.strip() for item in identifiers
    ) or len(set(identifiers)) != len(identifiers):
        report_issue("TEST_REPORT_IDENTIFIERS_INVALID", "test_identifiers must be a non-empty unique string list")
        identifiers = []
    try:
        started = parse_datetime(report.get("started_at"))
        completed = parse_datetime(report.get("completed_at"))
        if completed < started:
            raise ValueError("completed_at precedes started_at")
    except (ValueError, TypeError) as exc:
        report_issue("TEST_REPORT_TIME_INVALID", str(exc))
    summary = report.get("summary")
    required_summary = {"tests_run", "tests_passed", "tests_failed", "tests_skipped"}
    if not isinstance(summary, dict) or set(summary) != required_summary:
        report_issue("TEST_REPORT_SUMMARY_INVALID", "summary must contain exactly %s" % ", ".join(sorted(required_summary)))
        derived_status = "FAIL"
    else:
        try:
            derived_status = _derive_runner_status(summary, report.get("exit_code"))
        except HarnessError as exc:
            report_issue("TEST_REPORT_SUMMARY_INVALID", str(exc))
            derived_status = "FAIL"
    if isinstance(summary, dict) and identifiers and len(identifiers) != summary.get("tests_run"):
        report_issue("TEST_REPORT_IDENTIFIERS_INVALID", "test_identifiers count differs from tests_run")
    if report.get("status") != derived_status:
        report_issue("TEST_REPORT_STATUS_MISMATCH", "claimed status %s differs from derived status %s" % (report.get("status"), derived_status))
    if current_commit and str(report.get("git_commit", "")).lower() != current_commit:
        report_issue("TEST_REPORT_COMMIT_MISMATCH", "report git_commit does not equal current HEAD")

    if report.get("runner") == "unittest" and isinstance(report.get("raw_output"), str):
        match = re.search(r"Ran\s+(\d+)\s+tests?\s+in\s+", report["raw_output"])
        if match is None or not isinstance(summary, dict) or int(match.group(1)) != summary.get("tests_run"):
            report_issue("TEST_REPORT_RAW_OUTPUT_MISMATCH", "unittest raw output does not corroborate tests_run")
        if derived_status == "PASS" and not re.search(r"(?:^|\n)OK(?:\s|$)", report["raw_output"]):
            report_issue("TEST_REPORT_RAW_OUTPUT_MISMATCH", "unittest PASS report lacks the OK result marker")
        if derived_status == "FAIL" and "FAILED" not in report["raw_output"]:
            report_issue("TEST_REPORT_RAW_OUTPUT_MISMATCH", "unittest FAIL report lacks the FAILED result marker")

    execution_id = report.get("execution_id")
    test_case_id = report.get("test_case_id")
    test_case = indexes.get("tests", {}).get(test_case_id)
    if not isinstance(test_case, dict):
        report_issue("TEST_REPORT_TEST_MISSING", "report test_case_id does not exist")
    else:
        expected_automation = test_case.get("automation_path")
        if report.get("automation_path") != expected_automation:
            report_issue("TEST_REPORT_AUTOMATION_PATH_MISMATCH", "report automation_path differs from the test case")
        if test_case.get("is_mocked") is False and isinstance(report.get("automation_path"), str):
            try:
                markers = _automation_mock_markers(_safe_repo_path(repo, report["automation_path"], must_exist=True))
                if markers:
                    report_issue(
                        "AUTOMATION_MOCK_USAGE_UNDECLARED",
                        "automation declares is_mocked=false but contains common mocking constructs: %s" % ", ".join(markers),
                        status="BLOCKED",
                    )
            except (HarnessError, OSError) as exc:
                report_issue("AUTOMATION_MOCK_REVIEW_UNAVAILABLE", str(exc), status="BLOCKED")
    for path_field, hash_field, code in (
        ("runner_path", "runner_sha256", "TEST_REPORT_RUNNER_SOURCE_MISMATCH"),
        ("automation_path", "automation_sha256", "TEST_REPORT_AUTOMATION_SOURCE_MISMATCH"),
    ):
        try:
            source_path = _safe_repo_path(repo, report.get(path_field), must_exist=True)
            actual_digest, _size = _assert_git_head_file(repo, report.get(path_field), 2 * 1024 * 1024)
            if actual_digest != report.get(hash_field):
                report_issue(code, "%s hash differs from the committed source" % path_field)
        except (HarnessError, OSError, subprocess.SubprocessError) as exc:
            report_issue(code, "%s is invalid: %s" % (path_field, exc))
    try:
        command_tokens = shlex.split(report.get("command", ""))
    except ValueError as exc:
        report_issue("TEST_REPORT_COMMAND_INVALID", "cannot parse command: %s" % exc)
        command_tokens = []
    expected_collector_tokens = None
    if isinstance(report.get("automation_path"), str):
        automation_value = Path(report["automation_path"])
        expected_collector_tokens = [
            sys.executable, "-m", "unittest", "discover", "-s",
            automation_value.parent.as_posix() or ".", "-p", automation_value.name, "-v",
        ]
    if report.get("runner_path") not in command_tokens and command_tokens != expected_collector_tokens:
        report_issue("TEST_REPORT_COMMAND_RUNNER_MISMATCH", "command does not invoke the declared runner_path")
    if isinstance(report.get("raw_output"), str):
        observed_identifiers = _unittest_observed_identifiers(report["raw_output"])
        if sorted(observed_identifiers) != sorted(identifiers):
            report_issue(
                "TEST_REPORT_IDENTIFIER_NOT_OBSERVED",
                "archived unittest identifiers differ from test_identifiers",
            )
    attestation = None
    if not allow_fresh_execution:
        report_issue(
            "TEST_REPORT_FRESH_EXECUTION_NOT_AUTHORIZED",
            "preflight integrity failed, so repository test code was not executed",
            status="NOT_RUN",
        )
    elif report.get("runner") == "unittest" and isinstance(report.get("automation_path"), str):
        try:
            attestation = _fresh_unittest_attestation(repo, report["automation_path"], identifiers)
            if attestation.get("status") != derived_status:
                report_issue(
                    "TEST_REPORT_FRESH_STATUS_MISMATCH",
                    "fresh verifier-run unittest status %s differs from reported %s" % (
                        attestation.get("status"), derived_status,
                    ),
                )
            if isinstance(summary, dict) and attestation.get("summary") != summary:
                report_issue(
                    "TEST_REPORT_FRESH_SUMMARY_MISMATCH",
                    "fresh verifier-run unittest summary differs from the archived report",
                )
            for path_field, hash_field, code in (
                ("runner_path", "runner_sha256", "TEST_REPORT_RUNNER_SOURCE_CHANGED"),
                ("automation_path", "automation_sha256", "TEST_REPORT_AUTOMATION_SOURCE_CHANGED"),
            ):
                source_path = _safe_repo_path(repo, report.get(path_field), must_exist=True)
                fresh_digest, _size = _hash_regular_file(source_path, 2 * 1024 * 1024)
                if fresh_digest != report.get(hash_field):
                    report_issue(code, "%s changed during fresh execution" % path_field)
        except (HarnessError, OSError) as exc:
            report_issue("TEST_REPORT_FRESH_EXECUTION_FAILED", str(exc), status="NOT_RUN")
    if _list_of_strings(evidence_item.get("execution_ids")) != [execution_id]:
        report_issue("TEST_REPORT_EXECUTION_LINK_MISMATCH", "evidence execution_ids must exactly match the report execution_id")
    if _list_of_strings(evidence_item.get("test_case_ids")) != [test_case_id]:
        report_issue("TEST_REPORT_TEST_LINK_MISMATCH", "evidence test_case_ids must exactly match the report test_case_id")
    for field_name in ("command", "exit_code", "started_at", "completed_at", "git_commit"):
        left = evidence_item.get(field_name)
        right = report.get(field_name)
        if field_name == "git_commit" and isinstance(left, str) and isinstance(right, str):
            matches = left.lower() == right.lower()
        else:
            matches = left == right
        if not matches:
            report_issue("TEST_REPORT_EVIDENCE_MISMATCH", "evidence %s differs from the test report" % field_name)
    if evidence_item.get("result") != derived_status:
        report_issue("TEST_REPORT_EVIDENCE_MISMATCH", "evidence result differs from the derived runner status")

    execution = indexes.get("executions", {}).get(execution_id)
    if not isinstance(execution, dict):
        report_issue("TEST_REPORT_EXECUTION_MISSING", "report execution_id does not exist")
    else:
        comparisons = {
            "test_case_id": test_case_id,
            "command": report.get("command"),
            "cwd": report.get("cwd"),
            "started_at": report.get("started_at"),
            "completed_at": report.get("completed_at"),
            "exit_code": report.get("exit_code"),
            "status": derived_status,
            "git_commit": report.get("git_commit"),
            "runner": report.get("runner"),
        }
        if isinstance(summary, dict):
            comparisons.update(summary)
        for field_name, expected in comparisons.items():
            observed = execution.get(field_name)
            if field_name == "git_commit" and isinstance(observed, str) and isinstance(expected, str):
                matches = observed.lower() == expected.lower()
            else:
                matches = observed == expected
            if not matches:
                report_issue("TEST_REPORT_EXECUTION_MISMATCH", "execution %s differs from the test report" % field_name)
        if evidence_id not in _list_of_strings(execution.get("evidence_ids")):
            report_issue("TEST_REPORT_EXECUTION_LINK_MISMATCH", "execution does not link back to evidence")
    return {
        "format": report.get("format"),
        "runner": report.get("runner"),
        "execution_id": execution_id,
        "test_case_id": test_case_id,
        "runner_path": report.get("runner_path"),
        "automation_path": report.get("automation_path"),
        "test_identifiers": identifiers,
        "attestation": attestation,
        "status": derived_status,
        "summary": summary,
    }, report_issues


def verify_evidence(repo, indexes, git_info, config=None, runtime_run_id=None, allow_fresh_execution=True):
    config = config or {}
    issues = []
    valid = {}
    max_bytes = config.get("max_evidence_bytes", 1024 * 1024 * 1024)
    if not is_integer(max_bytes) or max_bytes < 1:
        max_bytes = 1024 * 1024 * 1024
    current_commit = git_info.get("git_commit") if git_info else None
    for evidence_id, evidence_item in indexes["evidence"].items():
        item_issues = []
        test_report = None
        evidence_path = None
        path_value = evidence_item.get("path")
        try:
            normalized_path = Path(path_value).as_posix() if isinstance(path_value, str) else ""
            expected_prefix = ".harness/runs/%s/" % runtime_run_id if isinstance(runtime_run_id, str) and runtime_run_id else ".harness/runs/"
            if not normalized_path.startswith(expected_prefix):
                raise HarnessError("evidence must be stored below %s" % expected_prefix)
            evidence_path = _safe_repo_path(repo, path_value, must_exist=True)
            actual_hash, byte_count = _hash_regular_file(evidence_path, max_bytes)
            expected_hash = evidence_item.get("sha256")
            if actual_hash != expected_hash:
                item_issues.append(issue("EVIDENCE_HASH_MISMATCH", "%s expected %s but got %s" % (evidence_id, expected_hash, actual_hash)))
        except (HarnessError, OSError) as exc:
            byte_count = None
            item_issues.append(issue("EVIDENCE_FILE_INVALID", "%s: %s" % (evidence_id, exc), status="NOT_RUN" if isinstance(exc, FileNotFoundError) else "FAIL"))
        if current_commit and str(evidence_item.get("git_commit", "")).lower() != current_commit:
            item_issues.append(issue("EVIDENCE_COMMIT_MISMATCH", "%s evidence commit does not equal current Git commit" % evidence_id))
        try:
            started = parse_datetime(evidence_item.get("started_at"))
            completed = parse_datetime(evidence_item.get("completed_at"))
            if completed < started:
                raise ValueError("completed_at precedes started_at")
        except (ValueError, TypeError) as exc:
            item_issues.append(issue("EVIDENCE_TIME_INVALID", "%s: %s" % (evidence_id, exc)))
        result = evidence_item.get("result")
        exit_code = evidence_item.get("exit_code")
        if result == "PASS" and exit_code != 0:
            item_issues.append(issue("EVIDENCE_RESULT_CONTRADICTION", "%s says PASS with nonzero exit code" % evidence_id))
        if result == "FAIL" and exit_code == 0:
            item_issues.append(issue("EVIDENCE_RESULT_CONTRADICTION", "%s says FAIL with zero exit code" % evidence_id))
        if evidence_item.get("evidence_type") == "test_report" and evidence_path is not None:
            test_report, report_issues = _parse_test_report(
                repo, evidence_id, evidence_item, evidence_path, indexes, current_commit,
                allow_fresh_execution=allow_fresh_execution,
            )
            item_issues.extend(report_issues)
        validity_status = worst_status(item.get("status") for item in item_issues) if item_issues else "PASS"
        valid[evidence_id] = {
            "valid": not item_issues,
            "status": validity_status,
            "sha256": evidence_item.get("sha256"),
            "path": path_value,
            "bytes": byte_count,
            "evidence_type": evidence_item.get("evidence_type"),
            "result": evidence_item.get("result"),
            "command": evidence_item.get("command"),
            "exit_code": evidence_item.get("exit_code"),
            "execution_ids": _list_of_strings(evidence_item.get("execution_ids")),
            "requirement_ids": _list_of_strings(evidence_item.get("requirement_ids")),
            "acceptance_criterion_ids": _list_of_strings(
                evidence_item.get("acceptance_criterion_ids")
            ),
            "test_case_ids": _list_of_strings(evidence_item.get("test_case_ids")),
            "test_report": test_report,
            "issues": item_issues,
        }
        issues.extend(item_issues)
    return issues, valid


def _execution_status(execution_id, execution, current_commit, evidence_validity):
    claimed = execution.get("status")
    test_id = execution.get("test_case_id")
    local_issues = []
    if claimed not in STATUS_VALUES:
        return "FAIL", [issue("EXECUTION_STATUS_INVALID", "execution has invalid status", test_case_ids=[test_id] if isinstance(test_id, str) else None)]
    commit = execution.get("git_commit")
    if current_commit and (not isinstance(commit, str) or commit.lower() != current_commit):
        local_issues.append(issue("EXECUTION_COMMIT_MISMATCH", "execution for %s is not tied to the current commit" % test_id, test_case_ids=[test_id] if isinstance(test_id, str) else None))
    tests_run = execution.get("tests_run")
    tests_passed = execution.get("tests_passed")
    tests_failed = execution.get("tests_failed")
    tests_skipped = execution.get("tests_skipped")
    if all(is_integer(value) for value in (tests_run, tests_passed, tests_failed, tests_skipped)):
        if tests_passed + tests_failed + tests_skipped != tests_run:
            local_issues.append(issue("EXECUTION_COUNT_MISMATCH", "execution counts do not add up for %s" % test_id, test_case_ids=[test_id] if isinstance(test_id, str) else None))
    if claimed == "PASS":
        if execution.get("exit_code") != 0 or not is_integer(tests_run) or tests_run < 1 or tests_failed != 0 or tests_skipped != 0:
            local_issues.append(issue("EXECUTION_FALSE_PASS", "PASS execution for %s has invalid raw counts/exit code" % test_id, test_case_ids=[test_id] if isinstance(test_id, str) else None))
        evidence_ids = _list_of_strings(execution.get("evidence_ids"))
        if not evidence_ids:
            local_issues.append(issue("EXECUTION_PASS_WITHOUT_EVIDENCE", "PASS execution for %s has no evidence" % test_id, status="NOT_RUN", test_case_ids=[test_id] if isinstance(test_id, str) else None))
        else:
            matching_reports = []
            for evidence_id in evidence_ids:
                validity = evidence_validity.get(evidence_id)
                if validity is None:
                    local_issues.append(issue("EXECUTION_EVIDENCE_NOT_AVAILABLE", "PASS execution for %s references unavailable evidence %s" % (test_id, evidence_id), status="NOT_RUN", test_case_ids=[test_id] if isinstance(test_id, str) else None))
                    continue
                if not validity.get("valid"):
                    local_issues.append(issue("EXECUTION_INVALID_EVIDENCE", "PASS execution for %s references invalid evidence %s" % (test_id, evidence_id), status=validity.get("status", "FAIL"), test_case_ids=[test_id] if isinstance(test_id, str) else None))
                if validity.get("result") != "PASS":
                    local_issues.append(issue("EXECUTION_EVIDENCE_RESULT_MISMATCH", "PASS execution for %s links evidence %s whose result is not PASS" % (test_id, evidence_id), test_case_ids=[test_id] if isinstance(test_id, str) else None))
                if validity.get("command") != execution.get("command") or validity.get("exit_code") != execution.get("exit_code"):
                    local_issues.append(issue("EXECUTION_EVIDENCE_COMMAND_MISMATCH", "PASS execution for %s differs from evidence %s command or exit code" % (test_id, evidence_id), test_case_ids=[test_id] if isinstance(test_id, str) else None))
                if execution_id not in validity.get("execution_ids", []) or test_id not in validity.get("test_case_ids", []):
                    local_issues.append(issue("EXECUTION_EVIDENCE_LINK_MISMATCH", "PASS execution for %s is not consistently linked by evidence %s" % (test_id, evidence_id), test_case_ids=[test_id] if isinstance(test_id, str) else None))
                report = validity.get("test_report")
                if validity.get("valid") and validity.get("evidence_type") == "test_report" and isinstance(report, dict):
                    matching_reports.append(report)
            if not matching_reports:
                local_issues.append(issue("EXECUTION_PASS_WITHOUT_TEST_REPORT", "PASS execution for %s lacks a valid machine-parseable test_report" % test_id, status="NOT_RUN", test_case_ids=[test_id] if isinstance(test_id, str) else None))
            elif any(report.get("status") != "PASS" for report in matching_reports):
                local_issues.append(issue("EXECUTION_RUNNER_RESULT_MISMATCH", "PASS execution for %s conflicts with the parsed runner result" % test_id, test_case_ids=[test_id] if isinstance(test_id, str) else None))
    elif claimed == "FAIL":
        if not is_integer(tests_run) or tests_run < 1 or not is_integer(tests_failed) or tests_failed < 1:
            local_issues.append(issue("EXECUTION_FAIL_COUNTS_INVALID", "FAIL execution for %s lacks a recorded failing test" % test_id, test_case_ids=[test_id] if isinstance(test_id, str) else None))
    elif claimed == "BLOCKED":
        if not isinstance(execution.get("blocker"), str) or not execution["blocker"].strip():
            local_issues.append(issue("EXECUTION_BLOCKER_MISSING", "BLOCKED execution for %s lacks blocker reason" % test_id, test_case_ids=[test_id] if isinstance(test_id, str) else None))
    if is_integer(tests_skipped) and tests_skipped > 0:
        local_issues.append(issue("TESTS_SKIPPED", "execution for %s skipped %d tests" % (test_id, tests_skipped), test_case_ids=[test_id] if isinstance(test_id, str) else None))
    derived = worst_status([claimed] + [item["status"] for item in local_issues])
    return derived, local_issues


def _validate_pass_link_outcomes(indexes, evidence_validity, execution_outcomes, test_outcomes):
    """Require PASS declarations to link the exact PASS executions and evidence."""
    issues = []

    def pass_evidence_issue(owner, evidence_id, requirement_id, test_case_ids, prefix):
        validity = evidence_validity.get(evidence_id)
        if not isinstance(validity, dict):
            issues.append(issue(
                "%s_EVIDENCE_NOT_VERIFIED" % prefix,
                "%s links unavailable evidence %s" % (owner, evidence_id),
                status="NOT_RUN", requirement_ids=[requirement_id],
                test_case_ids=test_case_ids,
            ))
            return False
        if not validity.get("valid"):
            issues.append(issue(
                "%s_EVIDENCE_INVALID" % prefix,
                "%s links invalid evidence %s" % (owner, evidence_id),
                status=validity.get("status", "FAIL"), requirement_ids=[requirement_id],
                test_case_ids=test_case_ids,
            ))
            return False
        if validity.get("result") != "PASS":
            issues.append(issue(
                "%s_EVIDENCE_NOT_PASS" % prefix,
                "%s links evidence %s whose derived result is not PASS" % (owner, evidence_id),
                requirement_ids=[requirement_id], test_case_ids=test_case_ids,
            ))
            return False
        report = validity.get("test_report")
        if validity.get("evidence_type") == "test_report" and (
            not isinstance(report, dict) or report.get("status") != "PASS"
        ):
            issues.append(issue(
                "%s_TEST_REPORT_NOT_PASS" % prefix,
                "%s links test report %s without a parsed PASS result" % (owner, evidence_id),
                requirement_ids=[requirement_id], test_case_ids=test_case_ids,
            ))
            return False
        return True

    for trace_id, trace in indexes.get("traces", {}).items():
        verdict = trace.get("final_verdict") if isinstance(trace, dict) else None
        if not isinstance(verdict, dict) or verdict.get("status") != "PASS":
            continue
        requirement_id = trace.get("requirement_id")
        test_ids = _list_of_strings(trace.get("test_case_ids"))
        execution_ids = _list_of_strings(trace.get("test_execution_ids"))
        evidence_ids = _list_of_strings(trace.get("evidence_ids"))
        for execution_id in execution_ids:
            execution = indexes.get("executions", {}).get(execution_id, {})
            test_id = execution.get("test_case_id")
            if execution_outcomes.get(execution_id) != "PASS":
                derived_execution_status = execution_outcomes.get(execution_id, "NOT_RUN")
                issues.append(issue(
                    "TRACE_EXECUTION_NOT_PASS",
                    "%s links execution %s whose derived status is %s" % (
                        trace_id, execution_id, derived_execution_status,
                    ),
                    status=derived_execution_status,
                    requirement_ids=[requirement_id],
                    test_case_ids=[test_id] if isinstance(test_id, str) else test_ids,
                ))
        passing_evidence = {
            evidence_id for evidence_id in evidence_ids
            if pass_evidence_issue(trace_id, evidence_id, requirement_id, test_ids, "TRACE")
        }
        for test_id in test_ids:
            linked_pass_executions = {
                execution_id for execution_id in execution_ids
                if execution_outcomes.get(execution_id) == "PASS"
                and indexes.get("executions", {}).get(execution_id, {}).get("test_case_id") == test_id
            }
            if not linked_pass_executions:
                issues.append(issue(
                    "TRACE_TEST_WITHOUT_PASS_EXECUTION",
                    "%s test %s has no linked execution whose derived status is PASS" % (trace_id, test_id),
                    status="NOT_RUN", requirement_ids=[requirement_id], test_case_ids=[test_id],
                ))
                continue
            linked_chain = any(
                linked_pass_executions.intersection(
                    set(evidence_validity.get(evidence_id, {}).get("execution_ids", []))
                )
                and test_id in evidence_validity.get(evidence_id, {}).get("test_case_ids", [])
                for evidence_id in passing_evidence
            )
            if not linked_chain:
                issues.append(issue(
                    "TRACE_PASS_CHAIN_BROKEN",
                    "%s has no valid PASS evidence connecting test %s to its PASS execution" % (trace_id, test_id),
                    status="NOT_RUN", requirement_ids=[requirement_id], test_case_ids=[test_id],
                ))

    for requirement_id, acceptance in indexes.get("acceptance", {}).items():
        if not isinstance(acceptance, dict) or acceptance.get("status") != "PASS":
            continue
        test_ids = _list_of_strings(acceptance.get("test_case_ids"))
        evidence_ids = _list_of_strings(acceptance.get("evidence_ids"))
        passing_evidence = {
            evidence_id for evidence_id in evidence_ids
            if pass_evidence_issue(
                "acceptance %s" % requirement_id, evidence_id, requirement_id,
                test_ids, "ACCEPTANCE",
            )
        }
        for test_id in test_ids:
            if test_outcomes.get(test_id) != "PASS":
                derived_test_status = test_outcomes.get(test_id, "NOT_RUN")
                issues.append(issue(
                    "ACCEPTANCE_TEST_NOT_PASS",
                    "acceptance %s links test %s whose selected outcome is not PASS" % (requirement_id, test_id),
                    status=derived_test_status,
                    requirement_ids=[requirement_id], test_case_ids=[test_id],
                ))
            connected = False
            for evidence_id in passing_evidence:
                validity = evidence_validity.get(evidence_id, {})
                if test_id not in validity.get("test_case_ids", []):
                    continue
                if any(
                    execution_outcomes.get(execution_id) == "PASS"
                    and indexes.get("executions", {}).get(execution_id, {}).get("test_case_id") == test_id
                    for execution_id in validity.get("execution_ids", [])
                ):
                    connected = True
                    break
            if not connected:
                issues.append(issue(
                    "ACCEPTANCE_PASS_CHAIN_BROKEN",
                    "acceptance %s has no valid PASS evidence connecting test %s to a PASS execution" % (requirement_id, test_id),
                    status="NOT_RUN", requirement_ids=[requirement_id], test_case_ids=[test_id],
                ))
    return issues


def _run_applicable_checks(repo, config, indexes=None, evidence_validity=None):
    issues = []
    commands = []
    indexes = indexes or {}
    evidence_validity = evidence_validity or {}
    test_check_types = {
        "api": "api", "database": "database", "e2e": "e2e", "file": "file",
        "integration": "integration", "permission": "permission", "security": "security", "unit": "unit",
    }
    checks = config.get("checks") if isinstance(config.get("checks"), dict) else {}
    for check_name, check in sorted(checks.items()):
        if not isinstance(check, dict) or check.get("applicable") is not True:
            continue
        if check_name in test_check_types:
            test_ids = [
                test_id for test_id, test_case in indexes.get("tests", {}).items()
                if test_case.get("test_type") == test_check_types[check_name]
            ]
            passed = bool(test_ids)
            for test_id in test_ids:
                corroborated = any(
                    validity.get("valid")
                    and validity.get("result") == "PASS"
                    and test_id in validity.get("test_case_ids", [])
                    and isinstance(validity.get("test_report"), dict)
                    and isinstance(validity["test_report"].get("attestation"), dict)
                    and validity["test_report"]["attestation"].get("status") == "PASS"
                    for validity in evidence_validity.values() if isinstance(validity, dict)
                )
                if not corroborated:
                    passed = False
            if not passed:
                issues.append(issue(
                    "APPLICABLE_TEST_CHECK_NOT_PROVEN",
                    "applicable %s check lacks fresh PASS evidence for every %s test case" % (check_name, test_check_types[check_name]),
                    status="NOT_RUN",
                ))
            continue
        issues.append(issue(
            "APPLICABLE_CHECK_TRUSTED_RECEIPT_REQUIRED",
            "applicable check %s requires a protected external CI receipt; project config commands are not executed by the trusted local Hook" % check_name,
            status="BLOCKED",
            details={"check": check_name, "declared_command": check.get("command")},
        ))
    return issues, commands


COLLECTOR_ALLOWED_EMPTY_RUNTIME_CODES = frozenset((
    "TRACE_EXECUTION_NOT_RUN", "TRACE_EVIDENCE_NOT_AVAILABLE", "TRACE_MISSING_ACCEPTANCE",
    "TRACE_ACCEPTANCE_TEST_MISMATCH", "TRACE_ACCEPTANCE_EVIDENCE_MISMATCH",
    "TRACE_VERDICT_PENDING",
))


def _collector_static_preflight(repo):
    """Validate immutable inputs without trusting or requiring pre-existing runtime receipts."""
    issues = []
    try:
        config, config_issues = harden_config(load_config(repo))
        issues.extend(config_issues)
    except HarnessError as exc:
        config = {}
        issues.append(issue("CONFIG_READ_ERROR", str(exc)))
    issues.extend(_canonical_distribution_issues(repo))
    immutable_inputs = {
        ".harness/config.json",
        ".harness/state/requirements-baseline.json",
    }
    immutable_inputs.update("scripts/%s" % name for name in CANONICAL_SCRIPT_NAMES)
    immutable_inputs.update(".harness/schemas/%s" % name for name in CANONICAL_SCHEMA_NAMES)
    arrays = {name: [] for name, _filename, _schema, _root in ARTIFACTS}
    for artifact_name, filename, schema_name, root_key in ARTIFACTS:
        if artifact_name in {"test_executions", "evidence", "acceptance_results"}:
            continue
        relative = ".harness/spec/%s" % filename
        immutable_inputs.add(relative)
        try:
            data_path = _safe_repo_path(repo, relative, must_exist=True)
            document = strict_json_load(data_path)
            schema_path = find_project_schema(repo, schema_name)
            schema = strict_json_load(schema_path)
            validator = StrictSchemaValidator(schema, source=str(schema_path))
            for message in validator.validate(document):
                issues.append(issue("SCHEMA_VALIDATION_ERROR", message, path=relative))
            arrays[artifact_name] = extract_array(document, root_key)
        except HarnessError as exc:
            issues.append(issue("COLLECTOR_STATIC_INPUT_INVALID", str(exc), path=relative))
    for relative in sorted(immutable_inputs):
        try:
            _assert_git_head_file(repo, relative, 32 * 1024 * 1024)
        except (HarnessError, OSError, subprocess.SubprocessError) as exc:
            issues.append(issue(
                "COLLECTOR_INPUT_NOT_AT_HEAD",
                "%s is not the exact regular blob recorded at HEAD: %s" % (relative, exc),
                path=relative,
            ))
    semantic_issues, indexes = validate_semantics(repo, arrays, config)
    issues.extend(item for item in semantic_issues if item.get("code") not in COLLECTOR_ALLOWED_EMPTY_RUNTIME_CODES)
    issues.extend(_robot_orchestration_issues(repo, config, indexes))
    issues.extend(validate_requirement_baseline(repo, indexes, config, strict=True))
    git_info, git_issues, commands = git_snapshot(repo)
    issues.extend(git_issues)
    if git_info is None or git_info.get("dirty_paths"):
        issues.append(issue("COLLECTOR_GIT_NOT_CLEAN", "collector requires a clean, committed target repository"))
    return config, indexes, git_info, issues, commands


def _collector_trace_bindings(indexes, test_id):
    traces = [
        trace for trace in indexes.get("traces", {}).values()
        if test_id in _list_of_strings(trace.get("test_case_ids"))
    ]
    if not traces:
        raise HarnessError("test %s is not linked by any trace" % test_id)
    execution_ids = set()
    evidence_ids = set()
    for trace in traces:
        if _list_of_strings(trace.get("test_case_ids")) != [test_id]:
            raise HarnessError("collector requires each trace to bind exactly one test case: %s" % trace.get("trace_id"))
        execution_ids.update(_list_of_strings(trace.get("test_execution_ids")))
        evidence_ids.update(_list_of_strings(trace.get("evidence_ids")))
    if len(execution_ids) != 1 or len(evidence_ids) != 1:
        raise HarnessError("test %s must have exactly one stable execution ID and evidence ID across its traces" % test_id)
    return traces, next(iter(execution_ids)), next(iter(evidence_ids))


def collect_runtime(repo):
    """Run supported tests and atomically publish machine-derived runtime documents."""
    repo = find_repo(repo)
    config, indexes, git_info, preflight_issues, preflight_commands = _collector_static_preflight(repo)
    blocking = [item for item in preflight_issues if item.get("status") in {"FAIL", "BLOCKED", "NOT_RUN"}]
    if blocking:
        raise HarnessError("collector preflight rejected the repository: %s" % "; ".join(
            "%s: %s" % (item.get("code"), item.get("message")) for item in blocking
        ))
    if not indexes.get("tests"):
        raise HarnessError("collector requires at least one test case")
    automation_owners = {}
    execution_id_owners = {}
    evidence_id_owners = {}
    bindings = {}
    for test_id, test_case in sorted(indexes["tests"].items()):
        automation_path = test_case.get("automation_path")
        if not isinstance(automation_path, str) or not automation_path.endswith(".py"):
            raise HarnessError("test %s has no supported stdlib unittest .py automation_path" % test_id)
        if automation_path in automation_owners:
            raise HarnessError("collector requires one test case per automation_path: %s" % automation_path)
        automation_owners[automation_path] = test_id
        automation = _safe_repo_path(repo, automation_path, must_exist=True)
        if not automation.is_file() or automation.is_symlink():
            raise HarnessError("test %s automation is not a safe regular file" % test_id)
        if test_case.get("is_mocked") is not False:
            raise HarnessError("collector cannot derive production PASS from mocked test %s" % test_id)
        markers = _automation_mock_markers(automation)
        if markers:
            raise HarnessError("test %s contains common mocking constructs: %s" % (test_id, ", ".join(markers)))
        binding = _collector_trace_bindings(indexes, test_id)
        execution_id = binding[1]
        evidence_id = binding[2]
        if execution_id in execution_id_owners:
            raise HarnessError("collector execution ID %s is shared by tests %s and %s" % (execution_id, execution_id_owners[execution_id], test_id))
        if evidence_id in evidence_id_owners:
            raise HarnessError("collector evidence ID %s is shared by tests %s and %s" % (evidence_id, evidence_id_owners[evidence_id], test_id))
        execution_id_owners[execution_id] = test_id
        evidence_id_owners[evidence_id] = test_id
        bindings[test_id] = binding

    collection_run_id = new_run_id()
    runs_dir = _safe_repo_directory(repo, ".harness/runs", create=True)
    collection_dir = runs_dir / collection_run_id
    if collection_dir.exists() or collection_dir.is_symlink():
        raise HarnessError("collector run path already exists")
    collection_dir.mkdir(mode=0o755)
    executions = []
    evidence_items = []
    statuses = {}
    evidence_by_test = {}
    commands = list(preflight_commands)
    current_commit = git_info["git_commit"]
    for test_id, test_case in sorted(indexes["tests"].items()):
        traces, execution_id, evidence_id = bindings[test_id]
        automation_path = test_case["automation_path"]
        automation = _safe_repo_path(repo, automation_path, must_exist=True)
        automation_hash, _size = _hash_regular_file(automation, 2 * 1024 * 1024)
        attestation = _fresh_unittest_attestation(repo, automation_path, None)
        commands.append(dict(attestation, check_name="collector:%s" % test_id))
        identifiers = attestation.get("test_identifiers") or []
        if not identifiers:
            raise HarnessError("collector observed no exact unittest identifiers for %s" % test_id)
        status = attestation["status"]
        summary = attestation["summary"]
        statuses[test_id] = status
        artifact_path = collection_dir / ("test-report-%s.json" % re.sub(r"[^A-Za-z0-9._-]", "_", test_id))
        command_text = shlex.join(attestation["argv"])
        report = {
            "format": TEST_REPORT_FORMAT,
            "runner": "unittest",
            "tool_version": "python-%s" % sys.version.split()[0],
            "command": command_text,
            "cwd": ".",
            "started_at": attestation["started_at"],
            "completed_at": attestation["completed_at"],
            "exit_code": attestation["exit_code"],
            "status": status,
            "git_commit": current_commit,
            "execution_id": execution_id,
            "test_case_id": test_id,
            "runner_path": automation_path,
            "runner_sha256": automation_hash,
            "automation_path": automation_path,
            "automation_sha256": automation_hash,
            "test_identifiers": identifiers,
            "summary": summary,
            "raw_output": (attestation.get("stdout") or "") + (attestation.get("stderr") or ""),
        }
        atomic_write_json(artifact_path, report)
        artifact_hash, _artifact_size = _hash_regular_file(artifact_path, 32 * 1024 * 1024)
        requirement_ids = sorted(set(
            requirement_id for trace in traces
            for requirement_id in [trace.get("requirement_id")]
            if isinstance(requirement_id, str)
        ))
        criterion_ids = sorted(set(
            criterion_id for trace in traces
            for criterion_id in [trace.get("acceptance_criterion_id")]
            if isinstance(criterion_id, str)
        ))
        environment = test_case.get("environment") if isinstance(test_case.get("environment"), dict) else {}
        runtime_environment = {
            "name": environment.get("name") or "harness-collector",
            "os": environment.get("os") or sys.platform,
            "details": ["sandboxed stdlib unittest collector"],
        }
        failure_reason = "fresh collector unittest failed" if status == "FAIL" else None
        blocker = "fresh collector could not execute the test" if status == "BLOCKED" else None
        executions.append({
            "execution_id": execution_id, "test_case_id": test_id,
            "command": command_text, "cwd": ".",
            "started_at": attestation["started_at"], "completed_at": attestation["completed_at"],
            "status": status, "exit_code": attestation["exit_code"],
            "tests_run": summary["tests_run"], "tests_passed": summary["tests_passed"],
            "tests_failed": summary["tests_failed"], "tests_skipped": summary["tests_skipped"],
            "evidence_ids": [evidence_id], "git_commit": current_commit,
            "environment": runtime_environment, "runner": "unittest",
            "failure_reason": failure_reason, "blocker": blocker,
            "notes": "machine-derived by the trusted Harness collector",
        })
        evidence_items.append({
            "evidence_id": evidence_id, "execution_ids": [execution_id],
            "requirement_ids": requirement_ids, "acceptance_criterion_ids": criterion_ids,
            "test_case_ids": [test_id], "evidence_type": "test_report",
            "path": artifact_path.relative_to(repo).as_posix(), "sha256": artifact_hash,
            "command": command_text, "exit_code": attestation["exit_code"],
            "started_at": attestation["started_at"], "completed_at": attestation["completed_at"],
            "git_commit": current_commit, "environment": runtime_environment,
            "tool_version": "python-%s" % sys.version.split()[0], "result": status,
            "notes": "sandboxed collector evidence",
        })
        evidence_by_test[test_id] = evidence_id

    acceptance_results = []
    acceptance_statuses = {}
    acceptance_tests = {}
    acceptance_evidence = {}
    acceptance_locations = {}
    for requirement_id, requirement in sorted(indexes["requirements"].items()):
        test_ids = _list_of_strings(requirement.get("test_case_ids"))
        linked_statuses = [statuses.get(test_id, "NOT_RUN") for test_id in test_ids]
        acceptance_statuses[requirement_id] = worst_status(linked_statuses) if linked_statuses else "NOT_RUN"
        acceptance_tests[requirement_id] = set(test_ids)
        acceptance_evidence[requirement_id] = {evidence_by_test[test_id] for test_id in test_ids if test_id in evidence_by_test}
        acceptance_locations[requirement_id] = {
            reference.get("path")
            for trace in indexes.get("traces", {}).values()
            if trace.get("requirement_id") == requirement_id
            for reference in (trace.get("implementation", {}).get("references", []) if isinstance(trace.get("implementation"), dict) else [])
            if isinstance(reference, dict) and isinstance(reference.get("path"), str)
        }
    ordered_requirements = sorted(
        indexes["requirements"].items(), key=lambda item: item[1].get("level", 0), reverse=True,
    )
    for requirement_id, requirement in ordered_requirements:
        if requirement.get("node_type") != "group" or acceptance_tests.get(requirement_id):
            continue
        children = indexes.get("children", {}).get(requirement_id, [])
        acceptance_statuses[requirement_id] = worst_status(
            [acceptance_statuses.get(child, "NOT_RUN") for child in children]
        ) if children else "NOT_RUN"
        acceptance_tests[requirement_id] = set().union(*(acceptance_tests.get(child, set()) for child in children)) if children else set()
        acceptance_evidence[requirement_id] = set().union(*(acceptance_evidence.get(child, set()) for child in children)) if children else set()
        acceptance_locations[requirement_id] = set().union(*(acceptance_locations.get(child, set()) for child in children)) if children else set()

    for requirement_id, requirement in sorted(indexes["requirements"].items()):
        status = acceptance_statuses[requirement_id]
        test_ids = sorted(acceptance_tests[requirement_id])
        evidence_ids = sorted(acceptance_evidence[requirement_id])
        implementation_locations = sorted(acceptance_locations[requirement_id])
        acceptance_results.append({
            "requirement_id": requirement_id, "status": status,
            "implementation_locations": implementation_locations,
            "test_case_ids": test_ids, "evidence_ids": evidence_ids,
            "verified_by": {
                "agent_id": "HARNESS-COLLECTOR", "role": "runtime_acceptance",
                "independent_of_implementation": True,
            },
            "verified_at": utc_now(),
            "failure_reason": "one or more fresh collector tests failed" if status == "FAIL" else None,
            "blocker": "fresh collector was blocked" if status == "BLOCKED" else None,
            "unverified_scope": ["one or more tests were not run"] if status == "NOT_RUN" else [],
        })

    collected_traces = []
    for trace_id, trace in sorted(indexes.get("traces", {}).items()):
        collected_trace = json.loads(json.dumps(trace))
        requirement_id = collected_trace.get("requirement_id")
        status = acceptance_statuses.get(requirement_id, "NOT_RUN")
        collected_trace["final_verdict"] = {
            "state": "LINKED",
            "acceptance_result_requirement_id": requirement_id,
            "status": status,
            "reason": None,
        }
        collected_traces.append(collected_trace)

    state_dir = _safe_repo_directory(repo, ".harness/state", create=True)
    documents = {
        "test-executions.json": {
            "standard_version": STANDARD_VERSION, "run_id": collection_run_id,
            "git_commit": current_commit, "test_executions": executions,
        },
        "evidence.json": {
            "standard_version": STANDARD_VERSION, "run_id": collection_run_id,
            "git_commit": current_commit, "evidence": evidence_items,
        },
        "acceptance-results.json": {
            "standard_version": STANDARD_VERSION, "run_id": collection_run_id,
            "git_commit": current_commit, "acceptance_results": acceptance_results,
        },
        "traceability.json": {
            "standard_version": STANDARD_VERSION, "traceability": collected_traces,
        },
    }
    schema_by_filename = {
        "test-executions.json": "test-execution.schema.json",
        "evidence.json": "evidence.schema.json",
        "acceptance-results.json": "acceptance-result.schema.json",
        "traceability.json": "traceability.schema.json",
    }
    for filename, document in sorted(documents.items()):
        schema_path = find_project_schema(repo, schema_by_filename[filename])
        validator = StrictSchemaValidator(strict_json_load(schema_path), source=str(schema_path))
        schema_messages = validator.validate(document)
        if schema_messages:
            raise HarnessError(
                "collector generated invalid %s: %s" % (filename, "; ".join(schema_messages))
            )
    collected_arrays = {
        "requirements": list(indexes.get("requirements", {}).values()),
        "change_ledger": list(indexes.get("changes", {}).values()),
        "test_cases": list(indexes.get("tests", {}).values()),
        "test_executions": executions,
        "evidence": evidence_items,
        "traceability": collected_traces,
        "acceptance_results": acceptance_results,
    }
    collected_semantic_issues, _collected_indexes = validate_semantics(repo, collected_arrays, config)
    collected_blocking = [
        item for item in collected_semantic_issues
        if item.get("status") in {"FAIL", "BLOCKED", "NOT_RUN"}
    ]
    if collected_blocking:
        raise HarnessError("collector generated semantically invalid runtime documents: %s" % "; ".join(
            "%s: %s" % (item.get("code"), item.get("message")) for item in collected_blocking
        ))
    for filename, document in documents.items():
        target_relative = ".harness/state/%s" % filename
        target = _safe_repo_path(repo, target_relative, must_exist=False)
        atomic_write_json(target, document)
    manifest_artifacts = []
    for filename in sorted(documents):
        relative = ".harness/state/%s" % filename
        target = _safe_repo_path(repo, relative, must_exist=True)
        digest, size = _hash_regular_file(target, 32 * 1024 * 1024)
        manifest_artifacts.append({"path": relative, "sha256": digest, "bytes": size})
    atomic_write_json(_safe_repo_path(repo, ".harness/state/collection-manifest.json", must_exist=False), {
        "standard_version": STANDARD_VERSION, "run_id": collection_run_id,
        "git_commit": current_commit, "artifacts": manifest_artifacts,
    })
    return {
        "standard_version": STANDARD_VERSION,
        "kind": "collection_result",
        "repo": str(repo),
        "run_id": collection_run_id,
        "git_commit": current_commit,
        "statuses": statuses,
        "commands": commands,
        "state_files": sorted(".harness/state/%s" % name for name in documents),
    }


def derive_outcomes(indexes, evidence_validity, current_commit, strict=False):
    issues = []
    executions_by_test = {}
    execution_outcomes = {}
    for execution_id, execution in indexes["executions"].items():
        test_id = execution.get("test_case_id")
        status, local_issues = _execution_status(execution_id, execution, current_commit, evidence_validity)
        execution_outcomes[execution_id] = status
        issues.extend(local_issues)
        if isinstance(test_id, str):
            executions_by_test.setdefault(test_id, []).append((execution_id, execution, status))

    test_outcomes = {}
    for test_id, test_case in indexes["tests"].items():
        candidates = executions_by_test.get(test_id, [])
        if not candidates:
            test_outcomes[test_id] = "NOT_RUN"
            issues.append(issue("TEST_NOT_RUN", "test has no execution: %s" % test_id, status="NOT_RUN", test_case_ids=[test_id]))
            continue
        normalized = []
        for candidate in candidates:
            try:
                completed = parse_datetime(candidate[1].get("completed_at"))
            except (ValueError, TypeError):
                completed = _dt.datetime.min.replace(tzinfo=_dt.timezone.utc)
            normalized.append((completed, candidate))
        latest_time = max(item[0] for item in normalized)
        latest_candidates = [item[1] for item in normalized if item[0] == latest_time]
        latest_status = worst_status(item[2] for item in latest_candidates)
        if len({item[2] for item in latest_candidates}) > 1:
            issues.append(issue(
                "TEST_EXECUTION_TIME_COLLISION",
                "test %s has conflicting executions at the same absolute completed_at" % test_id,
                test_case_ids=[test_id],
            ))
            latest_status = "FAIL"
        test_outcomes[test_id] = latest_status
        if latest_status != "PASS":
            issues.append(issue("TEST_OUTCOME_%s" % latest_status, "latest execution for %s is %s" % (test_id, latest_status), status=latest_status, test_case_ids=[test_id]))

    requirement_outcomes = {}
    for requirement_id, requirement in indexes["requirements"].items():
        linked_tests = _list_of_strings(requirement.get("test_case_ids"))
        if requirement.get("node_type") == "group" and not linked_tests:
            child_ids = indexes.get("children", {}).get(requirement_id, [])
            child_statuses = [requirement_outcomes.get(child_id, "PASS") for child_id in child_ids]
            requirement_outcomes[requirement_id] = worst_status(child_statuses) if child_statuses else "NOT_RUN"
            continue
        if not linked_tests:
            requirement_outcomes[requirement_id] = "NOT_RUN"
            issues.append(issue("CRITICAL_REQUIREMENT_WITHOUT_TEST" if requirement.get("criticality") == "critical" else "REQUIREMENT_WITHOUT_TEST", "requirement has no test: %s" % requirement_id, status="NOT_RUN", requirement_ids=[requirement_id]))
            continue
        statuses = [test_outcomes.get(test_id, "NOT_RUN") for test_id in linked_tests]
        outcome = worst_status(statuses)
        if outcome == "PASS":
            nonmocked_pass = any(
                test_outcomes.get(test_id) == "PASS" and not indexes["tests"].get(test_id, {}).get("is_mocked", False)
                for test_id in linked_tests
            )
            if not nonmocked_pass:
                outcome = "NOT_RUN"
                code = "CRITICAL_ONLY_MOCKED" if requirement.get("criticality") == "critical" else "REQUIREMENT_ONLY_MOCKED"
                issues.append(issue(code, "requirement has no passing non-mocked test: %s" % requirement_id, status="NOT_RUN", requirement_ids=[requirement_id]))
        if outcome == "PASS":
            for contract_id, owner in indexes.get("contracts", {}).items():
                if owner[0] != requirement_id:
                    continue
                covered = any(
                    contract_id in _list_of_strings(indexes["tests"].get(test_id, {}).get("contract_ids"))
                    and indexes["tests"].get(test_id, {}).get("is_mocked") is False
                    and test_outcomes.get(test_id) == "PASS"
                    for test_id in linked_tests
                )
                if not covered:
                    outcome = "NOT_RUN"
                    issues.append(issue(
                        "CONTRACT_NOT_PROVEN_BY_PASS_TEST",
                        "contract %s lacks a linked non-mocked PASS test execution" % contract_id,
                        status="NOT_RUN", requirement_ids=[requirement_id],
                    ))
        requirement_outcomes[requirement_id] = outcome

    # Recompute group results bottom-up now that child leaves have outcomes.
    ordered = sorted(indexes["requirements"].items(), key=lambda item: item[1].get("level", 0), reverse=True)
    for requirement_id, requirement in ordered:
        if requirement.get("node_type") == "group" and not _list_of_strings(requirement.get("test_case_ids")):
            children = indexes.get("children", {}).get(requirement_id, [])
            requirement_outcomes[requirement_id] = worst_status([requirement_outcomes.get(child, "NOT_RUN") for child in children]) if children else "NOT_RUN"

    for requirement_id, acceptance in indexes["acceptance"].items():
        computed = requirement_outcomes.get(requirement_id, "FAIL")
        claimed = acceptance.get("status")
        if claimed != computed:
            issues.append(issue("ACCEPTANCE_STATUS_MISMATCH", "acceptance for %s claims %s but computed status is %s" % (requirement_id, claimed, computed), status=computed, requirement_ids=[requirement_id]))
        if claimed == "PASS" and not acceptance.get("evidence_ids"):
            issues.append(issue("ACCEPTANCE_FALSE_PASS", "acceptance for %s claims PASS without evidence" % requirement_id, requirement_ids=[requirement_id]))

    for requirement_id, outcome in requirement_outcomes.items():
        if requirement_id not in indexes["acceptance"]:
            issues.append(issue("ACCEPTANCE_RESULT_MISSING", "requirement lacks acceptance result: %s" % requirement_id, status="NOT_RUN", requirement_ids=[requirement_id]))
    issues.extend(_validate_pass_link_outcomes(
        indexes, evidence_validity, execution_outcomes, test_outcomes,
    ))
    return issues, execution_outcomes, test_outcomes, requirement_outcomes


FOCUS_PATTERNS = (
    re.compile(r"\b(?:describe|context|it|test|suite)\s*\.\s*only\s*\("),
    re.compile(r"\b(?:fdescribe|fit)\s*\("),
)
SKIP_PATTERNS = (
    re.compile(r"\b(?:describe|context|it|test|suite)\s*\.\s*skip\s*\("),
    re.compile(r"\b(?:xdescribe|xit)\s*\("),
    re.compile(r"(?:pytest\.mark\.skip|pytest\.skip\s*\(|unittest\.skip\s*\(|@Disabled\b|#\[ignore\]|\bt\.Skip\s*\()"),
)
PLACEHOLDER_PATTERNS = (
    re.compile(r"\b(?:TODO|FIXME)\b", re.IGNORECASE),
    re.compile(r"\braise\s+NotImplementedError\b"),
    re.compile(r"^\s*pass\s*(?:#.*)?$", re.MULTILINE),
    re.compile(r"\bplaceholder\b", re.IGNORECASE),
    re.compile(r"\bmock(?:ed)?[_ -]?data\b", re.IGNORECASE),
)
SOURCE_TEXT_SUFFIXES = frozenset((
    ".c", ".cc", ".cpp", ".css", ".cs", ".go", ".h", ".hpp", ".html",
    ".java", ".js", ".jsx", ".kt", ".kts", ".m", ".mm", ".php", ".pl",
    ".py", ".rb", ".rs", ".scala", ".sh", ".sql", ".swift", ".tsx",
    ".ts", ".vue", ".xml", ".graphql", ".gql",
))
CANONICAL_TOOL_PATHS = frozenset("scripts/%s" % name for name in CANONICAL_SCRIPT_NAMES)


def scan_sources(repo, indexes, config=None, strict=False):
    config = config or {}
    repo = Path(repo).resolve()
    candidates = {}
    declared_paths = set()
    test_path_to_ids = {}
    for test_id, test_case in indexes["tests"].items():
        value = test_case.get("automation_path")
        if isinstance(value, str) and value:
            normalized = Path(value).as_posix()
            candidates[normalized] = "test"
            declared_paths.add(normalized)
            test_path_to_ids.setdefault(normalized, []).append(test_id)
    for trace in indexes.get("traces", {}).values():
        if not isinstance(trace, dict):
            continue
        for lane_name in ("implementation", "frontend", "backend", "storage", "permissions"):
            lane = trace.get(lane_name)
            if not isinstance(lane, dict) or lane.get("state") != "LINKED":
                continue
            for reference in lane.get("references", []) if isinstance(lane.get("references"), list) else []:
                value = reference.get("path") if isinstance(reference, dict) else None
                if isinstance(value, str) and value.strip():
                    normalized = Path(value).as_posix()
                    candidates.setdefault(normalized, "source")
                    declared_paths.add(normalized)
    configured_roots = config.get("source_scan_paths", [])
    if not isinstance(configured_roots, list):
        configured_roots = []
    for value in configured_roots:
        if isinstance(value, str) and value.strip():
            normalized = Path(value).as_posix()
            kind = "test" if normalized.split("/", 1)[0] in {"test", "tests"} else "source"
            candidates.setdefault(normalized, kind)
    for value in DEFAULT_SOURCE_SCAN_PATHS:
        if (repo / value).exists():
            kind = "test" if value in {"test", "tests"} else "source"
            candidates.setdefault(value, kind)
    raw_excludes = []
    if isinstance(config.get("scan_excludes"), list):
        raw_excludes.extend(config.get("scan_excludes", []))
    heuristics = config.get("heuristics") if isinstance(config.get("heuristics"), dict) else {}
    if isinstance(heuristics.get("exclude_paths"), list):
        raw_excludes.extend(heuristics.get("exclude_paths", []))
    excludes = tuple(dict.fromkeys(
        str(item).replace("\\", "/").strip("/") for item in raw_excludes
        if isinstance(item, str) and item.strip("/ ")
    ))

    def excluded(relative_path):
        normalized_path = relative_path.replace("\\", "/").strip("/")
        parts = normalized_path.split("/") if normalized_path else []
        for prefix in excludes:
            if normalized_path == prefix or normalized_path.startswith(prefix + "/"):
                return True
            if "/" not in prefix and prefix in parts:
                return True
        return False
    issues = []
    findings = []
    scanned_files = set()
    discovered = {}
    for relative, kind in sorted(candidates.items()):
        normalized = relative.replace("\\", "/")
        if excluded(normalized):
            issues.append(issue(
                "CONFIG_SCAN_REQUIRED_PATH_EXCLUDED",
                "scan exclusions cover required/configured source path: %s" % normalized,
                path=normalized,
            ))
            if normalized not in declared_paths:
                continue
        try:
            path = _safe_repo_path(repo, relative, must_exist=True)
            if not path.is_file() and not path.is_dir():
                raise HarnessError("scan path is neither a regular file nor directory")
        except (HarnessError, OSError) as exc:
            issues.append(issue(
                "SOURCE_SCAN_PATH_INVALID",
                "cannot scan %s: %s" % (normalized, exc),
                status="FAIL" if normalized in declared_paths or normalized in configured_roots else "NOT_RUN",
                path=normalized,
            ))
            continue
        paths = []
        if path.is_dir():
            paths.extend(item for item in path.rglob("*") if item.is_file())
        else:
            paths.append(path)
        for file_path in paths:
            try:
                rel = file_path.relative_to(repo).as_posix()
                safe_file = _safe_repo_path(repo, rel, must_exist=True)
                if not safe_file.is_file():
                    raise HarnessError("source candidate is not a regular file")
            except (HarnessError, OSError, ValueError) as exc:
                issues.append(issue("SOURCE_SCAN_FILE_INVALID", "%s: %s" % (file_path, exc), path=str(file_path)))
                continue
            if rel in CANONICAL_TOOL_PATHS:
                continue
            if rel not in declared_paths and excluded(rel):
                continue
            if any(part in {".git", "node_modules", "vendor", "dist", "build", "coverage", ".harness", "__pycache__", ".venv", "venv"} for part in safe_file.parts):
                continue
            if rel not in declared_paths and safe_file.suffix.lower() not in SOURCE_TEXT_SUFFIXES:
                continue
            effective_kind = "test" if (
                kind == "test" or rel.split("/", 1)[0] in {"test", "tests"}
            ) else kind
            previous_kind = discovered.get(rel)
            if previous_kind == "test" or effective_kind == "test":
                discovered[rel] = "test"
            else:
                discovered[rel] = "source"

    for rel, effective_kind in sorted(discovered.items()):
        if rel in scanned_files:
            continue
        try:
            safe_file = _safe_repo_path(repo, rel, must_exist=True)
            if not safe_file.is_file():
                raise HarnessError("source candidate is not a regular file")
        except (HarnessError, OSError) as exc:
            issues.append(issue("SOURCE_SCAN_FILE_INVALID", "%s: %s" % (rel, exc), path=rel))
            continue
        try:
            if safe_file.stat().st_size > 2 * 1024 * 1024:
                raise HarnessError("file exceeds 2 MiB source scan limit")
            text = safe_file.read_text(encoding="utf-8")
        except (HarnessError, OSError, UnicodeError) as exc:
            if rel in declared_paths:
                issues.append(issue(
                    "SOURCE_SCAN_FILE_UNREADABLE",
                    "cannot inspect %s: %s" % (rel, exc),
                    status="NOT_RUN", path=rel,
                ))
            continue
        scanned_files.add(rel)
        test_ids = test_path_to_ids.get(rel, [])
        for label, patterns, finding_status in (
            ("FOCUSED_TEST", FOCUS_PATTERNS, "FAIL"),
            ("SKIPPED_TEST_MARKER", SKIP_PATTERNS, "FAIL"),
            ("PLACEHOLDER_REVIEW", PLACEHOLDER_PATTERNS, "BLOCKED"),
        ):
            if effective_kind != "test" and label != "PLACEHOLDER_REVIEW":
                continue
            for pattern in patterns:
                match = pattern.search(text)
                if match is None:
                    continue
                line = text.count("\n", 0, match.start()) + 1
                finding = {
                    "code": label,
                    "path": rel,
                    "line": line,
                    "status": finding_status,
                    "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "test_case_ids": sorted(test_ids),
                }
                findings.append(finding)
                if label != "PLACEHOLDER_REVIEW" or strict:
                    issues.append(issue(label, "%s detected at %s:%d" % (label, rel, line), status=finding_status, path=rel, test_case_ids=test_ids))
                break
    if indexes.get("requirements") and not scanned_files:
        issues.append(issue(
            "SOURCE_SCAN_EMPTY",
            "non-empty requirements produced no readable implementation or test source candidates",
            status="NOT_RUN",
        ))
    return issues, findings


def _count_statuses(mapping):
    counts = {status.lower(): 0 for status in STATUS_VALUES}
    for status in mapping.values():
        key = status.lower() if isinstance(status, str) else "fail"
        if key in counts:
            counts[key] += 1
        else:
            counts["fail"] += 1
    counts["total"] = len(mapping)
    return counts


def _environment_summary():
    try:
        uname = os.uname()
        os_name = "%s %s %s" % (uname.sysname, uname.release, uname.machine)
    except AttributeError:
        os_name = sys.platform
    return {
        "name": "software-delivery-harness",
        "os": os_name,
        "details": [
            "Python %s" % sys.version.split()[0],
            "Executable %s" % sys.executable,
        ],
    }


def _project_name(repo, config):
    for key in ("project_name", "name"):
        if isinstance(config.get(key), str) and config[key].strip():
            return config[key].strip()[:256]
    return (Path(repo).name or "unnamed-project")[:256]


def build_report(repo, run_id, started_at, completed_at, git_info, config, issues, indexes,
                 evidence_validity, test_outcomes, requirement_outcomes, findings, commands):
    overall = worst_status([item.get("status") for item in issues] + list(requirement_outcomes.values()) + list(test_outcomes.values()))
    if not indexes.get("requirements") or not indexes.get("tests"):
        overall = worst_status([overall, "NOT_RUN"])
    def report_issue(item):
        requirement_ids = item.get("requirement_ids") or []
        test_case_ids = item.get("test_case_ids") or []
        return {
            "id": re.sub(r"[^A-Z0-9_.-]", "_", str(item.get("code") or "HARNESS_ISSUE").upper()),
            "requirement_id": requirement_ids[0] if requirement_ids else None,
            "test_case_id": test_case_ids[0] if test_case_ids else None,
            "message": str(item.get("message") or "unspecified harness issue"),
            "evidence_ids": sorted(set(item.get("evidence_ids") or [])),
        }
    failures = [report_issue(item) for item in issues if item.get("status") == "FAIL"]
    blockers = [report_issue(item) for item in issues if item.get("status") == "BLOCKED"]
    not_run = [report_issue(item) for item in issues if item.get("status") == "NOT_RUN"]
    issue_codes = [str(item.get("code") or "") for item in issues]
    evidence_counts = {
        "total": len(evidence_validity),
        "verified": sum(1 for value in evidence_validity.values() if value.get("valid")),
        "missing": sum(1 for code in issue_codes if code in {"EVIDENCE_FILE_INVALID", "EXECUTION_PASS_WITHOUT_EVIDENCE", "ACCEPTANCE_MISSING_EVIDENCE"}),
        "hash_mismatch": sum(1 for code in issue_codes if code == "EVIDENCE_HASH_MISMATCH"),
        "commit_mismatch": sum(1 for code in issue_codes if code in {"EVIDENCE_COMMIT_MISMATCH", "EXECUTION_COMMIT_MISMATCH", "RUNTIME_ENVELOPE_COMMIT_MISMATCH"}),
    }
    requirement_details = []
    for requirement_id in sorted(indexes.get("requirements", {})):
        requirement = indexes["requirements"][requirement_id]
        acceptance = indexes.get("acceptance", {}).get(requirement_id, {})
        requirement_details.append({
            "requirement_id": requirement_id,
            "status": requirement_outcomes.get(requirement_id, "NOT_RUN"),
            "test_case_ids": sorted(set(
                _list_of_strings(requirement.get("test_case_ids"))
                or _list_of_strings(acceptance.get("test_case_ids"))
            )),
            "evidence_ids": sorted(_list_of_strings(acceptance.get("evidence_ids"))),
            "reason": str(
                acceptance.get("failure_reason")
                or acceptance.get("blocker")
                or ("; ".join(acceptance.get("unverified_scope", [])) if isinstance(acceptance.get("unverified_scope"), list) else "")
                or "status computed from linked executions and verified evidence"
            ),
        })
    command_details = []
    recorded_commands = set()
    verification_result_path = ".harness/runs/%s/verification-result.json" % run_id
    for command in commands:
        detail = {
            "command": " ".join(command.get("argv", [])),
            "cwd": command.get("cwd"),
            "started_at": command.get("started_at"),
            "completed_at": command.get("completed_at"),
            "exit_code": command.get("exit_code"),
            "status": "PASS" if command.get("exit_code") == 0 else "FAIL",
            "stdout_path": verification_result_path if command.get("stdout") else None,
            "stderr_path": verification_result_path if command.get("stderr") else None,
        }
        command_details.append(detail)
        recorded_commands.add((detail["command"], detail["started_at"], detail["completed_at"]))
    for execution_id, execution in sorted(indexes.get("executions", {}).items()):
        command_value = execution.get("command")
        if not isinstance(command_value, str) or not command_value.strip():
            continue
        if not all(isinstance(execution.get(name), str) and execution[name] for name in ("cwd", "started_at", "completed_at")):
            continue
        if not is_integer(execution.get("exit_code")):
            continue
        key = (command_value, execution["started_at"], execution["completed_at"])
        if key in recorded_commands:
            continue
        stdout_path = None
        stderr_path = None
        for evidence_id in _list_of_strings(execution.get("evidence_ids")):
            validity = evidence_validity.get(evidence_id, {})
            if not validity.get("valid") or not isinstance(validity.get("path"), str):
                continue
            if validity.get("evidence_type") in {"stdout", "test_report"} and stdout_path is None:
                stdout_path = validity["path"]
            if validity.get("evidence_type") == "stderr" and stderr_path is None:
                stderr_path = validity["path"]
        command_details.append({
            "command": command_value,
            "cwd": execution["cwd"],
            "started_at": execution["started_at"],
            "completed_at": execution["completed_at"],
            "exit_code": execution["exit_code"],
            "status": execution.get("status") if execution.get("status") in STATUS_VALUES else "FAIL",
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
        })
        recorded_commands.add(key)
    for evidence_id, validity in sorted(evidence_validity.items()):
        report_value = validity.get("test_report") if isinstance(validity, dict) else None
        attestation = report_value.get("attestation") if isinstance(report_value, dict) else None
        if not isinstance(attestation, dict):
            continue
        argv = attestation.get("argv")
        command_value = " ".join(argv) if isinstance(argv, list) else ""
        key = (command_value, attestation.get("started_at"), attestation.get("completed_at"))
        if not command_value or key in recorded_commands:
            continue
        command_details.append({
            "command": command_value,
            "cwd": attestation.get("cwd"),
            "started_at": attestation.get("started_at"),
            "completed_at": attestation.get("completed_at"),
            "exit_code": attestation.get("exit_code"),
            "status": attestation.get("status") if attestation.get("status") in STATUS_VALUES else "FAIL",
            "stdout_path": verification_result_path if attestation.get("stdout") else None,
            "stderr_path": verification_result_path if attestation.get("stderr") else None,
        })
        recorded_commands.add(key)
    verdict_reason = "all deterministic release checks passed" if overall == "PASS" else (
        "%d failure(s), %d blocker(s), and %d not-run finding(s) prevent release"
        % (len(failures), len(blockers), len(not_run))
    )
    requirement_counts = _count_statuses(requirement_outcomes)
    requirement_counts["critical_total"] = sum(
        1 for value in indexes.get("requirements", {}).values() if value.get("criticality") == "critical"
    )
    test_counts = _count_statuses(test_outcomes)
    test_counts["skipped"] = sum(
        int(value.get("tests_skipped", 0))
        for value in indexes.get("executions", {}).values()
        if is_integer(value.get("tests_skipped"))
    )
    test_counts["mocked"] = sum(1 for value in indexes.get("tests", {}).values() if value.get("is_mocked") is True)
    artifacts = []
    for evidence_id, value in sorted(evidence_validity.items()):
        if value.get("valid") and isinstance(value.get("path"), str) and isinstance(value.get("sha256"), str):
            artifacts.append({"path": value["path"], "sha256": value["sha256"], "artifact_type": "evidence:%s" % evidence_id})
    known_limitations = [
        "Declared approved_by identities require an external signed review or protected branch for authority proof.",
        "Fresh local test execution proves the process ran on the current checkout; external CI attestation is still required for independent provenance.",
        "Acceptance verifier independence is declared metadata; independent agent execution requires an external thread or audit receipt.",
        "Heuristic placeholder findings require human review; they are not asserted as deterministic product bugs.",
    ]
    for finding in findings:
        known_limitations.append("Unresolved heuristic %s at %s:%s." % (finding.get("code"), finding.get("path"), finding.get("line")))
    return {
        "standard_version": STANDARD_VERSION,
        "project_name": _project_name(repo, config),
        "run_id": run_id,
        "git_commit": git_info.get("git_commit") if git_info else None,
        "environment": _environment_summary(),
        "started_at": started_at,
        "completed_at": completed_at,
        "overall_status": overall,
        "release_allowed": overall == "PASS",
        "requirement_counts": requirement_counts,
        "test_counts": test_counts,
        "evidence_counts": evidence_counts,
        "requirements": requirement_details,
        "failures": failures,
        "blockers": blockers,
        "not_run_items": not_run,
        "scope_changes": sorted(indexes.get("changes", {}).keys()),
        "commands_executed": command_details,
        "artifacts": artifacts,
        "known_limitations": list(dict.fromkeys(known_limitations)),
        "verdict_reason": verdict_reason,
    }


def build_fail_closed_report(repo, code, message, run_id=None):
    """Build a complete release-report-shaped FAIL result without trusting project data."""
    try:
        resolved_repo = find_repo(repo)
    except BaseException:
        try:
            resolved_repo = Path(repo or os.getcwd()).expanduser().resolve(strict=False)
        except BaseException:
            resolved_repo = Path(os.getcwd()).resolve(strict=False)
    started_at = utc_now()
    failure_issue = issue(code, message, status="FAIL")
    indexes = {
        name: {}
        for name in (
            "requirements", "changes", "tests", "executions", "evidence",
            "traces", "acceptance",
        )
    }
    return build_report(
        resolved_repo,
        validate_run_id(run_id) if run_id is not None else new_run_id(),
        started_at,
        utc_now(),
        None,
        {},
        [failure_issue],
        indexes,
        {},
        {},
        {},
        [],
        [],
    )


def _release_report_invariant_issues(report):
    issues = []
    if not isinstance(report, dict):
        return [issue("RELEASE_REPORT_INVARIANT_ERROR", "release report must be an object")]
    overall = report.get("overall_status")
    release_allowed = report.get("release_allowed")
    if release_allowed != (overall == "PASS"):
        issues.append(issue("RELEASE_REPORT_INVARIANT_ERROR", "release_allowed must be true if and only if overall_status is PASS"))
    if overall == "PASS" and not re.fullmatch(r"[0-9a-f]{40,64}", str(report.get("git_commit") or "")):
        issues.append(issue("RELEASE_REPORT_GIT_COMMIT_INVALID", "PASS release report requires a full lowercase Git commit object ID"))
    try:
        if parse_datetime(report.get("completed_at")) < parse_datetime(report.get("started_at")):
            issues.append(issue("RELEASE_REPORT_TIME_INVALID", "completed_at precedes started_at"))
    except (ValueError, TypeError):
        # The schema validator emits the format detail.
        pass

    requirement_details = report.get("requirements") if isinstance(report.get("requirements"), list) else []
    derived_requirement_counts = {status.lower(): 0 for status in STATUS_VALUES}
    for item in requirement_details:
        status = item.get("status") if isinstance(item, dict) else None
        if status in STATUS_VALUES:
            derived_requirement_counts[status.lower()] += 1
    derived_requirement_counts["total"] = len(requirement_details)
    stored_requirement_counts = report.get("requirement_counts") if isinstance(report.get("requirement_counts"), dict) else {}
    for name, expected in derived_requirement_counts.items():
        if stored_requirement_counts.get(name) != expected:
            issues.append(issue("RELEASE_REPORT_COUNT_MISMATCH", "requirement_counts.%s is not derived from requirement details" % name))

    stored_test_counts = report.get("test_counts") if isinstance(report.get("test_counts"), dict) else {}
    if all(is_integer(stored_test_counts.get(name)) for name in ("total", "pass", "fail", "blocked", "not_run")):
        if sum(stored_test_counts[name] for name in ("pass", "fail", "blocked", "not_run")) != stored_test_counts["total"]:
            issues.append(issue("RELEASE_REPORT_COUNT_MISMATCH", "test_counts statuses do not add up to total"))
    stored_evidence_counts = report.get("evidence_counts") if isinstance(report.get("evidence_counts"), dict) else {}
    if is_integer(stored_evidence_counts.get("verified")) and is_integer(stored_evidence_counts.get("total")) and stored_evidence_counts["verified"] > stored_evidence_counts["total"]:
        issues.append(issue("RELEASE_REPORT_COUNT_MISMATCH", "evidence_counts.verified exceeds total"))
    return issues


def _validate_release_report(repo, report):
    issues = []
    try:
        schema_path = find_project_schema(repo, "release-report.schema.json")
    except HarnessError as exc:
        issues.append(issue("RELEASE_REPORT_SCHEMA_MISSING", str(exc)))
        issues.extend(_release_report_invariant_issues(report))
        return issues
    try:
        schema = strict_json_load(schema_path)
        validator = StrictSchemaValidator(schema, source=str(schema_path))
        issues.extend(issue("RELEASE_REPORT_SCHEMA_ERROR", message) for message in validator.validate(report))
    except HarnessError as exc:
        issues.append(issue("RELEASE_REPORT_SCHEMA_ENGINE_ERROR", str(exc)))
    issues.extend(_release_report_invariant_issues(report))
    return issues


def _validate_runtime_envelopes(documents, git_info):
    issues = []
    current_commit = git_info.get("git_commit") if git_info else None
    run_ids = set()
    for artifact_name in ("test_executions", "evidence", "acceptance_results"):
        document = documents.get(artifact_name)
        if not isinstance(document, dict):
            continue
        run_id = document.get("run_id")
        if isinstance(run_id, str) and run_id:
            run_ids.add(run_id)
        commit = document.get("git_commit")
        if current_commit and (not isinstance(commit, str) or commit.lower() != current_commit):
            issues.append(issue("RUNTIME_ENVELOPE_COMMIT_MISMATCH", "%s top-level git_commit does not equal current HEAD" % artifact_name))
    if len(run_ids) > 1:
        issues.append(issue("RUNTIME_RUN_ID_MISMATCH", "runtime artifacts have different run_id values", details=sorted(run_ids)))
    manifest = documents.get("_collection_manifest")
    if isinstance(manifest, dict):
        manifest_run_id = manifest.get("run_id")
        manifest_commit = manifest.get("git_commit")
        if run_ids and manifest_run_id not in run_ids:
            issues.append(issue("RUNTIME_MANIFEST_RUN_ID_MISMATCH", "collection manifest run_id differs from runtime artifacts"))
        if current_commit and manifest_commit != current_commit:
            issues.append(issue("RUNTIME_MANIFEST_COMMIT_MISMATCH", "collection manifest git_commit does not equal current HEAD"))
        for artifact_name in ("test_executions", "evidence", "acceptance_results"):
            document = documents.get(artifact_name)
            if isinstance(document, dict) and (
                document.get("run_id") != manifest_run_id
                or document.get("git_commit") != manifest_commit
            ):
                issues.append(issue(
                    "RUNTIME_MANIFEST_ENVELOPE_MISMATCH",
                    "%s envelope differs from the collection manifest" % artifact_name,
                ))
    return issues


def _validate_collected_trace_overlay(repo, runtime_document, requirement_outcomes):
    state_path = Path(repo) / ".harness/state/traceability.json"
    if not state_path.exists() and not state_path.is_symlink():
        return []
    try:
        static_path = _safe_repo_path(repo, ".harness/spec/traceability.json", must_exist=True)
        static_document = strict_json_load(static_path)
        static_items = extract_array(static_document, "traceability")
        expected_items = []
        for trace in sorted(
            (item for item in static_items if isinstance(item, dict)),
            key=lambda item: str(item.get("trace_id")),
        ):
            expected = json.loads(json.dumps(trace))
            requirement_id = expected.get("requirement_id")
            expected["final_verdict"] = {
                "state": "LINKED",
                "acceptance_result_requirement_id": requirement_id,
                "status": requirement_outcomes.get(requirement_id, "NOT_RUN"),
                "reason": None,
            }
            expected_items.append(expected)
        expected_document = {"standard_version": STANDARD_VERSION, "traceability": expected_items}
        if not json_equal(runtime_document, expected_document):
            return [issue(
                "COLLECTED_TRACE_MISMATCH",
                "runtime traceability is not the deterministic static-trace plus derived-verdict overlay",
            )]
    except (HarnessError, OSError) as exc:
        return [issue("COLLECTED_TRACE_INVALID", str(exc))]
    return []


def _capture_verification_inputs(repo, indexes, require_git=True):
    relative_paths = {
        ".harness/config.json",
        ".harness/state/requirements-baseline.json",
    }
    for artifact_name, filename, schema_name, _root_key in ARTIFACTS:
        collected_name = COLLECTED_RUNTIME_ARTIFACTS.get(artifact_name)
        collected_relative = ".harness/state/%s" % collected_name if collected_name else None
        if collected_relative and (Path(repo) / collected_relative).is_file():
            relative_paths.add(collected_relative)
        else:
            relative_paths.add(".harness/spec/%s" % filename)
        relative_paths.add(".harness/schemas/%s" % schema_name)
    relative_paths.add(".harness/schemas/release-report.schema.json")
    relative_paths.add(".harness/spec/traceability.json")
    if (Path(repo) / ".harness/state/collection-manifest.json").is_file():
        relative_paths.add(".harness/state/collection-manifest.json")
    for script_name in CANONICAL_SCRIPT_NAMES:
        relative_paths.add("scripts/%s" % script_name)
    for evidence_item in indexes.get("evidence", {}).values():
        path_value = evidence_item.get("path") if isinstance(evidence_item, dict) else None
        if isinstance(path_value, str) and path_value:
            relative_paths.add(Path(path_value).as_posix())
    snapshot = {}
    issues = []
    for relative in sorted(relative_paths):
        try:
            path = _safe_repo_path(repo, relative, must_exist=True)
            digest, size = _hash_regular_file(path, 1024 * 1024 * 1024)
            snapshot[relative] = {"sha256": digest, "bytes": size}
            immutable = (
                relative == ".harness/config.json"
                or relative == ".harness/state/requirements-baseline.json"
                or relative.startswith("scripts/")
                or relative.startswith(".harness/schemas/")
                or (
                    relative.startswith(".harness/spec/")
                    and relative not in RUNTIME_MUTABLE_PATHS
                )
            )
            if immutable and require_git:
                _assert_git_head_file(repo, relative, 1024 * 1024 * 1024)
        except (HarnessError, OSError, subprocess.SubprocessError) as exc:
            issues.append(issue(
                "VERIFICATION_INPUT_UNAVAILABLE",
                "cannot snapshot verification input %s: %s" % (relative, exc),
                path=relative,
            ))
    return snapshot, issues


def _write_run_archive(repo, run_id, verification, report):
    run_id = validate_run_id(run_id)
    _safe_repo_directory(repo, ".harness", create=False)
    runs_dir = _safe_repo_directory(repo, ".harness/runs", create=True)
    state_dir = _safe_repo_directory(repo, ".harness/state", create=True)
    run_dir = runs_dir / run_id
    if run_dir.exists() or run_dir.is_symlink():
        raise HarnessError("run archive target already exists: %s" % run_dir)
    run_dir.mkdir(mode=0o755)
    verification_path = run_dir / "verification-result.json"
    report_path = run_dir / "release-report.json"
    atomic_write_json(verification_path, verification)
    atomic_write_json(report_path, report)
    manifest = {
        "standard_version": STANDARD_VERSION,
        "run_id": run_id,
        "artifacts": [],
    }
    for artifact_path in (verification_path, report_path):
        digest, size = _hash_regular_file(artifact_path, 128 * 1024 * 1024)
        manifest["artifacts"].append({
            "path": artifact_path.relative_to(Path(repo)).as_posix(),
            "sha256": digest,
            "bytes": size,
        })
    atomic_write_json(run_dir / "artifacts-manifest.json", manifest)
    atomic_write_json(state_dir / "latest-run.json", {
        "standard_version": STANDARD_VERSION,
        "run_id": run_id,
        "overall_status": report["overall_status"],
        "release_allowed": report["release_allowed"],
        "report_path": report_path.relative_to(Path(repo)).as_posix(),
    })
    return run_dir


def validate_repository(repo, include_report=False):
    started_at = utc_now()
    try:
        repo = find_repo(repo)
    except BaseException as exc:
        fallback = Path(repo or os.getcwd()).expanduser().resolve(strict=False)
        return {
            "standard_version": STANDARD_VERSION, "kind": "validation_result",
            "repo": str(fallback), "started_at": started_at, "completed_at": utc_now(),
            "overall_status": "FAIL", "valid": False,
            "counts": {"requirements": 0, "test_cases": 0, "test_executions": 0, "evidence": 0, "traces": 0, "acceptance_results": 0, "issues": 1},
            "issues": [issue("REPOSITORY_LAYOUT_UNSAFE", "%s: %s" % (type(exc).__name__, exc))],
        }
    try:
        raw_config = load_config(repo)
    except HarnessError as exc:
        raw_config = {}
        config_issues = [issue("CONFIG_MISSING" if "is missing" in str(exc) else "CONFIG_READ_ERROR", str(exc))]
    else:
        config_issues = []
    config, hardening_issues = harden_config(raw_config)
    config_issues.extend(hardening_issues)
    loaded = load_and_validate_artifacts(repo, include_report=include_report)
    semantic_issues, indexes = validate_semantics(repo, loaded["arrays"], config)
    baseline_issues = validate_requirement_baseline(repo, indexes, config, strict=False)
    issues = config_issues + loaded["issues"] + semantic_issues + baseline_issues
    if not indexes["requirements"] or not indexes["tests"]:
        issues.append(issue("SPEC_NOT_READY", "requirements and test cases must both be non-empty", status="NOT_RUN"))
    overall = worst_status(item["status"] for item in issues)
    return {
        "standard_version": STANDARD_VERSION,
        "kind": "validation_result",
        "repo": str(repo),
        "started_at": started_at,
        "completed_at": utc_now(),
        "overall_status": overall,
        "valid": overall == "PASS",
        "counts": {
            "requirements": len(indexes["requirements"]),
            "test_cases": len(indexes["tests"]),
            "test_executions": len(indexes["executions"]),
            "evidence": len(indexes["evidence"]),
            "traces": len(indexes["traces"]),
            "acceptance_results": len(indexes["acceptance"]),
            "issues": len(issues),
        },
        "issues": issues,
    }


def _verify_repository_impl(
    repo, strict=False, run_id=None, initial_issues=None, collector_result=None,
):
    repo = find_repo(repo)
    started_at = utc_now()
    run_id = run_id or new_run_id()
    commands = []
    if isinstance(collector_result, dict):
        collector_commands = collector_result.get("commands")
        if isinstance(collector_commands, list):
            commands.extend(item for item in collector_commands if isinstance(item, dict))
    issues = list(initial_issues or [])
    try:
        raw_config = load_config(repo)
    except HarnessError as exc:
        raw_config = {}
        issues.append(issue("CONFIG_MISSING" if "is missing" in str(exc) else "CONFIG_READ_ERROR", str(exc)))
    config, hardening_issues = harden_config(raw_config)
    issues.extend(hardening_issues)
    strict = bool(strict or config.get("strict"))
    if strict:
        issues.extend(_canonical_distribution_issues(repo))
    loaded = load_and_validate_artifacts(repo)
    issues.extend(loaded["issues"])
    semantic_issues, indexes = validate_semantics(repo, loaded["arrays"], config)
    issues.extend(semantic_issues)
    issues.extend(_robot_orchestration_issues(repo, config, indexes))
    issues.extend(validate_requirement_baseline(repo, indexes, config, strict=strict))
    git_info, git_issues, git_commands = git_snapshot(repo)
    issues.extend(git_issues)
    commands.extend(git_commands)
    issues.extend(_validate_runtime_envelopes(loaded["documents"], git_info))
    input_snapshot, snapshot_issues = _capture_verification_inputs(
        repo, indexes, require_git=git_info is not None
    )
    issues.extend(snapshot_issues)
    evidence_document = loaded["documents"].get("evidence")
    evidence_run_id = evidence_document.get("run_id") if isinstance(evidence_document, dict) else None
    allow_fresh_execution = bool(
        git_info and not git_info.get("dirty_paths")
        and not any(item.get("status") in {"FAIL", "BLOCKED", "NOT_RUN"} for item in issues)
    )
    evidence_issues, evidence_validity = verify_evidence(
        repo, indexes, git_info, config, evidence_run_id,
        allow_fresh_execution=allow_fresh_execution,
    )
    issues.extend(evidence_issues)
    check_issues, check_commands = _run_applicable_checks(repo, config, indexes, evidence_validity)
    issues.extend(check_issues)
    commands.extend(check_commands)
    ending_input_snapshot, ending_snapshot_issues = _capture_verification_inputs(
        repo, indexes, require_git=git_info is not None
    )
    issues.extend(ending_snapshot_issues)
    if ending_input_snapshot != input_snapshot:
        changed_paths = sorted(
            path for path in set(input_snapshot) | set(ending_input_snapshot)
            if input_snapshot.get(path) != ending_input_snapshot.get(path)
        )
        issues.append(issue(
            "VERIFICATION_INPUT_CHANGED",
            "verification inputs changed during fresh test execution: %s" % ", ".join(changed_paths),
            details={"paths": changed_paths},
        ))
    outcome_issues, execution_outcomes, test_outcomes, requirement_outcomes = derive_outcomes(
        indexes,
        evidence_validity,
        git_info.get("git_commit") if git_info else None,
        strict=strict,
    )
    issues.extend(outcome_issues)
    issues.extend(_validate_collected_trace_overlay(
        repo, loaded["documents"].get("traceability"), requirement_outcomes,
    ))
    scan_issues, findings = scan_sources(repo, indexes, config, strict=strict)
    issues.extend(scan_issues)

    # Detect a concurrent commit or newly dirty release-relevant source after evidence checking.
    ending_git, ending_issues, ending_commands = git_snapshot(repo)
    commands.extend(ending_commands)
    issues.extend(ending_issues)
    if git_info and ending_git and git_info.get("git_commit") != ending_git.get("git_commit"):
        issues.append(issue("GIT_CHANGED_DURING_VERIFY", "HEAD changed during verification"))
    if not indexes["requirements"] or not indexes["tests"]:
        issues.append(issue("SPEC_NOT_READY", "requirements and test cases must both be non-empty", status="NOT_RUN"))
    completed_at = utc_now()
    report = build_report(
        repo, run_id, started_at, completed_at, ending_git or git_info, config, issues,
        indexes, evidence_validity, test_outcomes, requirement_outcomes, findings, commands,
    )
    report_schema_issues = _validate_release_report(repo, report)
    if report_schema_issues:
        issues.extend(report_schema_issues)
        completed_at = utc_now()
        report = build_report(
            repo, run_id, started_at, completed_at, ending_git or git_info, config, issues,
            indexes, evidence_validity, test_outcomes, requirement_outcomes, findings, commands,
        )
        remaining_report_issues = _validate_release_report(repo, report)
        if remaining_report_issues:
            issues.extend(remaining_report_issues)
            report = build_fail_closed_report(
                repo,
                "RELEASE_REPORT_UNRECOVERABLE",
                "release report remained invalid after fail-closed rebuild: %s" % "; ".join(
                    item.get("message", "unknown report error") for item in remaining_report_issues
                ),
                run_id=run_id,
            )
    verification = {
        "standard_version": STANDARD_VERSION,
        "kind": "verification_result",
        "repo": str(repo),
        "run_id": run_id,
        "strict": bool(strict),
        "started_at": started_at,
        "completed_at": completed_at,
        "overall_status": report["overall_status"],
        "release_allowed": report["release_allowed"],
        "git": ending_git or git_info,
        "issues": issues,
        "execution_outcomes": execution_outcomes,
        "test_outcomes": test_outcomes,
        "requirement_outcomes": requirement_outcomes,
        "evidence_validity": evidence_validity,
        "heuristic_findings": findings,
        "command_receipts": commands,
        "collector_result": collector_result,
        "validated_collection_run_id": (
            loaded["documents"].get("_collection_manifest", {}).get("run_id")
            if isinstance(loaded["documents"].get("_collection_manifest"), dict)
            else None
        ),
        "report": report,
    }
    run_dir = _write_run_archive(repo, run_id, verification, report)
    verification["run_dir"] = str(run_dir)
    return verification


def safe_verify(repo, strict=False, run_id=None, verifier=None):
    runner = verifier or _verify_repository_impl
    started_at = utc_now()
    selected_run_id = new_run_id()
    run_id_invalid = False
    resolution_error = None
    try:
        resolved_repo = find_repo(repo)
    except BaseException as exc:
        resolution_error = exc
        try:
            resolved_repo = Path(repo or os.getcwd()).expanduser().resolve(strict=False)
        except BaseException:
            resolved_repo = Path(os.getcwd()).resolve(strict=False)
    try:
        if resolution_error is not None:
            raise resolution_error
        if run_id is not None:
            try:
                selected_run_id = validate_run_id(run_id)
            except HarnessError:
                run_id_invalid = True
                raise
        return runner(resolved_repo, strict=strict, run_id=selected_run_id)
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            error_name = "KeyboardInterrupt"
        else:
            error_name = type(exc).__name__
        failure_issue = issue(
            "RUN_ID_INVALID" if run_id_invalid else "VERIFIER_INTERNAL_ERROR",
            "%s: %s" % (error_name, exc),
            details={"traceback": traceback.format_exc(limit=20)},
        )
        completed_at = utc_now()
        indexes = {name: {} for name in ("requirements", "changes", "tests", "executions", "evidence", "traces", "acceptance")}
        report = build_report(
            resolved_repo, selected_run_id, started_at, completed_at, None, {}, [failure_issue],
            indexes, {}, {}, {}, [], [],
        )
        verification = {
            "standard_version": STANDARD_VERSION,
            "kind": "verification_result",
            "repo": str(resolved_repo),
            "run_id": selected_run_id,
            "strict": bool(strict),
            "started_at": started_at,
            "completed_at": completed_at,
            "overall_status": "FAIL",
            "release_allowed": False,
            "git": None,
            "issues": [failure_issue],
            "execution_outcomes": {},
            "test_outcomes": {},
            "requirement_outcomes": {},
            "evidence_validity": {},
            "heuristic_findings": [],
            "report": report,
        }
        try:
            if resolution_error is not None:
                raise HarnessError("unsafe repository layout prevents fail-closed archive writes")
            run_dir = _write_run_archive(resolved_repo, selected_run_id, verification, report)
            verification["run_dir"] = str(run_dir)
        except BaseException as archive_exc:
            archive_issue = issue("RUN_ARCHIVE_ERROR", "%s: %s" % (type(archive_exc).__name__, archive_exc))
            verification["issues"].append(archive_issue)
            completed_at = utc_now()
            report = build_report(
                resolved_repo, selected_run_id, started_at, completed_at, None, {},
                verification["issues"], indexes, {}, {}, {}, [], [],
            )
            verification.update({
                "completed_at": completed_at,
                "overall_status": report["overall_status"],
                "release_allowed": report["release_allowed"],
                "report": report,
            })
        return verification


def exit_code_for_status(status):
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 2, "NOT_RUN": 3}.get(status, 4)


def load_latest_report(repo, run_id=None):
    repo = find_repo(repo)
    if run_id is None:
        state_dir = _safe_repo_directory(repo, ".harness/state", create=False)
        latest_path = _safe_repo_path(repo, (state_dir / "latest-run.json").relative_to(repo).as_posix(), must_exist=True)
        latest = strict_json_load(latest_path)
        run_id = latest.get("run_id") if isinstance(latest, dict) else None
    if run_id is None:
        raise HarnessError("no run_id is available")
    run_id = validate_run_id(run_id)
    run_dir = _safe_repo_path(repo, ".harness/runs/%s" % run_id, must_exist=True)
    if not run_dir.is_dir():
        raise HarnessError("archived run path is not a directory")
    report_path = run_dir / "release-report.json"
    verification_path = run_dir / "verification-result.json"
    manifest_path = run_dir / "artifacts-manifest.json"
    manifest = strict_json_load(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("run_id") != run_id or not isinstance(manifest.get("artifacts"), list):
        raise HarnessError("run artifact manifest is malformed or belongs to a different run")
    manifest_entries = {}
    for item in manifest["artifacts"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("sha256"), str):
            raise HarnessError("run artifact manifest contains a malformed entry")
        if item["path"] in manifest_entries:
            raise HarnessError("run artifact manifest contains a duplicate path")
        manifest_entries[item["path"]] = item
    for artifact_path in (report_path, verification_path):
        relative = artifact_path.relative_to(repo).as_posix()
        entry = manifest_entries.get(relative)
        if entry is None:
            raise HarnessError("run artifact manifest does not cover %s" % relative)
        resolved = _safe_repo_path(repo, relative, must_exist=True)
        digest, size = _hash_regular_file(resolved, 128 * 1024 * 1024)
        if digest != entry.get("sha256") or size != entry.get("bytes"):
            raise HarnessError("run artifact manifest hash/size mismatch for %s" % relative)
    report = strict_json_load(report_path)
    verification = strict_json_load(verification_path)
    if not isinstance(verification, dict) or verification.get("run_id") != run_id:
        raise HarnessError("archived verification is malformed or belongs to a different run")
    if not json_equal(verification.get("report"), report):
        raise HarnessError("archived release report differs from the verification's embedded report")
    if verification.get("overall_status") != report.get("overall_status") or verification.get("release_allowed") != report.get("release_allowed"):
        raise HarnessError("archived verification and release report verdicts disagree")
    schema_issues = _validate_release_report(repo, report)
    if schema_issues:
        raise HarnessError("stored release report is invalid: %s" % "; ".join(item["message"] for item in schema_issues))
    evidence_validity = verification.get("evidence_validity")
    if not isinstance(evidence_validity, dict):
        raise HarnessError("archived verification lacks evidence_validity")
    expected_artifacts = {}
    for evidence_id, validity in evidence_validity.items():
        if not isinstance(validity, dict) or not validity.get("valid"):
            continue
        artifact_key = "evidence:%s" % evidence_id
        path_value = validity.get("path")
        digest_value = validity.get("sha256")
        if not isinstance(path_value, str) or not isinstance(digest_value, str):
            raise HarnessError("valid archived evidence lacks path/hash: %s" % evidence_id)
        expected_artifacts[artifact_key] = (path_value, digest_value)
    observed_artifacts = {}
    for artifact in report.get("artifacts", []) if isinstance(report.get("artifacts"), list) else []:
        if not isinstance(artifact, dict):
            raise HarnessError("archived release report contains a malformed artifact")
        artifact_type = artifact.get("artifact_type")
        if not isinstance(artifact_type, str) or artifact_type in observed_artifacts:
            raise HarnessError("archived release report contains duplicate/malformed artifact types")
        observed_artifacts[artifact_type] = (artifact.get("path"), artifact.get("sha256"))
    if observed_artifacts != expected_artifacts:
        raise HarnessError("archived report artifacts do not exactly match verified evidence")
    for artifact_type, (path_value, expected_digest) in sorted(expected_artifacts.items()):
        evidence_path = _safe_repo_path(repo, path_value, must_exist=True)
        actual_digest, _size = _hash_regular_file(evidence_path, 1024 * 1024 * 1024)
        if actual_digest != expected_digest:
            raise HarnessError("archived evidence artifact hash mismatch for %s" % artifact_type)
    derived_status = worst_status(
        [item.get("status") for item in verification.get("issues", []) if isinstance(item, dict)]
        + list((verification.get("test_outcomes") or {}).values())
        + list((verification.get("requirement_outcomes") or {}).values())
    )
    if derived_status != report.get("overall_status"):
        raise HarnessError("archived verdict is not derivable from verification details")
    derived_tests = _count_statuses(verification.get("test_outcomes") or {})
    stored_tests = report.get("test_counts") if isinstance(report.get("test_counts"), dict) else {}
    for name in ("total", "pass", "fail", "blocked", "not_run"):
        if stored_tests.get(name) != derived_tests.get(name):
            raise HarnessError("archived test_counts.%s is not derivable from verification details" % name)
    derived_requirements = _count_statuses(verification.get("requirement_outcomes") or {})
    stored_requirements = report.get("requirement_counts") if isinstance(report.get("requirement_counts"), dict) else {}
    for name in ("total", "pass", "fail", "blocked", "not_run"):
        if stored_requirements.get(name) != derived_requirements.get(name):
            raise HarnessError("archived requirement_counts.%s is not derivable from verification details" % name)
    git_info, git_issues, _commands = git_snapshot(repo)
    if git_issues:
        raise HarnessError("current repository cannot reuse an archived release report: %s" % "; ".join(item["message"] for item in git_issues))
    if not git_info or git_info.get("git_commit") != report.get("git_commit"):
        raise HarnessError("archived release report does not match current Git HEAD")
    return report, report_path


def _legacy_read_hook_input(stream=None):
    stream = stream or sys.stdin
    try:
        if stream.isatty():
            return None
    except (AttributeError, OSError):
        pass
    try:
        raw = stream.read(2 * 1024 * 1024 + 1)
    except (OSError, ValueError):
        return None
    if not raw or not raw.strip():
        return None
    if len(raw.encode("utf-8")) > 2 * 1024 * 1024:
        return {"_normalized_event": "stop", "_hook_input_error": "hook input exceeds 2 MiB"}
    try:
        value = strict_json_loads(raw, "hook stdin")
    except HarnessError as exc:
        return {"_normalized_event": "stop", "_hook_input_error": str(exc)}
    if not isinstance(value, dict):
        return {"_normalized_event": "stop", "_hook_input_error": "hook input must be a JSON object"}
    event = value.get("hook_event_name") or value.get("hookEventName") or value.get("eventName") or value.get("event_name")
    if not isinstance(event, str):
        value["_normalized_event"] = "stop"
        value["_hook_input_error"] = "hook event name is missing"
        return value
    normalized = event.replace("_", "").replace("-", "").lower()
    if normalized not in ("stop", "subagentstop"):
        value["_normalized_event"] = "stop"
        value["_hook_input_error"] = "unsupported hook event: %s" % event
        return value
    value["_normalized_event"] = normalized
    return value


def _legacy_subagent_summary_errors(message, expected_status, repo=None):
    required = ("TASK_STATUS", "FILES", "EVIDENCE", "FAILURES", "BLOCKERS", "NOT_RUN")
    values = {}
    errors = []
    text = message if isinstance(message, str) else ""
    for name in required:
        match = re.search(r"(?m)^%s\s*:\s*(.*?)\s*$" % name, text)
        if match is None or not match.group(1).strip():
            errors.append("missing or empty %s field" % name)
        else:
            values[name] = match.group(1).strip()
    claimed = values.get("TASK_STATUS")
    if claimed is not None and claimed not in STATUS_VALUES:
        errors.append("TASK_STATUS must be one of %s" % ", ".join(STATUS_VALUES))
    elif claimed is not None and claimed != expected_status:
        errors.append("TASK_STATUS %s does not match computed status %s" % (claimed, expected_status))

    def empty_marker(value):
        return str(value or "").strip().lower() in {"none", "n/a", "na", "[]", "-", "null"}

    if expected_status in ("PASS", "FAIL") and "EVIDENCE" in values and empty_marker(values["EVIDENCE"]):
        errors.append("EVIDENCE must name machine evidence for %s" % expected_status)
    if expected_status == "PASS" and "FILES" in values and empty_marker(values["FILES"]):
        errors.append("FILES must name reviewed or changed project files for PASS")
    if expected_status == "PASS":
        for name in ("FAILURES", "BLOCKERS", "NOT_RUN"):
            if name in values and not empty_marker(values[name]):
                errors.append("%s must explicitly be NONE for PASS" % name)
    elif expected_status == "FAIL" and "FAILURES" in values and empty_marker(values["FAILURES"]):
        errors.append("FAILURES must describe the executed failure")
    elif expected_status == "BLOCKED" and "BLOCKERS" in values and empty_marker(values["BLOCKERS"]):
        errors.append("BLOCKERS must describe the blocking condition")
    elif expected_status == "NOT_RUN" and "NOT_RUN" in values and empty_marker(values["NOT_RUN"]):
        errors.append("NOT_RUN must describe unexecuted scope")
    if repo:
        for field_name in ("FILES", "EVIDENCE"):
            value = values.get(field_name)
            if value is None or empty_marker(value):
                continue
            paths = [item.strip() for item in re.split(r"\s*[,;]\s*", value) if item.strip()]
            for path_value in paths:
                try:
                    path = _safe_repo_path(repo, path_value, must_exist=True)
                    if not path.is_file():
                        raise HarnessError("path is not a regular file")
                    normalized = Path(path_value).as_posix()
                    if field_name == "EVIDENCE" and not normalized.startswith(".harness/runs/"):
                        raise HarnessError("evidence path is outside .harness/runs")
                except (HarnessError, OSError) as exc:
                    errors.append("%s path %s is unsafe or unavailable: %s" % (field_name, path_value, exc))
    return errors


def _legacy_hook_continuation_exhausted(verification, detail=None):
    location = verification.get("run_dir") or verification.get("run_id") or "the archived Harness run"
    return {
        "continue": False,
        "stopReason": detail or "Software Delivery Harness continuation limit reached; inspect %s." % location,
        "systemMessage": "Do not claim delivery completion. Report the archived machine status and remaining failures, blockers, or not-run scope.",
    }, 0


def _legacy_hook_fingerprint(verification):
    normalized_issues = []
    for item in verification.get("issues", []) if isinstance(verification.get("issues"), list) else []:
        if isinstance(item, dict):
            normalized_issues.append({
                "code": item.get("code"), "status": item.get("status"),
                "message": item.get("message"),
                "requirement_ids": sorted(_list_of_strings(item.get("requirement_ids"))),
                "test_case_ids": sorted(_list_of_strings(item.get("test_case_ids"))),
            })
    normalized_issues.sort(key=lambda item: canonical_json_bytes(item))
    payload = {
        "overall_status": verification.get("overall_status"),
        "release_allowed": verification.get("release_allowed"),
        "issues": normalized_issues,
        "execution_outcomes": verification.get("execution_outcomes") or {},
        "test_outcomes": verification.get("test_outcomes") or {},
        "requirement_outcomes": verification.get("requirement_outcomes") or {},
    }
    return canonical_sha256(payload)


def _legacy_hook_state_transition(hook_input, verification, active):
    repo_value = verification.get("repo") or hook_input.get("cwd")
    if not isinstance(repo_value, str) or not repo_value:
        raise HarnessError("Hook state requires a repository path")
    repo = find_repo(repo_value)
    state_dir = _safe_repo_directory(repo, ".harness/state", create=True)
    state_path = _safe_repo_path(repo, (state_dir / "hook-state.json").relative_to(repo).as_posix(), must_exist=False)
    state = {"standard_version": STANDARD_VERSION, "entries": {}}
    if state_path.exists():
        loaded = strict_json_load(state_path)
        if not isinstance(loaded, dict) or not isinstance(loaded.get("entries"), dict):
            raise HarnessError("Hook state file is malformed")
        state = loaded
    session = hook_input.get("session_id") or hook_input.get("sessionId") or "unknown-session"
    event = hook_input.get("_normalized_event") or "stop"
    key = hashlib.sha256((str(session) + "\x00" + event).encode("utf-8")).hexdigest()
    fingerprint = _hook_fingerprint(verification)
    previous = state["entries"].get(key) if isinstance(state["entries"].get(key), dict) else {}
    previous_fingerprint = previous.get("fingerprint")
    previous_count = previous.get("continuations") if is_integer(previous.get("continuations")) else 0
    if not active:
        count = 1
        action = "continue"
    elif previous_fingerprint == fingerprint and previous_count >= 1:
        count = previous_count
        action = "unchanged"
    elif previous_count >= 3:
        count = previous_count
        action = "exhausted"
    else:
        count = previous_count + 1
        action = "continue"
    state["entries"][key] = {
        "event": event,
        "fingerprint": fingerprint,
        "continuations": count,
        "updated_at": utc_now(),
    }
    atomic_write_json(state_path, state)
    return action, count


def _legacy_hook_response(hook_input, verification):
    status = verification.get("overall_status", "FAIL")
    event = hook_input.get("_normalized_event")
    active = bool(hook_input.get("stop_hook_active") or hook_input.get("stopHookActive"))
    input_error = hook_input.get("_hook_input_error")
    if input_error:
        if active:
            return _hook_continuation_exhausted(
                verification,
                "Invalid Hook input persisted during the continuation: %s" % input_error,
            )
        return {"decision": "block", "reason": "Invalid Software Delivery Harness Hook input: %s" % input_error}, 0
    reason = "Software Delivery Harness is %s; release_allowed=false. See %s" % (
        status,
        verification.get("run_dir") or verification.get("run_id"),
    )
    if event == "subagentstop":
        last_message = hook_input.get("last_assistant_message") or hook_input.get("lastAssistantMessage") or ""
        summary_errors = _subagent_summary_errors(last_message, status, verification.get("repo"))
        if summary_errors:
            if active:
                return _hook_continuation_exhausted(
                    verification,
                    "SubagentStop continuation still lacks a valid six-field summary: %s" % "; ".join(summary_errors),
                )
            return {
                "decision": "block",
                "reason": "SubagentStop summary protocol failed: %s. Return TASK_STATUS/FILES/EVIDENCE/FAILURES/BLOCKERS/NOT_RUN." % "; ".join(summary_errors),
            }, 0
        if status == "PASS":
            return {"systemMessage": "Harness verified the listed paths and evidence integrity; it cannot prove that every file modification was within the subagent's authorization boundary."}, 0
    if status == "PASS":
        return {}, 0
    try:
        action, count = _hook_state_transition(hook_input, verification, active)
    except (HarnessError, OSError) as exc:
        return _hook_continuation_exhausted(
            verification,
            "Harness could not persist bounded Hook continuation state: %s" % exc,
        )
    if action == "unchanged":
        return _hook_continuation_exhausted(
            verification,
            "Harness failures are unchanged after continuation; stop retrying and report the truthful %s result." % status,
        )
    if action == "exhausted":
        return _hook_continuation_exhausted(
            verification,
            "Harness failure changes exceeded the bounded continuation limit; report the truthful %s result." % status,
        )
    suffix = " Continue attempt %d of 3 to repair or produce a truthful failure report." % count
    if event == "subagentstop":
        suffix += " The six-field handoff was structurally valid, but the machine result still prevents PASS."
    return {"decision": "block", "reason": reason + "." + suffix}, 0

# Production Codex 0.148.0 Hook wire and subagent authorization guard.
_HOOK_EVENT_NAMES = {
    "pretooluse": "PreToolUse",
    "stop": "Stop",
    "subagentstart": "SubagentStart",
    "subagentstop": "SubagentStop",
}
_AGENT_TOOL_NAMES = frozenset({
    "Agent",
    "spawn_agent",
    # Codex 0.148.0 flattens the namespaced collaboration tool on the Hook wire.
    "collaborationspawn_agent",
    # Keep the transcript-qualified spelling fail-closed if Codex exposes it directly.
    "collaboration.spawn_agent",
})
_HOOK_PERMISSION_MODES = {"default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"}
_HOOK_WIRE_SCHEMAS = {
    "pretooluse": {
        "cwd": str,
        "hook_event_name": str,
        "model": str,
        "permission_mode": str,
        "session_id": str,
        "tool_input": dict,
        "tool_name": str,
        "tool_use_id": str,
        "transcript_path": (str, type(None)),
        "turn_id": str,
    },
    "stop": {
        "cwd": str,
        "hook_event_name": str,
        "last_assistant_message": (str, type(None)),
        "model": str,
        "permission_mode": str,
        "session_id": str,
        "stop_hook_active": bool,
        "transcript_path": (str, type(None)),
        "turn_id": str,
    },
    "subagentstart": {
        "agent_id": str,
        "agent_type": str,
        "cwd": str,
        "hook_event_name": str,
        "model": str,
        "permission_mode": str,
        "session_id": str,
        "transcript_path": (str, type(None)),
        "turn_id": str,
    },
    "subagentstop": {
        "agent_id": str,
        "agent_transcript_path": (str, type(None)),
        "agent_type": str,
        "cwd": str,
        "hook_event_name": str,
        "last_assistant_message": (str, type(None)),
        "model": str,
        "permission_mode": str,
        "session_id": str,
        "stop_hook_active": bool,
        "transcript_path": (str, type(None)),
        "turn_id": str,
    },
}
_HOOK_INTERNAL_FIELDS = {"_normalized_event", "_hook_input_error"}
_SUBAGENT_GUARD_ROOT = ".harness/state/subagent-guard"
_SUBAGENT_PROTECTED_DOCUMENTS = {
    ".harness/config.json": None,
    ".harness/spec/requirements.json": "requirements",
    ".harness/spec/change-ledger.json": "change_ledger",
    ".harness/spec/test-cases.json": "test_cases",
    ".harness/spec/traceability.json": "traceability",
    ".harness/spec/acceptance-results.json": "acceptance_results",
    ".harness/state/requirements-baseline.json": "requirements",
}
_SUBAGENT_GRANTABLE_DOCUMENTS = {
    ".harness/spec/requirements.json",
    ".harness/spec/test-cases.json",
    ".harness/spec/traceability.json",
    ".harness/spec/acceptance-results.json",
}


def _normalize_hook_event(value):
    if not isinstance(value, str):
        return None
    normalized = value.replace("_", "").replace("-", "").lower()
    return normalized if normalized in _HOOK_EVENT_NAMES else None


def _hook_input_error(event, message, seed=None):
    value = dict(seed) if isinstance(seed, dict) else {}
    value["_normalized_event"] = event or "stop"
    value["_hook_input_error"] = str(message)
    return value


def read_hook_input(stream=None, expected_event=None):
    """Read at most 2 MiB and preserve an expected event for valid error output."""
    expected = _normalize_hook_event(expected_event)
    if expected_event is not None and expected is None:
        raise HarnessError("unsupported expected Hook event: %s" % expected_event)
    stream = stream or sys.stdin
    try:
        if stream.isatty():
            return _hook_input_error(expected, "hook stdin is a terminal") if expected else None
    except (AttributeError, OSError):
        pass
    try:
        raw = stream.read(2 * 1024 * 1024 + 1)
    except (OSError, ValueError) as exc:
        return _hook_input_error(expected, "cannot read hook stdin: %s" % exc) if expected else None
    if not raw or not raw.strip():
        return _hook_input_error(expected, "hook stdin is empty") if expected else None
    if len(raw.encode("utf-8")) > 2 * 1024 * 1024:
        return _hook_input_error(expected, "hook input exceeds 2 MiB")
    try:
        value = strict_json_loads(raw, "hook stdin")
    except HarnessError as exc:
        return _hook_input_error(expected, str(exc))
    if not isinstance(value, dict):
        return _hook_input_error(expected, "hook input must be a JSON object")
    actual = _normalize_hook_event(value.get("hook_event_name"))
    if actual is None:
        return _hook_input_error(expected, "hook event name is missing or unsupported", value)
    if expected is not None and actual != expected:
        return _hook_input_error(
            expected,
            "hook event %s does not match configured event %s"
            % (value.get("hook_event_name"), _HOOK_EVENT_NAMES[expected]),
            value,
        )
    value["_normalized_event"] = actual
    return value


def _is_agent_tool_name(value):
    """Return true only for exact Codex Agent tool wire names and aliases."""
    return isinstance(value, str) and value in _AGENT_TOOL_NAMES


def _hook_wire_errors(value, event):
    schema = _HOOK_WIRE_SCHEMAS.get(event)
    if schema is None:
        return ["unsupported Hook event"]
    errors = []
    public_keys = set(value) - _HOOK_INTERNAL_FIELDS
    missing = sorted(set(schema) - public_keys)
    extra = sorted(public_keys - set(schema))
    if missing:
        errors.append("missing required Hook fields: %s" % ", ".join(missing))
    if extra:
        errors.append("unsupported Hook fields: %s" % ", ".join(extra))
    for name, wanted in schema.items():
        if name in value and not isinstance(value[name], wanted):
            errors.append("Hook field %s has the wrong type" % name)
    if value.get("hook_event_name") != _HOOK_EVENT_NAMES[event]:
        errors.append("hook_event_name must be %s" % _HOOK_EVENT_NAMES[event])
    for name in ("cwd", "model", "session_id", "turn_id"):
        if name in value and isinstance(value[name], str) and not value[name].strip():
            errors.append("Hook field %s must not be empty" % name)
    if event == "pretooluse":
        for name in ("tool_name", "tool_use_id"):
            if name in value and isinstance(value[name], str) and not value[name].strip():
                errors.append("Hook field %s must not be empty" % name)
        tool_input = value.get("tool_input")
        if _is_agent_tool_name(value.get("tool_name")) and isinstance(tool_input, dict):
            agent_type = tool_input.get("agent_type")
            if not isinstance(agent_type, str) or not agent_type.strip():
                errors.append("Agent tool_input.agent_type must be a non-empty string")
    if event in {"subagentstart", "subagentstop"}:
        for name in ("agent_id", "agent_type"):
            if name in value and isinstance(value[name], str) and not value[name].strip():
                errors.append("Hook field %s must not be empty" % name)
    if value.get("permission_mode") not in _HOOK_PERMISSION_MODES:
        errors.append("Hook permission_mode is unsupported")
    return errors


def _parse_hook_utc(value, field):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise HarnessError("%s must be a UTC RFC 3339 timestamp ending in Z" % field)
    try:
        parsed = _dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise HarnessError("invalid %s: %s" % (field, exc))
    if parsed.tzinfo is None:
        raise HarnessError("%s must include a timezone" % field)
    return parsed.astimezone(_dt.timezone.utc)


def _normalized_guard_pattern(value, field="path pattern"):
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise HarnessError("%s must be a non-empty repository-relative POSIX pattern" % field)
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise HarnessError("%s must be repository-relative: %s" % (field, value))
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise HarnessError("%s contains an unsafe path segment: %s" % (field, value))
    return "/".join(parts)


def _guard_glob_match(path, pattern):
    path_parts = _normalized_guard_pattern(path, "repository path").split("/")
    pattern_parts = _normalized_guard_pattern(pattern).split("/")

    def match(path_index, pattern_index):
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


def _guard_matches_any(path, patterns):
    return any(_guard_glob_match(path, pattern) for pattern in patterns)


def _guard_directories(repo, create=False):
    root = _safe_repo_directory(repo, _SUBAGENT_GUARD_ROOT, create=create)
    return {
        "root": root,
        "pending": _safe_repo_directory(repo, _SUBAGENT_GUARD_ROOT + "/pending", create=create),
        "prestart": _safe_repo_directory(repo, _SUBAGENT_GUARD_ROOT + "/prestart", create=create),
        "active": _safe_repo_directory(repo, _SUBAGENT_GUARD_ROOT + "/active", create=create),
        "receipts": _safe_repo_directory(repo, _SUBAGENT_GUARD_ROOT + "/receipts", create=create),
    }


def _hook_repository(hook_input, verification):
    repo_value = verification.get("repo") or hook_input.get("cwd")
    repo = find_repo(repo_value)
    try:
        Path(hook_input["cwd"]).resolve().relative_to(repo.resolve())
    except (KeyError, OSError, ValueError):
        raise HarnessError("Hook cwd is outside the configured repository")
    return repo


def _canonical_subagent_role_policy():
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
    roles = {}
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
        "standard_version": STANDARD_VERSION,
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


def _load_subagent_policy(repo):
    policy_dir = _safe_repo_directory(repo, ".harness/policy", create=False)
    path = _safe_repo_path(repo, (policy_dir / "subagent-roles.json").relative_to(repo).as_posix(), must_exist=True)
    if path.is_symlink() or not path.is_file():
        raise HarnessError("subagent role policy is missing or unsafe")
    policy = strict_json_load(path)
    if not json_equal(policy, _canonical_subagent_role_policy()):
        raise HarnessError("project subagent role policy differs from the trusted canonical policy")
    roles = policy.get("roles")
    if not isinstance(roles, dict):
        raise HarnessError("subagent role policy roles is not an object")
    protected = policy.get("protected_acceptance_paths")
    if not isinstance(protected, list) or any(not isinstance(item, str) for item in protected):
        raise HarnessError("protected_acceptance_paths is invalid")
    for pattern in protected:
        _normalized_guard_pattern(pattern, "protected path pattern")
    return policy


def _guard_tuple(hook_input, repo):
    values = [str(repo.resolve())]
    for name in ("session_id", "turn_id", "agent_id"):
        value = hook_input.get(name)
        if not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value:
            raise HarnessError("Subagent Hook %s is missing or unsafe" % name)
        values.append(value)
    return tuple(values)


def _guard_key(hook_input, repo):
    return hashlib.sha256("\x00".join(_guard_tuple(hook_input, repo)).encode("utf-8")).hexdigest()


def _guard_atomic_create(path, document):
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".guard.", dir=str(path.parent))
    published = False
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, str(path), follow_symlinks=False)
        except FileExistsError:
            raise HarnessError("guard state target already exists: %s" % path)
        published = True
        os.unlink(temporary)
        temporary = None
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except BaseException:
        if published:
            try:
                path.unlink()
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise


def _guard_private_directory(path, create=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            raise HarnessError("protected Hook state directory is missing: %s" % path)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise HarnessError("protected Hook state path is not a real directory: %s" % path)
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise HarnessError("protected Hook state directory must have mode 0700: %s" % path)
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise HarnessError("protected Hook state directory is not owned by the current user: %s" % path)
    return path


def _guard_external_directories(repo, create=False):
    configured = os.environ.get("CODEX_HOME")
    base = Path(os.path.abspath(os.path.expanduser(configured))) if configured else Path.home() / ".codex"
    try:
        base_info = base.lstat()
    except FileNotFoundError:
        raise HarnessError("effective CODEX_HOME does not exist: %s" % base)
    if base.is_symlink() or not stat.S_ISDIR(base_info.st_mode):
        raise HarnessError("effective CODEX_HOME must be a real directory: %s" % base)
    if hasattr(os, "geteuid") and base_info.st_uid != os.geteuid():
        raise HarnessError("effective CODEX_HOME is not owned by the current user: %s" % base)
    current = base
    for part in (
        "software-delivery-harness",
        "state",
        hashlib.sha256(str(Path(repo).resolve()).encode("utf-8")).hexdigest(),
    ):
        current = _guard_private_directory(current / part, create=create)
    root = current
    active = _guard_private_directory(root / "active", create=create)
    receipts = _guard_private_directory(root / "receipts", create=create)
    return {"root": root, "active": active, "receipts": receipts}


def _guard_private_file(path, description):
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise HarnessError("%s is not a safe regular file" % description)
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise HarnessError("%s must have mode 0600" % description)
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise HarnessError("%s is not owned by the current user" % description)
    return info


def _guard_git_bytes(repo, args):
    git = shutil.which("git")
    if not git:
        raise HarnessError("git executable is unavailable")
    try:
        result = subprocess.run(
            [git, "-C", str(repo)] + list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HarnessError("Git snapshot command failed: %s" % exc)
    if result.returncode != 0:
        raise HarnessError(
            "Git snapshot command failed: %s"
            % result.stderr.decode("utf-8", errors="replace").strip()
        )
    return result.stdout


def _guard_snapshot_excluded(relative):
    normalized = relative.replace("\\", "/")
    return (
        normalized == ".git"
        or normalized.startswith(".git/")
        or normalized == _SUBAGENT_GUARD_ROOT
        or normalized.startswith(_SUBAGENT_GUARD_ROOT + "/")
        or normalized in RUNTIME_MUTABLE_PATHS
        or normalized in {".harness/release-report.json", ".harness/install-report.json"}
    )


def _guard_index_entries(raw):
    entries = {}
    for record in raw.split(b"\x00"):
        if not record:
            continue
        try:
            metadata, path_raw = record.split(b"\t", 1)
            mode, object_id, stage = metadata.decode("ascii").split(" ")
            path = path_raw.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise HarnessError("Git index contains an unsupported path or record")
        _normalized_guard_pattern(path, "Git index path")
        if _guard_snapshot_excluded(path):
            continue
        entries.setdefault(path, []).append({"mode": mode, "object_id": object_id, "stage": stage})
    for value in entries.values():
        value.sort(key=lambda item: (item["stage"], item["mode"], item["object_id"]))
    return entries


def _guard_snapshot_limits(policy):
    limits = policy.get("snapshot_limits") if isinstance(policy, dict) else None
    defaults = {"max_files": 50000, "max_total_bytes": 512 * 1024 * 1024, "max_file_bytes": 64 * 1024 * 1024}
    if not isinstance(limits, dict):
        raise HarnessError("subagent snapshot_limits is missing")
    result = {}
    for name, minimum, maximum in (
        ("max_files", 1, 1000000),
        ("max_total_bytes", 1, 16 * 1024 * 1024 * 1024),
        ("max_file_bytes", 1, 2 * 1024 * 1024 * 1024),
    ):
        value = limits.get(name, defaults[name])
        if not is_integer(value) or value < minimum or value > maximum:
            raise HarnessError("subagent snapshot limit %s is invalid" % name)
        result[name] = value
    return result


def _subagent_repository_snapshot(repo, policy):
    repo = Path(repo).resolve()
    top = _guard_git_bytes(repo, ["rev-parse", "--show-toplevel"]).decode("utf-8", errors="strict").strip()
    if Path(top).resolve() != repo:
        raise HarnessError("Subagent Hook cwd is not the configured Git top level")
    head = _guard_git_bytes(repo, ["rev-parse", "HEAD^{commit}"]).decode("ascii").strip().lower()
    if re.fullmatch(r"[0-9a-f]{40,64}", head) is None:
        raise HarnessError("Subagent Hook cannot resolve a valid Git HEAD")
    index_raw = _guard_git_bytes(repo, ["ls-files", "--stage", "-z"])
    status_raw = _guard_git_bytes(
        repo,
        ["status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignored=matching", "--ignore-submodules=none"],
    )
    limits = _guard_snapshot_limits(policy)
    entries = {}
    protected_documents = {}
    total_bytes = 0
    total_files = 0
    for directory, directory_names, file_names in os.walk(str(repo), topdown=True, followlinks=False):
        base = Path(directory)
        safe_directories = []
        for name in sorted(directory_names):
            item = base / name
            relative = item.relative_to(repo).as_posix()
            if _guard_snapshot_excluded(relative):
                continue
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode):
                target_text = os.readlink(str(item))
                target = item.resolve(strict=False)
                try:
                    target.relative_to(repo)
                except ValueError:
                    raise HarnessError("repository contains an external directory symlink: %s" % relative)
                entries[relative] = {
                    "type": "symlink", "mode": stat.S_IMODE(info.st_mode),
                    "sha256": hashlib.sha256(target_text.encode("utf-8")).hexdigest(),
                    "size": len(target_text.encode("utf-8")),
                }
                total_files += 1
            elif stat.S_ISDIR(info.st_mode):
                safe_directories.append(name)
            else:
                raise HarnessError("repository contains an unsupported directory entry: %s" % relative)
        directory_names[:] = safe_directories
        for name in sorted(file_names):
            item = base / name
            relative = item.relative_to(repo).as_posix()
            if _guard_snapshot_excluded(relative):
                continue
            _normalized_guard_pattern(relative, "repository path")
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode):
                target_text = os.readlink(str(item))
                target = item.resolve(strict=False)
                try:
                    target.relative_to(repo)
                except ValueError:
                    raise HarnessError("repository contains an external file symlink: %s" % relative)
                encoded = target_text.encode("utf-8")
                entries[relative] = {
                    "type": "symlink", "mode": stat.S_IMODE(info.st_mode),
                    "sha256": hashlib.sha256(encoded).hexdigest(), "size": len(encoded),
                }
                total_bytes += len(encoded)
            elif stat.S_ISREG(info.st_mode):
                digest, size = _hash_regular_file(item, limits["max_file_bytes"])
                entries[relative] = {
                    "type": "file", "mode": stat.S_IMODE(info.st_mode),
                    "sha256": digest, "size": size,
                }
                total_bytes += size
                if relative in _SUBAGENT_PROTECTED_DOCUMENTS:
                    protected_documents[relative] = strict_json_load(item)
            else:
                raise HarnessError("repository contains an unsupported special file: %s" % relative)
            total_files += 1
            if total_files > limits["max_files"] or total_bytes > limits["max_total_bytes"]:
                raise HarnessError("repository exceeds complete Subagent Hook snapshot limits")
    index_entries = _guard_index_entries(index_raw)
    payload = {
        "git_head": head,
        "git_root": str(repo),
        "git_index": index_entries,
        "git_status_sha256": hashlib.sha256(status_raw).hexdigest(),
        "entries": entries,
        "protected_documents": protected_documents,
        "file_count": total_files,
        "total_bytes": total_bytes,
    }
    payload["snapshot_sha256"] = canonical_sha256(payload)
    return payload


def _validate_string_array(document, name, patterns=False):
    values = document.get(name)
    if not isinstance(values, list) or any(not isinstance(item, str) or not item for item in values):
        raise HarnessError("pending authorization %s must be an array of non-empty strings" % name)
    if len(values) != len(set(values)):
        raise HarnessError("pending authorization %s contains duplicates" % name)
    if patterns:
        for value in values:
            _normalized_guard_pattern(value, "pending authorization %s" % name)
    return values


def _validate_pending_authorization(document, hook_input, repo, policy):
    if not isinstance(document, dict):
        raise HarnessError("pending authorization is not a JSON object")
    required = {
        "schema_version", "authorization_id", "state", "repo_realpath", "created_at",
        "expires_at", "expected_agent_type", "expected_session_id", "expected_turn_id",
        "expected_worktree_realpath", "role_policy_sha256", "allowed_paths",
        "allowed_operations", "review_paths", "required_files",
        "expected_requirement_ids", "expected_test_case_ids", "acceptance_change_ids",
        "approved_change_entries", "expected_evidence_ids", "evidence_run_prefix",
        "exclusive_repo_lease", "issuer", "git_head",
    }
    missing = sorted(required - set(document))
    extra = sorted(set(document) - required)
    if missing or extra:
        raise HarnessError(
            "pending authorization fields mismatch; missing=%s extra=%s"
            % (missing, extra)
        )
    if document.get("schema_version") != STANDARD_VERSION or document.get("state") != "PENDING":
        raise HarnessError("pending authorization version/state is invalid")
    if not isinstance(document.get("authorization_id"), str) or re.fullmatch(
        r"[A-Z][A-Z0-9_.-]{2,127}", document["authorization_id"]
    ) is None:
        raise HarnessError("pending authorization_id is unsafe")
    try:
        authorized_repo = Path(document.get("repo_realpath")).resolve()
    except (TypeError, OSError, ValueError):
        raise HarnessError("pending authorization repo_realpath is invalid")
    if authorized_repo != repo.resolve():
        raise HarnessError("pending authorization belongs to another repository")
    agent_type = hook_input.get("agent_type")
    if document.get("expected_agent_type") != agent_type:
        raise HarnessError("pending authorization agent_type does not match")
    roles = policy.get("roles")
    role = roles.get(agent_type) if isinstance(roles, dict) else None
    if not isinstance(role, dict):
        raise HarnessError("unknown or invalid static agent role: %s" % agent_type)
    if role.get("mode") not in {"read_only", "write", "evidence_only"}:
        raise HarnessError("static agent role mode is invalid")
    for name in ("allow_paths", "deny_paths", "allowed_operations"):
        values = role.get(name)
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise HarnessError("static role %s is invalid" % name)
        if name != "allowed_operations":
            for value in values:
                _normalized_guard_pattern(value, "static role pattern")
    if canonical_sha256(policy) != document.get("role_policy_sha256"):
        raise HarnessError("pending authorization role policy hash is stale")
    now = _dt.datetime.now(_dt.timezone.utc)
    created = _parse_hook_utc(document.get("created_at"), "created_at")
    expires = _parse_hook_utc(document.get("expires_at"), "expires_at")
    if created > now + _dt.timedelta(minutes=5) or expires <= now or expires <= created:
        raise HarnessError("pending authorization is expired or has invalid timestamps")
    for pending_name, wire_name in (
        ("expected_session_id", "session_id"),
        ("expected_turn_id", "turn_id"),
    ):
        expected = document.get(pending_name)
        if expected is not None and (not isinstance(expected, str) or expected != hook_input.get(wire_name)):
            raise HarnessError("pending authorization %s does not match" % pending_name)
    expected_worktree = document.get("expected_worktree_realpath")
    if expected_worktree is not None:
        if not isinstance(expected_worktree, str) or Path(expected_worktree).resolve() != repo.resolve():
            raise HarnessError("pending authorization worktree does not match")
    if document.get("exclusive_repo_lease") is not True:
        raise HarnessError("pending authorization must require an exclusive repository lease")
    if not isinstance(document.get("issuer"), str) or not document["issuer"].strip():
        raise HarnessError("pending authorization issuer is missing")
    if re.fullmatch(r"[0-9a-f]{40,64}", str(document.get("git_head", ""))) is None:
        raise HarnessError("pending authorization git_head is invalid")
    allowed_paths = _validate_string_array(document, "allowed_paths", patterns=True)
    review_paths = _validate_string_array(document, "review_paths", patterns=True)
    required_files = _validate_string_array(document, "required_files", patterns=True)
    operations = _validate_string_array(document, "allowed_operations")
    if any(item not in {"add", "modify", "delete"} for item in operations):
        raise HarnessError("pending authorization contains an unsupported operation")
    for name in (
        "expected_requirement_ids", "expected_test_case_ids",
        "acceptance_change_ids", "expected_evidence_ids",
    ):
        _validate_string_array(document, name)
    if not any(
        document.get(name)
        for name in (
            "expected_requirement_ids", "expected_test_case_ids", "expected_evidence_ids"
        )
    ):
        raise HarnessError(
            "pending authorization has no expected requirement, test-case, or evidence scope"
        )
    approved_entries = document.get("approved_change_entries")
    if not isinstance(approved_entries, list) or any(not isinstance(item, dict) for item in approved_entries):
        raise HarnessError("pending approved_change_entries is invalid")
    approved_ids = [item.get("change_id") for item in approved_entries]
    if sorted(approved_ids) != sorted(document["acceptance_change_ids"]):
        raise HarnessError("pending approved change entries do not match acceptance_change_ids")
    for item in approved_entries:
        if not isinstance(item.get("approved_by"), str) or not item["approved_by"].strip():
            raise HarnessError("pending acceptance change lacks an approver")
        if item["approved_by"].strip() == document["issuer"].strip():
            raise HarnessError("pending acceptance change is not independently approved")
        if not isinstance(item.get("requirement_ids"), list) or not item["requirement_ids"]:
            raise HarnessError("pending acceptance change lacks requirement_ids")
        _parse_hook_utc(item.get("timestamp"), "approved change timestamp")
    run_prefix = document.get("evidence_run_prefix")
    if run_prefix is not None:
        _normalized_guard_pattern(run_prefix, "evidence_run_prefix")
    mode = role["mode"]
    if mode == "read_only":
        if allowed_paths or operations or required_files or not review_paths:
            raise HarnessError("read-only authorization cannot grant writes and must name review_paths")
    else:
        if not allowed_paths or not operations or not required_files:
            raise HarnessError("write/evidence authorization requires paths, operations, and exact required files")
        if any(any(token in path for token in ("*", "?", "[")) for path in required_files):
            raise HarnessError("write/evidence required files must be concrete paths")
        outside_grant = [
            path for path in required_files if not _guard_matches_any(path, allowed_paths)
        ]
        if outside_grant:
            raise HarnessError(
                "write/evidence required files escape allowed_paths: %s"
                % ", ".join(outside_grant)
            )
        static_operations = set(role.get("allowed_operations", []))
        if not set(operations).issubset(static_operations):
            raise HarnessError("pending authorization operations exceed the static role")
    if mode == "evidence_only":
        if not isinstance(run_prefix, str) or not run_prefix.startswith(".harness/runs/"):
            raise HarnessError("evidence-only authorization requires an evidence run prefix")
        if any(not (path == run_prefix or path.startswith(run_prefix + "/")) for path in allowed_paths):
            raise HarnessError("evidence-only authorization escapes its run prefix")
    return role


def _select_pending_authorization(hook_input, repo, policy, pending_dir):
    matches = []
    now = _dt.datetime.now(_dt.timezone.utc)
    for path in sorted(pending_dir.iterdir()):
        if path.name.startswith("."):
            continue
        if path.is_symlink() or not path.is_file() or path.suffix != ".json":
            raise HarnessError("pending authorization directory contains an unsafe entry: %s" % path.name)
        document = strict_json_load(path)
        if not isinstance(document, dict):
            raise HarnessError("pending authorization is not an object: %s" % path.name)
        try:
            expires = _parse_hook_utc(document.get("expires_at"), "expires_at")
        except HarnessError:
            raise HarnessError("pending authorization has an invalid expiry: %s" % path.name)
        if expires <= now:
            continue
        if document.get("repo_realpath") != str(repo.resolve()):
            continue
        if document.get("expected_agent_type") != hook_input.get("agent_type"):
            continue
        if document.get("expected_session_id") not in (None, hook_input.get("session_id")):
            continue
        if document.get("expected_turn_id") not in (None, hook_input.get("turn_id")):
            continue
        if document.get("expected_worktree_realpath") not in (None, str(repo.resolve())):
            continue
        role = _validate_pending_authorization(document, hook_input, repo, policy)
        matches.append((path, document, role))
    if len(matches) != 1:
        raise HarnessError(
            "SubagentStart requires exactly one unexpired matching pending authorization; found %d"
            % len(matches)
        )
    return matches[0]


def _prestart_lease_integrity(document):
    value = dict(document)
    value.pop("integrity_sha256", None)
    return canonical_sha256(value)


def _load_prestart_lease(path):
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise HarnessError("pre-spawn reservation lease is unsafe")
    document = strict_json_load(path)
    required = {
        "standard_version", "kind", "state", "repo_realpath", "session_id",
        "turn_id", "tool_use_id", "agent_type", "authorization_file",
        "authorization_sha256", "role_policy_sha256", "created_at", "expires_at",
        "integrity_sha256",
    }
    if not isinstance(document, dict) or set(document) != required:
        raise HarnessError("pre-spawn reservation lease fields are invalid")
    if (
        document.get("standard_version") != STANDARD_VERSION
        or document.get("kind") != "subagent_prestart_lease"
        or document.get("state") != "RESERVED"
        or document.get("integrity_sha256") != _prestart_lease_integrity(document)
    ):
        raise HarnessError("pre-spawn reservation lease integrity is invalid")
    for name in ("repo_realpath", "session_id", "turn_id", "tool_use_id", "agent_type"):
        value = document.get(name)
        if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
            raise HarnessError("pre-spawn reservation lease %s is unsafe" % name)
    if re.fullmatch(r"[a-f0-9]{64}\.json", str(document.get("authorization_file", ""))) is None:
        raise HarnessError("pre-spawn reservation authorization filename is unsafe")
    for name in ("authorization_sha256", "role_policy_sha256"):
        if re.fullmatch(r"[a-f0-9]{64}", str(document.get(name, ""))) is None:
            raise HarnessError("pre-spawn reservation %s is invalid" % name)
    created = _parse_hook_utc(document.get("created_at"), "prestart created_at")
    expires = _parse_hook_utc(document.get("expires_at"), "prestart expires_at")
    if expires <= created:
        raise HarnessError("pre-spawn reservation timestamps are invalid")
    return document


def _pretool_authorization_input(hook_input):
    tool_input = hook_input.get("tool_input")
    if not isinstance(tool_input, dict):
        raise HarnessError("Agent tool_input must be an object")
    agent_type = tool_input.get("agent_type")
    if not isinstance(agent_type, str) or not agent_type.strip():
        raise HarnessError("Agent tool_input.agent_type is missing")
    return {
        "agent_type": agent_type,
        "session_id": hook_input.get("session_id"),
        "turn_id": hook_input.get("turn_id"),
    }


def _pretool_agent_block(reason):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "Subagent spawn authorization is BLOCKED: %s" % reason,
        },
        "systemMessage": "The Agent tool was denied before execution; issue one bounded authorization and retry exactly once.",
    }, 0


def _pretool_agent_allow(authorization_id):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": (
                "Harness authorization %s was atomically reserved for this one Agent tool call."
                % authorization_id
            ),
        }
    }, 0


def _pretool_agent_response(hook_input, verification):
    errors = _hook_wire_errors(hook_input, "pretooluse")
    if errors:
        return _pretool_agent_block("; ".join(errors))
    if not _is_agent_tool_name(hook_input.get("tool_name")):
        return _pretool_agent_block("configured matcher received a non-Agent tool call")
    try:
        repo = _hook_repository(hook_input, verification)
        directories = _guard_directories(repo, create=False)
        policy = _load_subagent_policy(repo)
        authorization_input = _pretool_authorization_input(hook_input)
        active_lease = _load_guard_lease(directories["root"] / "lease.json")
        if active_lease is not None:
            raise HarnessError("another subagent owns the exclusive repository lease")
        prestart_lease_path = directories["root"] / "prestart-lease.json"
        existing = _load_prestart_lease(prestart_lease_path)
        if existing is not None:
            expected = (
                str(repo.resolve()), hook_input.get("session_id"), hook_input.get("turn_id"),
                hook_input.get("tool_use_id"), authorization_input.get("agent_type"),
            )
            observed = tuple(
                existing.get(name)
                for name in ("repo_realpath", "session_id", "turn_id", "tool_use_id", "agent_type")
            )
            reservation_path = directories["prestart"] / existing["authorization_file"]
            if observed == expected and reservation_path.is_file() and not reservation_path.is_symlink():
                authorization = strict_json_load(reservation_path)
                if (
                    _parse_hook_utc(existing.get("expires_at"), "prestart expires_at")
                    > _dt.datetime.now(_dt.timezone.utc)
                    and existing.get("role_policy_sha256") == canonical_sha256(policy)
                    and canonical_sha256(authorization) == existing.get("authorization_sha256")
                ):
                    _validate_pending_authorization(
                        authorization, authorization_input, repo, policy
                    )
                    return _pretool_agent_allow(authorization.get("authorization_id"))
            raise HarnessError("another Agent tool call owns the pre-spawn authorization reservation")
        pending_path, authorization, _role = _select_pending_authorization(
            authorization_input, repo, policy, directories["pending"]
        )
        reservation_name = hashlib.sha256(
            (
                authorization["authorization_id"] + "\x00" + hook_input["session_id"]
                + "\x00" + hook_input["turn_id"] + "\x00" + hook_input["tool_use_id"]
            ).encode("utf-8")
        ).hexdigest() + ".json"
        reservation_path = directories["prestart"] / reservation_name
        if reservation_path.exists() or reservation_path.is_symlink():
            raise HarnessError("pre-spawn reservation target already exists")
        lease = {
            "standard_version": STANDARD_VERSION,
            "kind": "subagent_prestart_lease",
            "state": "RESERVED",
            "repo_realpath": str(repo.resolve()),
            "session_id": hook_input["session_id"],
            "turn_id": hook_input["turn_id"],
            "tool_use_id": hook_input["tool_use_id"],
            "agent_type": authorization_input["agent_type"],
            "authorization_file": reservation_name,
            "authorization_sha256": canonical_sha256(authorization),
            "role_policy_sha256": canonical_sha256(policy),
            "created_at": utc_now(),
            "expires_at": authorization["expires_at"],
        }
        lease["integrity_sha256"] = _prestart_lease_integrity(lease)
        _guard_atomic_create(prestart_lease_path, lease)
        published = False
        try:
            os.link(str(pending_path), str(reservation_path), follow_symlinks=False)
            published = True
            if canonical_sha256(strict_json_load(reservation_path)) != lease["authorization_sha256"]:
                raise HarnessError("pre-spawn authorization changed during reservation")
            pending_path.unlink()
        except BaseException:
            if published:
                try:
                    reservation_path.unlink()
                except OSError:
                    pass
            try:
                prestart_lease_path.unlink()
            except OSError:
                pass
            raise
        return _pretool_agent_allow(authorization.get("authorization_id"))
    except (HarnessError, OSError, ValueError) as exc:
        return _pretool_agent_block("%s: %s" % (type(exc).__name__, exc))


def _select_prestart_authorization(hook_input, repo, policy, directories):
    lease_path = directories["root"] / "prestart-lease.json"
    lease = _load_prestart_lease(lease_path)
    if lease is None:
        raise HarnessError("SubagentStart has no matching PreToolUse Agent reservation")
    expected = (
        str(repo.resolve()), hook_input.get("session_id"), hook_input.get("turn_id"),
        hook_input.get("agent_type"),
    )
    observed = tuple(
        lease.get(name) for name in ("repo_realpath", "session_id", "turn_id", "agent_type")
    )
    if observed != expected:
        raise HarnessError("SubagentStart does not match the reserved Agent tool call")
    if lease.get("role_policy_sha256") != canonical_sha256(policy):
        raise HarnessError("subagent role policy changed after PreToolUse authorization")
    if _parse_hook_utc(lease.get("expires_at"), "prestart expires_at") <= _dt.datetime.now(_dt.timezone.utc):
        raise HarnessError("pre-spawn authorization reservation expired")
    authorization_path = directories["prestart"] / lease["authorization_file"]
    if authorization_path.is_symlink() or not authorization_path.is_file():
        raise HarnessError("reserved authorization is missing or unsafe")
    authorization = strict_json_load(authorization_path)
    if canonical_sha256(authorization) != lease.get("authorization_sha256"):
        raise HarnessError("reserved authorization integrity mismatch")
    role = _validate_pending_authorization(authorization, hook_input, repo, policy)
    return authorization_path, authorization, role, lease_path


def _active_integrity(document):
    value = dict(document)
    value.pop("integrity_sha256", None)
    return canonical_sha256(value)


def _load_active_document(path, require_private=False):
    if path.is_symlink() or not path.is_file():
        raise HarnessError("active subagent state is missing or unsafe")
    if require_private:
        _guard_private_file(path, "protected active subagent state")
    document = strict_json_load(path)
    if not isinstance(document, dict):
        raise HarnessError("active subagent state is not an object")
    if document.get("integrity_sha256") != _active_integrity(document):
        raise HarnessError("active subagent state integrity mismatch")
    return document


def _load_guard_lease(path, require_private=False):
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise HarnessError("subagent lease is unsafe")
    if require_private:
        _guard_private_file(path, "protected subagent lease")
    document = strict_json_load(path)
    if not isinstance(document, dict) or not isinstance(document.get("guard_key"), str):
        raise HarnessError("subagent lease is malformed")
    return document


def _subagent_start_block(reason):
    return {
        "continue": False,
        "stopReason": "Subagent authorization is BLOCKED: %s" % reason,
        "systemMessage": "Do not start the subagent until the pending authorization and repository state are repaired.",
    }, 0


def _subagent_start_response(hook_input, verification):
    errors = _hook_wire_errors(hook_input, "subagentstart")
    if errors:
        return _subagent_start_block("; ".join(errors))
    try:
        repo = _hook_repository(hook_input, verification)
        directories = _guard_directories(repo, create=False)
        protected = _guard_external_directories(repo, create=True)
        policy = _load_subagent_policy(repo)
        key = _guard_key(hook_input, repo)
        active_path = directories["active"] / (key + ".json")
        lease_path = directories["root"] / "lease.json"
        protected_active_path = protected["active"] / (key + ".json")
        protected_lease_path = protected["root"] / "lease.json"
        existing_protected_lease = _load_guard_lease(
            protected_lease_path, require_private=True
        )
        existing_mirror_lease = _load_guard_lease(lease_path)
        if (existing_protected_lease is None) != (existing_mirror_lease is None):
            raise HarnessError("protected and repository lease mirrors disagree")
        if existing_protected_lease is not None:
            if not json_equal(existing_protected_lease, existing_mirror_lease):
                raise HarnessError("repository lease mirror differs from protected state")
            if existing_protected_lease.get("guard_key") != key:
                raise HarnessError("another subagent owns the exclusive repository lease")
            active = _load_active_document(protected_active_path, require_private=True)
            mirror_active = _load_active_document(active_path)
            if not json_equal(active, mirror_active):
                raise HarnessError("repository active-state mirror differs from protected state")
            if active.get("correlation") != list(_guard_tuple(hook_input, repo)):
                raise HarnessError("idempotent SubagentStart correlation does not match active state")
            return {
                "hookSpecificOutput": {
                    "hookEventName": "SubagentStart",
                    "additionalContext": "Harness authorization %s remains active; obey its exact path and acceptance boundaries."
                    % active.get("authorization", {}).get("authorization_id"),
                }
            }, 0
        reservation_path, authorization, role, prestart_lease_path = _select_prestart_authorization(
            hook_input, repo, policy, directories
        )
        lease = {
            "standard_version": STANDARD_VERSION,
            "guard_key": key,
            "correlation": list(_guard_tuple(hook_input, repo)),
            "agent_type": hook_input.get("agent_type"),
            "authorization_id": authorization.get("authorization_id"),
            "created_at": utc_now(),
        }
        _guard_atomic_create(protected_lease_path, lease)
        try:
            _guard_atomic_create(lease_path, lease)
            snapshot = _subagent_repository_snapshot(repo, policy)
            if snapshot.get("git_head") != authorization.get("git_head"):
                raise HarnessError("Git HEAD changed after the pending authorization was issued")
            if active_path.exists() or active_path.is_symlink():
                raise HarnessError("active subagent target already exists")
            active = {
                "standard_version": STANDARD_VERSION,
                "kind": "subagent_guard_active",
                "guard_key": key,
                "correlation": list(_guard_tuple(hook_input, repo)),
                "agent_type": hook_input.get("agent_type"),
                "authorization": authorization,
                "authorization_sha256": canonical_sha256(authorization),
                "role": role,
                "policy_sha256": canonical_sha256(policy),
                "start_snapshot": snapshot,
                "trusted_runtime_prefixes": [],
                "started_at": utc_now(),
            }
            active["integrity_sha256"] = _active_integrity(active)
            _guard_atomic_create(protected_active_path, active)
            _guard_atomic_create(active_path, active)
        except BaseException:
            try:
                lease_path.unlink()
            except OSError:
                pass
            try:
                protected_lease_path.unlink()
            except OSError:
                pass
            try:
                active_path.unlink()
            except OSError:
                pass
            try:
                protected_active_path.unlink()
            except OSError:
                pass
            raise
        cleanup_warnings = []
        for consumed in (reservation_path, prestart_lease_path):
            try:
                consumed.unlink()
            except OSError as exc:
                cleanup_warnings.append("could not remove consumed pre-spawn state %s: %s" % (consumed.name, exc))
        context = (
            "Harness authorization %s is active. Modify only authorized paths; "
            "acceptance changes require the recorded independent grant."
        ) % authorization.get("authorization_id")
        if cleanup_warnings:
            context += " Guard cleanup warning: " + "; ".join(cleanup_warnings)
        return {
            "hookSpecificOutput": {
                "hookEventName": "SubagentStart",
                "additionalContext": context,
            }
        }, 0
    except (HarnessError, OSError, ValueError) as exc:
        return _subagent_start_block("%s: %s" % (type(exc).__name__, exc))


def _guard_runtime_prefixes(repo, verification):
    prefixes = []
    run_dir = verification.get("run_dir")
    if isinstance(run_dir, str) and run_dir:
        try:
            relative = Path(run_dir).resolve().relative_to(Path(repo).resolve()).as_posix()
            prefixes.append(relative)
        except (OSError, ValueError):
            pass
    collector = verification.get("collector_result")
    if isinstance(collector, dict) and isinstance(collector.get("run_id"), str):
        prefixes.append(".harness/runs/%s" % collector["run_id"])
    collected_run_id = verification.get("validated_collection_run_id")
    if verification.get("overall_status") == "PASS" and isinstance(collected_run_id, str):
        try:
            validate_run_id(collected_run_id)
        except HarnessError:
            pass
        else:
            collected_prefix = ".harness/runs/%s/" % collected_run_id
            validity = verification.get("evidence_validity")
            if isinstance(validity, dict):
                for item in validity.values():
                    path = item.get("path") if isinstance(item, dict) else None
                    if (
                        isinstance(path, str)
                        and item.get("valid") is True
                        and path.startswith(collected_prefix)
                    ):
                        prefixes.append(path)
    return sorted(set(prefixes))


def _normalized_guard_runtime_prefix(value):
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise HarnessError("trusted Hook runtime prefix must be a non-empty bounded string")
    if "\\" in value or Path(value).is_absolute():
        raise HarnessError("trusted Hook runtime prefix must be a POSIX repository-relative path")
    parts = value.split("/")
    if len(parts) < 3 or parts[:2] != [".harness", "runs"]:
        raise HarnessError("trusted Hook runtime prefix must be inside .harness/runs")
    if any(part in {"", ".", ".."} for part in parts):
        raise HarnessError("trusted Hook runtime prefix contains an unsafe path component")
    validate_run_id(parts[2])
    return "/".join(parts)


def _guard_accumulated_runtime_prefixes(active, current_prefixes):
    stored = active.get("trusted_runtime_prefixes", []) if isinstance(active, dict) else []
    if not isinstance(stored, list) or not isinstance(current_prefixes, (list, tuple)):
        raise HarnessError("trusted Hook runtime prefixes are malformed")
    merged = []
    for value in list(stored) + list(current_prefixes):
        normalized = _normalized_guard_runtime_prefix(value)
        if normalized not in merged:
            merged.append(normalized)
    if len(merged) > 128:
        raise HarnessError("trusted Hook runtime prefix history exceeds the bounded limit")
    return sorted(merged)


def _persist_guard_runtime_prefixes(active, protected_path, mirror_path, prefixes):
    protected_before = _load_active_document(protected_path, require_private=True)
    mirror_before = _load_active_document(mirror_path)
    if not json_equal(protected_before, mirror_before) or not json_equal(
        protected_before, active
    ):
        raise HarnessError("trusted Hook runtime prefix mirrors differ before persistence")
    if active.get("trusted_runtime_prefixes", []) == prefixes:
        return protected_before
    updated = dict(active)
    updated["trusted_runtime_prefixes"] = list(prefixes)
    updated["integrity_sha256"] = _active_integrity(updated)
    protected_written = False
    try:
        atomic_write_json(protected_path, updated)
        protected_written = True
        _guard_private_file(protected_path, "protected active subagent state")
        atomic_write_json(mirror_path, updated)
    except BaseException as exc:
        if protected_written:
            try:
                atomic_write_json(protected_path, protected_before)
                _guard_private_file(protected_path, "protected active subagent state")
            except BaseException as rollback_exc:
                raise HarnessError(
                    "trusted Hook runtime prefix persistence failed (%s) and protected-state rollback failed (%s)"
                    % (exc, rollback_exc)
                )
        raise
    protected = _load_active_document(protected_path, require_private=True)
    mirror = _load_active_document(mirror_path)
    if not json_equal(protected, mirror):
        raise HarnessError("trusted Hook runtime prefix mirrors differ after persistence")
    return protected


def _subagent_snapshot_diff(start, stop, runtime_prefixes=()):
    start_entries = start.get("entries") if isinstance(start, dict) else None
    stop_entries = stop.get("entries") if isinstance(stop, dict) else None
    start_index = start.get("git_index") if isinstance(start, dict) else None
    stop_index = stop.get("git_index") if isinstance(stop, dict) else None
    if not all(isinstance(item, dict) for item in (start_entries, stop_entries, start_index, stop_index)):
        raise HarnessError("subagent snapshot entries are malformed")

    def excluded(path):
        return any(path == prefix or path.startswith(prefix + "/") for prefix in runtime_prefixes)

    changed = {}
    for path in sorted(set(start_entries) | set(stop_entries) | set(start_index) | set(stop_index)):
        if excluded(path):
            continue
        before = {"filesystem": start_entries.get(path), "index": start_index.get(path)}
        after = {"filesystem": stop_entries.get(path), "index": stop_index.get(path)}
        if json_equal(before, after):
            continue
        before_exists = path in start_entries or path in start_index
        after_exists = path in stop_entries or path in stop_index
        operation = "add" if not before_exists and after_exists else (
            "delete" if before_exists and not after_exists else "modify"
        )
        changed[path] = {
            "operation": operation,
            "before": before,
            "after": after,
        }
    return changed


def _changed_requirement_ids(path, before, after):
    root_key = _SUBAGENT_PROTECTED_DOCUMENTS.get(path)
    if root_key is None:
        return set()
    before_rows = before.get(root_key, []) if isinstance(before, dict) else []
    after_rows = after.get(root_key, []) if isinstance(after, dict) else []
    if not isinstance(before_rows, list) or not isinstance(after_rows, list):
        raise HarnessError("protected document %s has an invalid root array" % path)
    if path.endswith("requirements.json"):
        identity = "requirement_id"
    elif path.endswith("test-cases.json"):
        identity = "test_case_id"
    elif path.endswith("traceability.json"):
        identity = "trace_id"
    elif path.endswith("acceptance-results.json"):
        identity = "requirement_id"
    else:
        return set()
    before_by_id = {
        item.get(identity): item for item in before_rows
        if isinstance(item, dict) and isinstance(item.get(identity), str)
    }
    after_by_id = {
        item.get(identity): item for item in after_rows
        if isinstance(item, dict) and isinstance(item.get(identity), str)
    }
    changed_ids = {
        item_id for item_id in set(before_by_id) | set(after_by_id)
        if not json_equal(before_by_id.get(item_id), after_by_id.get(item_id))
    }
    if path.endswith("requirements.json") or path.endswith("acceptance-results.json"):
        return changed_ids
    requirements = set()
    for item_id in changed_ids:
        for row in (before_by_id.get(item_id), after_by_id.get(item_id)):
            if not isinstance(row, dict):
                continue
            if path.endswith("traceability.json"):
                requirement_id = row.get("requirement_id")
                if isinstance(requirement_id, str):
                    requirements.add(requirement_id)
            else:
                requirements.update(_list_of_strings(row.get("requirement_ids")))
    return requirements


def _subagent_path_policy_errors(active, policy, changed):
    errors = []
    authorization = active.get("authorization") if isinstance(active, dict) else None
    role = active.get("role") if isinstance(active, dict) else None
    if not isinstance(authorization, dict) or not isinstance(role, dict):
        return ["active authorization or static role is malformed"]
    mode = role.get("mode")
    allowed_paths = authorization.get("allowed_paths", [])
    allowed_operations = set(authorization.get("allowed_operations", []))
    role_allow = role.get("allow_paths", [])
    role_deny = role.get("deny_paths", [])
    protected_patterns = policy.get("protected_acceptance_paths", [])
    change_ids = set(authorization.get("acceptance_change_ids", []))
    approved_requirements = set()
    for entry in authorization.get("approved_change_entries", []):
        if isinstance(entry, dict) and entry.get("change_id") in change_ids:
            approved_requirements.update(_list_of_strings(entry.get("requirement_ids")))
    if mode == "read_only" and changed:
        errors.append("read-only agent modified the repository")
    run_prefix = authorization.get("evidence_run_prefix")
    for path, delta in sorted(changed.items()):
        operation = delta.get("operation")
        try:
            _normalized_guard_pattern(path, "changed path")
        except HarnessError as exc:
            errors.append(str(exc))
            continue
        protected = _guard_matches_any(path, protected_patterns)
        grantable = path in _SUBAGENT_GRANTABLE_DOCUMENTS
        has_grant = bool(change_ids) and grantable
        if operation not in allowed_operations:
            errors.append("operation %s is not authorized for %s" % (operation, path))
        if mode == "evidence_only":
            if not isinstance(run_prefix, str) or not (
                path == run_prefix or path.startswith(run_prefix + "/")
            ):
                errors.append("evidence-only agent changed a path outside its run: %s" % path)
        elif mode != "read_only":
            if not _guard_matches_any(path, allowed_paths):
                errors.append("changed path is outside the pending authorization: %s" % path)
            if not _guard_matches_any(path, role_allow):
                errors.append("changed path is outside the static role: %s" % path)
        denied = _guard_matches_any(path, role_deny)
        if denied and not has_grant:
            errors.append("changed path is denied by the static role: %s" % path)
        if protected:
            if not has_grant:
                errors.append("protected acceptance/policy path changed without an external grant: %s" % path)
                continue
            before_documents = active.get("start_snapshot", {}).get("protected_documents", {})
            after_documents = active.get("stop_snapshot", {}).get("protected_documents", {})
            affected = _changed_requirement_ids(
                path, before_documents.get(path), after_documents.get(path)
            )
            if not affected:
                errors.append("protected semantic change has no provable requirement scope: %s" % path)
            elif not affected.issubset(approved_requirements):
                errors.append(
                    "protected change affects requirements outside approved change records: %s (%s)"
                    % (path, ", ".join(sorted(affected - approved_requirements)))
                )
    required_files = authorization.get("required_files", [])
    if mode != "read_only":
        undeclared = sorted(set(changed) - set(required_files))
        if undeclared:
            errors.append(
                "changed paths were not declared as exact required files: %s"
                % ", ".join(undeclared)
            )
    for required_path in required_files:
        delta = changed.get(required_path)
        if not isinstance(delta, dict):
            errors.append("required file was not changed: %s" % required_path)
            continue
        operation = delta.get("operation")
        after = delta.get("after") if isinstance(delta.get("after"), dict) else {}
        filesystem = after.get("filesystem") if isinstance(after, dict) else None
        if operation != "delete" and (
            not isinstance(filesystem, dict) or filesystem.get("type") != "file"
        ):
            errors.append("required changed path is not a regular file: %s" % required_path)
        if mode == "evidence_only":
            if operation == "delete":
                errors.append("evidence-only required output was deleted: %s" % required_path)
            elif not isinstance(filesystem, dict) or filesystem.get("type") != "file":
                errors.append("evidence-only required output is not a regular file: %s" % required_path)
            elif not is_integer(filesystem.get("size")) or filesystem.get("size") <= 0:
                errors.append("evidence-only required output is empty: %s" % required_path)
    return errors


def _empty_summary_marker(value):
    return str(value or "").strip().lower() in {"none", "n/a", "na", "[]", "-", "null"}


def _parse_subagent_summary(message):
    required = ("TASK_STATUS", "FILES", "EVIDENCE", "FAILURES", "BLOCKERS", "NOT_RUN")
    values = {}
    errors = []
    text = message if isinstance(message, str) else ""
    for name in required:
        matches = re.findall(r"(?m)^%s\s*:\s*(.*?)\s*$" % name, text)
        if len(matches) != 1 or not matches[0].strip():
            errors.append("%s must appear exactly once with a non-empty value" % name)
        else:
            values[name] = matches[0].strip()
    return values, errors


def _summary_paths(value, field):
    if _empty_summary_marker(value):
        return []
    paths = []
    for item in re.split(r"\s*[,;]\s*", str(value)):
        if not item:
            continue
        normalized = _normalized_guard_pattern(item, "%s path" % field)
        if any(char in normalized for char in "*?["):
            raise HarnessError("%s must name concrete paths, not patterns: %s" % (field, item))
        paths.append(normalized)
    if len(paths) != len(set(paths)):
        raise HarnessError("%s contains duplicate paths" % field)
    return sorted(paths)


def _subagent_evidence_errors(values, expected_status, repo, verification, authorization):
    errors = []
    evidence_value = values.get("EVIDENCE")
    if expected_status in {"PASS", "FAIL"} and _empty_summary_marker(evidence_value):
        return ["EVIDENCE must name current verified machine evidence for %s" % expected_status]
    if _empty_summary_marker(evidence_value):
        return errors
    try:
        claimed = _summary_paths(evidence_value, "EVIDENCE")
    except HarnessError as exc:
        return [str(exc)]
    validity = verification.get("evidence_validity")
    if not isinstance(validity, dict):
        return ["current verification lacks evidence_validity"]
    report = verification.get("report")
    artifacts = report.get("artifacts") if isinstance(report, dict) else None
    if not isinstance(artifacts, list):
        return ["current verification report lacks artifacts"]
    artifact_index = {
        (item.get("path"), item.get("sha256"), item.get("artifact_type"))
        for item in artifacts if isinstance(item, dict)
    }
    by_path = {}
    for evidence_id, item in validity.items():
        if not isinstance(item, dict):
            continue
        is_valid = item.get("valid") is True
        is_current_failure = (
            expected_status == "FAIL"
            and item.get("status") == "FAIL"
            and isinstance(item.get("issues"), list)
            and bool(item.get("issues"))
        )
        if not is_valid and not is_current_failure:
            continue
        path = item.get("path")
        digest = item.get("sha256")
        if isinstance(path, str) and isinstance(digest, str):
            by_path.setdefault(path, []).append((evidence_id, item))
    claimed_ids = set()
    linked_requirements = set()
    linked_tests = set()
    for path_value in claimed:
        if not path_value.startswith(".harness/runs/"):
            errors.append("EVIDENCE path is outside .harness/runs: %s" % path_value)
            continue
        matches = by_path.get(path_value, [])
        if len(matches) != 1:
            errors.append("EVIDENCE is not uniquely present in current valid evidence: %s" % path_value)
            continue
        evidence_id, item = matches[0]
        try:
            path = _safe_repo_path(repo, path_value, must_exist=True)
            if path.is_symlink() or not path.is_file():
                raise HarnessError("evidence is not a safe regular file")
            actual, _size = _hash_regular_file(path, 1024 * 1024 * 1024)
            if actual != item.get("sha256"):
                raise HarnessError("fresh evidence hash differs from current verification")
        except (HarnessError, OSError) as exc:
            errors.append("EVIDENCE %s is unsafe or stale: %s" % (path_value, exc))
            continue
        artifact_type = "evidence:%s" % evidence_id
        if (
            item.get("valid") is True
            and (path_value, item.get("sha256"), artifact_type) not in artifact_index
        ):
            errors.append("EVIDENCE is not bound by the current release-report artifacts: %s" % path_value)
            continue
        claimed_ids.add(evidence_id)
        linked_requirements.update(_list_of_strings(item.get("requirement_ids")))
        linked_tests.update(_list_of_strings(item.get("test_case_ids")))
    expected_ids = set(authorization.get("expected_evidence_ids", []))
    expected_requirements = set(authorization.get("expected_requirement_ids", []))
    expected_tests = set(authorization.get("expected_test_case_ids", []))
    if not expected_ids.issubset(claimed_ids):
        errors.append("claimed EVIDENCE omits expected evidence IDs: %s" % ", ".join(sorted(expected_ids - claimed_ids)))
    if not expected_requirements.issubset(linked_requirements):
        errors.append("claimed EVIDENCE is not linked to expected requirements: %s" % ", ".join(sorted(expected_requirements - linked_requirements)))
    if not expected_tests.issubset(linked_tests):
        errors.append("claimed EVIDENCE is not linked to expected tests: %s" % ", ".join(sorted(expected_tests - linked_tests)))
    return errors


def _derive_subagent_task_status(authorization, verification, policy_errors):
    if not isinstance(authorization, dict):
        return "BLOCKED", ["active authorization is missing"]
    expected_requirements = authorization.get("expected_requirement_ids", [])
    expected_tests = authorization.get("expected_test_case_ids", [])
    expected_evidence = authorization.get("expected_evidence_ids", [])
    if not any((expected_requirements, expected_tests, expected_evidence)):
        return "BLOCKED", [
            "authorization has no expected requirement, test-case, or evidence scope"
        ]
    statuses = []
    details = []
    for identifier, outcomes, kind in (
        *[(item, verification.get("requirement_outcomes"), "requirement") for item in expected_requirements],
        *[(item, verification.get("test_outcomes"), "test case") for item in expected_tests],
    ):
        value = outcomes.get(identifier) if isinstance(outcomes, dict) else None
        if value not in STATUS_VALUES:
            statuses.append("BLOCKED")
            details.append("expected %s has no current machine outcome: %s" % (kind, identifier))
        else:
            statuses.append(value)
            if value != "PASS":
                details.append("expected %s %s is %s" % (kind, identifier, value))
    validity = verification.get("evidence_validity")
    for evidence_id in expected_evidence:
        item = validity.get(evidence_id) if isinstance(validity, dict) else None
        if not isinstance(item, dict):
            statuses.append("BLOCKED")
            details.append("expected evidence has no current validity record: %s" % evidence_id)
        elif item.get("valid") is True:
            statuses.append("PASS")
        else:
            statuses.append("FAIL")
            details.append("expected evidence is not currently valid: %s" % evidence_id)
    if policy_errors:
        statuses.append("FAIL")
        details.extend("authorization diff violation: %s" % item for item in policy_errors)
    return worst_status(statuses), details


def _effective_subagent_task_status(message, computed_status):
    values, _errors = _parse_subagent_summary(message)
    claimed = values.get("TASK_STATUS")
    if (
        computed_status in STATUS_RANK
        and claimed in STATUS_RANK
        and STATUS_RANK[claimed] > STATUS_RANK[computed_status]
    ):
        return claimed, [
            "independent subagent reported the stricter truthful status %s instead of computed %s"
            % (claimed, computed_status)
        ]
    return computed_status, []


def _subagent_summary_errors(message, expected_status, repo=None, verification=None, guard=None):
    values, errors = _parse_subagent_summary(message)
    claimed = values.get("TASK_STATUS")
    if claimed is not None and claimed not in STATUS_VALUES:
        errors.append("TASK_STATUS must be one of %s" % ", ".join(STATUS_VALUES))
    elif claimed is not None and claimed != expected_status:
        errors.append("TASK_STATUS %s does not match computed status %s" % (claimed, expected_status))
    if expected_status == "PASS":
        for name in ("FAILURES", "BLOCKERS", "NOT_RUN"):
            if name in values and not _empty_summary_marker(values[name]):
                errors.append("%s must explicitly be NONE for PASS" % name)
    elif expected_status == "FAIL" and "FAILURES" in values and _empty_summary_marker(values["FAILURES"]):
        errors.append("FAILURES must describe the executed failure")
    elif expected_status == "BLOCKED" and "BLOCKERS" in values and _empty_summary_marker(values["BLOCKERS"]):
        errors.append("BLOCKERS must describe the blocking condition")
    elif expected_status == "NOT_RUN" and "NOT_RUN" in values and _empty_summary_marker(values["NOT_RUN"]):
        errors.append("NOT_RUN must describe unexecuted scope")
    if not isinstance(guard, dict) or not isinstance(guard.get("active"), dict):
        errors.append("SubagentStop lacks a correlated active authorization snapshot")
        return errors
    active = guard["active"]
    authorization = active.get("authorization", {})
    role = active.get("role", {})
    changed = guard.get("changed", {})
    if "FILES" in values:
        try:
            claimed_files = _summary_paths(values["FILES"], "FILES")
            actual_files = sorted(changed)
            if role.get("mode") == "read_only":
                if actual_files:
                    errors.append("read-only role has repository changes")
                review_paths = authorization.get("review_paths", [])
                if expected_status == "PASS" and not claimed_files:
                    errors.append("read-only PASS must name reviewed FILES")
                for path_value in claimed_files:
                    path = _safe_repo_path(repo, path_value, must_exist=True)
                    if path.is_symlink() or not path.is_file():
                        raise HarnessError("reviewed path is not a safe regular file: %s" % path_value)
                    if not _guard_matches_any(path_value, review_paths):
                        errors.append("reviewed FILES path is outside review authorization: %s" % path_value)
            elif claimed_files != actual_files:
                errors.append(
                    "FILES must exactly equal the machine diff; claimed=%s actual=%s"
                    % (claimed_files, actual_files)
                )
            if role.get("mode") != "read_only" and expected_status == "PASS" and not actual_files:
                errors.append("write/evidence role PASS requires at least one authorized changed file")
        except (HarnessError, OSError) as exc:
            errors.append("FILES validation failed: %s" % exc)
    if repo and verification is not None and isinstance(authorization, dict):
        errors.extend(
            _subagent_evidence_errors(values, expected_status, repo, verification, authorization)
        )
    return errors


def _evaluate_subagent_stop(hook_input, verification):
    result = {
        "active": None,
        "changed": {},
        "errors": [],
        "snapshot_sha256": None,
        "task_status": "BLOCKED",
        "task_status_details": [],
    }
    wire_errors = _hook_wire_errors(hook_input, "subagentstop")
    if wire_errors:
        result["errors"].extend(wire_errors)
        return result
    try:
        repo = _hook_repository(hook_input, verification)
        directories = _guard_directories(repo, create=False)
        protected = _guard_external_directories(repo, create=False)
        policy = _load_subagent_policy(repo)
        key = _guard_key(hook_input, repo)
        lease_path = directories["root"] / "lease.json"
        protected_lease_path = protected["root"] / "lease.json"
        lease = _load_guard_lease(protected_lease_path, require_private=True)
        if lease is None or lease.get("guard_key") != key:
            raise HarnessError("SubagentStop has no matching exclusive repository lease")
        mirror_lease = _load_guard_lease(lease_path)
        if mirror_lease is None or not json_equal(lease, mirror_lease):
            raise HarnessError("repository lease mirror differs from protected state")
        active_path = directories["active"] / (key + ".json")
        protected_active_path = protected["active"] / (key + ".json")
        active = _load_active_document(protected_active_path, require_private=True)
        mirror_active = _load_active_document(active_path)
        if not json_equal(active, mirror_active):
            raise HarnessError("repository active-state mirror differs from protected state")
        if active.get("guard_key") != key or active.get("correlation") != list(_guard_tuple(hook_input, repo)):
            raise HarnessError("SubagentStop correlation does not match active state")
        if active.get("agent_type") != hook_input.get("agent_type"):
            raise HarnessError("SubagentStop agent_type differs from active state")
        authorization = active.get("authorization")
        if not isinstance(authorization, dict) or active.get("authorization_sha256") != canonical_sha256(authorization):
            raise HarnessError("active authorization hash mismatch")
        if active.get("policy_sha256") != canonical_sha256(policy):
            raise HarnessError("subagent role policy changed during the agent window")
        roles = policy.get("roles", {})
        if not json_equal(active.get("role"), roles.get(hook_input.get("agent_type"))):
            raise HarnessError("active static role differs from the current canonical policy")
        snapshot = _subagent_repository_snapshot(repo, policy)
        runtime_prefixes = _guard_accumulated_runtime_prefixes(
            active, _guard_runtime_prefixes(repo, verification)
        )
        active = _persist_guard_runtime_prefixes(
            active, protected_active_path, active_path, runtime_prefixes
        )
        evaluated_active = dict(active)
        evaluated_active["stop_snapshot"] = snapshot
        changed = _subagent_snapshot_diff(
            active.get("start_snapshot"), snapshot, runtime_prefixes=runtime_prefixes
        )
        result.update({
            "active": evaluated_active,
            "active_path": str(active_path),
            "lease_path": str(lease_path),
            "protected_active_path": str(protected_active_path),
            "protected_lease_path": str(protected_lease_path),
            "protected_receipts_dir": str(protected["receipts"]),
            "guard_key": key,
            "repo": str(repo),
            "changed": changed,
            "snapshot_sha256": snapshot.get("snapshot_sha256"),
            "runtime_prefixes": runtime_prefixes,
        })
        if active.get("start_snapshot", {}).get("git_head") != snapshot.get("git_head"):
            result["errors"].append("Git HEAD changed during the subagent window")
        policy_errors = _subagent_path_policy_errors(evaluated_active, policy, changed)
        result["errors"].extend(policy_errors)
        task_status, task_details = _derive_subagent_task_status(
            authorization, verification, policy_errors
        )
        task_status, claimed_details = _effective_subagent_task_status(
            hook_input.get("last_assistant_message") or "", task_status
        )
        task_details.extend(claimed_details)
        result["task_status"] = task_status
        result["task_status_details"] = task_details
        last_message = hook_input.get("last_assistant_message") or ""
        result["errors"].extend(
            _subagent_summary_errors(
                last_message,
                task_status,
                repo,
                verification,
                result,
            )
        )
    except (HarnessError, OSError, ValueError) as exc:
        result["errors"].append("%s: %s" % (type(exc).__name__, exc))
    result["errors"] = list(dict.fromkeys(result["errors"]))
    return result


def _guard_fsync_directory(path):
    try:
        directory_fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass


def _restore_guard_finalization_stages(staged):
    errors = []
    for source, stage in reversed(staged):
        try:
            if source.exists() or source.is_symlink():
                raise HarnessError("guard rollback target already exists: %s" % source)
            if stage.is_symlink() or not stage.is_file():
                raise HarnessError("guard rollback stage is missing or unsafe: %s" % stage)
            os.replace(str(stage), str(source))
            _guard_fsync_directory(source.parent)
        except (HarnessError, OSError) as exc:
            errors.append("%s: %s" % (type(exc).__name__, exc))
    if errors:
        raise HarnessError(
            "subagent finalization rollback is incomplete: %s" % "; ".join(errors)
        )


def _stage_guard_finalization_state(paths, transaction_id):
    staged = []
    try:
        for source in paths:
            stage = source.parent / (
                ".%s.finalize-%s" % (source.name, transaction_id)
            )
            if stage.exists() or stage.is_symlink():
                raise HarnessError("guard finalization stage already exists: %s" % stage)
            os.replace(str(source), str(stage))
            _guard_fsync_directory(source.parent)
            staged.append((source, stage))
    except BaseException as exc:
        try:
            _restore_guard_finalization_stages(staged)
        except HarnessError as rollback_exc:
            raise HarnessError(
                "subagent finalization staging failed (%s) and rollback failed (%s)"
                % (exc, rollback_exc)
            )
        raise
    return staged


def _discard_guard_finalization_stages(staged):
    # Receipts are already committed before this cleanup. A retained hidden stage
    # is a recoverable inactive remnant, never a live lease or active assignment.
    for _source, stage in staged:
        try:
            stage.unlink()
            _guard_fsync_directory(stage.parent)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _finalize_subagent_guard(hook_input, verification, guard, outcome):
    active = guard.get("active") if isinstance(guard, dict) else None
    if not isinstance(active, dict):
        return None
    repo = find_repo(guard.get("repo") or verification.get("repo") or hook_input.get("cwd"))
    directories = _guard_directories(repo, create=False)
    key = guard.get("guard_key")
    if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{64}", key) is None:
        raise HarnessError("cannot finalize an invalid subagent guard key")
    transaction_id = uuid.uuid4().hex
    receipt_name = "%s-%s.json" % (key, transaction_id)
    receipt_path = directories["receipts"] / receipt_name
    protected_receipts_dir = Path(guard.get("protected_receipts_dir"))
    protected_receipt_path = protected_receipts_dir / receipt_name
    changed = guard.get("changed", {})
    receipt = {
        "standard_version": STANDARD_VERSION,
        "kind": "subagent_guard_receipt",
        "guard_key": key,
        "authorization_id": active.get("authorization", {}).get("authorization_id"),
        "authorization_sha256": active.get("authorization_sha256"),
        "policy_sha256": active.get("policy_sha256"),
        "correlation": active.get("correlation"),
        "agent_type": active.get("agent_type"),
        "started_at": active.get("started_at"),
        "completed_at": utc_now(),
        "start_snapshot_sha256": active.get("start_snapshot", {}).get("snapshot_sha256"),
        "stop_snapshot_sha256": guard.get("snapshot_sha256"),
        "changed": changed,
        "guard_errors": guard.get("errors", []),
        "outcome": outcome,
        "task_status": guard.get("task_status"),
        "verification_run_id": verification.get("run_id"),
        "verification_status": verification.get("overall_status"),
        "summary_sha256": hashlib.sha256(
            str(hook_input.get("last_assistant_message") or "").encode("utf-8")
        ).hexdigest(),
    }
    active_path = Path(guard.get("active_path"))
    lease_path = Path(guard.get("lease_path"))
    protected_active_path = Path(guard.get("protected_active_path"))
    protected_lease_path = Path(guard.get("protected_lease_path"))
    protected_lease = _load_guard_lease(protected_lease_path, require_private=True)
    loaded_lease = _load_guard_lease(lease_path)
    if (
        protected_lease is None
        or protected_lease.get("guard_key") != key
        or not json_equal(protected_lease, loaded_lease)
    ):
        raise HarnessError("subagent lease changed before finalization")
    protected_active = _load_active_document(protected_active_path, require_private=True)
    mirror_active = _load_active_document(active_path)
    authoritative_active = dict(active)
    authoritative_active.pop("stop_snapshot", None)
    if not json_equal(protected_active, mirror_active) or not json_equal(
        protected_active, authoritative_active
    ):
        raise HarnessError("active subagent state changed before finalization")
    state_paths = [
        active_path, lease_path, protected_active_path, protected_lease_path,
    ]
    staged = _stage_guard_finalization_state(state_paths, transaction_id)
    try:
        _guard_atomic_create(receipt_path, receipt)
        _guard_atomic_create(protected_receipt_path, receipt)
    except BaseException:
        for path in (receipt_path, protected_receipt_path):
            try:
                path.unlink()
                _guard_fsync_directory(path.parent)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        _restore_guard_finalization_stages(staged)
        raise
    _discard_guard_finalization_stages(staged)
    return receipt_path.relative_to(repo).as_posix()


def _stable_hook_text(value):
    text = str(value or "")
    text = re.sub(r"(?:RUN|SELFTEST)-[A-Za-z0-9][A-Za-z0-9._-]*", "<RUN_ID>", text)
    text = re.sub(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})",
        "<TIMESTAMP>",
        text,
    )
    return text


def _stable_hook_value(value):
    if isinstance(value, dict):
        return {
            key: _stable_hook_value(item)
            for key, item in sorted(value.items())
            if key not in {
                "run_id", "run_dir", "started_at", "completed_at", "updated_at",
                "created_at", "verified_at", "timestamp",
            }
        }
    if isinstance(value, list):
        return [_stable_hook_value(item) for item in value]
    if isinstance(value, str):
        return _stable_hook_text(value)
    return value


def _hook_continuation_exhausted(verification, detail=None):
    location = verification.get("run_dir") or verification.get("run_id") or "the archived Harness run"
    return {
        "continue": False,
        "stopReason": detail or "Software Delivery Harness continuation limit reached; inspect %s." % location,
        "systemMessage": "Do not claim delivery completion. Report the archived machine status and remaining failures, blockers, or not-run scope.",
    }, 0


_TRUTHFUL_STOP_FIELDS = (
    "HARNESS_STATUS", "RELEASE_ALLOWED", "HARNESS_RUN",
    "FAILURES", "BLOCKERS", "NOT_RUN",
)


def _empty_report_marker(value):
    return str(value or "").strip().lower() in {
        "none", "n/a", "na", "[]", "-", "null",
    }


def _truthful_stop_report_errors(hook_input, verification):
    """Validate a root Stop final report against the current machine result.

    A truthful non-PASS report is an explicit terminal condition: the Hook may
    stop immediately instead of forcing a redundant repair turn.  The fixed
    labels deliberately make the decision machine-verifiable and prevent a
    prose completion claim from overriding release_allowed=false.
    """
    message = hook_input.get("last_assistant_message")
    text = message if isinstance(message, str) else ""
    values = {}
    errors = []
    for field in _TRUTHFUL_STOP_FIELDS:
        matches = re.findall(r"(?m)^%s\s*:\s*(.*?)\s*$" % field, text)
        if len(matches) != 1 or not matches[0].strip():
            errors.append("%s must appear exactly once with a nonempty value" % field)
        else:
            values[field] = matches[0].strip()

    status = verification.get("overall_status", "FAIL")
    if values.get("HARNESS_STATUS") not in (None, status):
        errors.append(
            "HARNESS_STATUS %s does not match computed status %s"
            % (values.get("HARNESS_STATUS"), status)
        )
    if values.get("RELEASE_ALLOWED") not in (None, "false"):
        errors.append("RELEASE_ALLOWED must be exactly false")
    run_value = values.get("HARNESS_RUN")
    allowed_runs = {
        "LATEST", ".harness/state/latest-run.json",
        str(verification.get("run_id") or ""),
        str(verification.get("run_dir") or ""),
    }
    if run_value is not None and run_value not in allowed_runs:
        errors.append("HARNESS_RUN must be LATEST, the latest-run pointer, or the current run")

    present_statuses = {
        item.get("status")
        for item in verification.get("issues", [])
        if isinstance(item, dict) and item.get("status") in STATUS_VALUES[1:]
    }
    if status in STATUS_VALUES[1:]:
        present_statuses.add(status)
    field_statuses = {
        "FAILURES": "FAIL", "BLOCKERS": "BLOCKED", "NOT_RUN": "NOT_RUN",
    }
    for field, issue_status in field_statuses.items():
        value = values.get(field)
        if value is None:
            continue
        if issue_status in present_statuses and _empty_report_marker(value):
            errors.append("%s must describe the current %s result" % (field, issue_status))
        if issue_status not in present_statuses and not _empty_report_marker(value):
            errors.append("%s must be NONE because no current %s result exists" % (field, issue_status))

    false_release_patterns = (
        r"(?i)release_allowed\s*[:=]\s*true",
        r"(?i)\b(?:software\s+delivery|delivery|installation|release)\s+(?:is\s+)?(?:complete|completed|ready|passed)\b",
        r"(?:软件交付|交付|安装|发布).{0,8}(?:已经|已)?(?:完成|通过|可以发布|可发布|就绪)",
    )
    if any(re.search(pattern, text) for pattern in false_release_patterns):
        errors.append("message contains a completion or release claim while release_allowed=false")
    return list(dict.fromkeys(errors))


def _truthful_stop_report_template(status):
    return (
        "HARNESS_STATUS: %s; RELEASE_ALLOWED: false; HARNESS_RUN: LATEST; "
        "FAILURES/BLOCKERS/NOT_RUN must each truthfully summarize its current status or be NONE"
        % status
    )


def _hook_fingerprint(verification):
    normalized_issues = []
    for item in verification.get("issues", []) if isinstance(verification.get("issues"), list) else []:
        if isinstance(item, dict):
            normalized_issues.append({
                "code": item.get("code"),
                "status": item.get("status"),
                "message": _stable_hook_text(item.get("message")),
                "path": item.get("path"),
                "details": _stable_hook_value(item.get("details")),
                "requirement_ids": sorted(_list_of_strings(item.get("requirement_ids"))),
                "test_case_ids": sorted(_list_of_strings(item.get("test_case_ids"))),
                "evidence_ids": sorted(_list_of_strings(item.get("evidence_ids"))),
            })
    normalized_issues.sort(key=lambda item: canonical_json_bytes(item))
    evidence = {}
    for evidence_id, item in (
        verification.get("evidence_validity", {}).items()
        if isinstance(verification.get("evidence_validity"), dict) else []
    ):
        if isinstance(item, dict):
            evidence[evidence_id] = {
                "valid": item.get("valid"), "path": item.get("path"),
                "sha256": item.get("sha256"),
                "requirement_ids": sorted(_list_of_strings(item.get("requirement_ids"))),
                "test_case_ids": sorted(_list_of_strings(item.get("test_case_ids"))),
            }
    payload = {
        "overall_status": verification.get("overall_status"),
        "release_allowed": verification.get("release_allowed"),
        "issues": normalized_issues,
        "execution_outcomes": verification.get("execution_outcomes") or {},
        "test_outcomes": verification.get("test_outcomes") or {},
        "requirement_outcomes": verification.get("requirement_outcomes") or {},
        "evidence_validity": evidence,
        "hook_guard": _stable_hook_value(verification.get("hook_guard") or {}),
    }
    return canonical_sha256(payload)


def _hook_state_transition(hook_input, verification, active):
    repo_value = verification.get("repo") or hook_input.get("cwd")
    if not isinstance(repo_value, str) or not repo_value:
        raise HarnessError("Hook state requires a repository path")
    repo = find_repo(repo_value)
    state_dir = _safe_repo_directory(repo, ".harness/state", create=True)
    state_path = _safe_repo_path(
        repo, (state_dir / "hook-state.json").relative_to(repo).as_posix(), must_exist=False
    )
    state = {"standard_version": STANDARD_VERSION, "entries": {}}
    if state_path.exists():
        loaded = strict_json_load(state_path)
        if not isinstance(loaded, dict) or not isinstance(loaded.get("entries"), dict):
            raise HarnessError("Hook state file is malformed")
        state = loaded
    session_id = str(hook_input.get("session_id") or "unknown-session")
    turn_id = str(hook_input.get("turn_id") or "unknown-turn")
    agent_id = str(hook_input.get("agent_id") or "root-agent")
    event = str(hook_input.get("_normalized_event") or "stop")
    # turn_id is intentionally excluded: an unchanged result must not restart
    # at attempt 1 merely because Codex began a new turn in the same session.
    key_parts = [session_id, agent_id, event]
    key = hashlib.sha256("\x00".join(key_parts).encode("utf-8")).hexdigest()
    fingerprint = _hook_fingerprint(verification)
    previous = state["entries"].get(key) if isinstance(state["entries"].get(key), dict) else {}
    previous_fingerprint = previous.get("fingerprint")
    previous_count = previous.get("continuations") if is_integer(previous.get("continuations")) else 0
    if not active:
        if previous_fingerprint == fingerprint and previous_count >= 1:
            count = previous_count if previous.get("turn_id") == turn_id else min(previous_count + 1, 3)
        else:
            count = 1
        action = "continue"
    elif previous_fingerprint == fingerprint and previous_count >= 1:
        count, action = previous_count, "unchanged"
    elif previous_count >= 3:
        count, action = previous_count, "exhausted"
    else:
        count, action = previous_count + 1, "continue"
    state["entries"][key] = {
        "session_id": session_id,
        "turn_id": turn_id,
        "agent_id": agent_id,
        "event": event,
        "fingerprint": fingerprint,
        "continuations": count,
        "updated_at": utc_now(),
    }
    atomic_write_json(state_path, state)
    return action, count


def _finalize_or_detail(hook_input, verification, guard, outcome, detail):
    try:
        receipt = _finalize_subagent_guard(hook_input, verification, guard, outcome)
        return detail + (" Receipt: %s." % receipt if receipt else "")
    except (HarnessError, OSError, ValueError) as exc:
        return detail + " Guard finalization failed closed: %s: %s." % (type(exc).__name__, exc)


def hook_response(hook_input, verification):
    event = hook_input.get("_normalized_event")
    active = bool(hook_input.get("stop_hook_active"))
    input_error = hook_input.get("_hook_input_error")
    if event == "pretooluse":
        if input_error:
            return _pretool_agent_block("invalid Hook input: %s" % input_error)
        return _pretool_agent_response(hook_input, verification)
    if event == "subagentstart":
        if input_error:
            return _subagent_start_block("invalid Hook input: %s" % input_error)
        return _subagent_start_response(hook_input, verification)
    if input_error:
        if active:
            return _hook_continuation_exhausted(
                verification,
                "Invalid Hook input persisted during the continuation: %s" % input_error,
            )
        return {
            "decision": "block",
            "reason": "Invalid Software Delivery Harness Hook input: %s" % input_error,
        }, 0
    if event not in {"stop", "subagentstop"}:
        return {"decision": "block", "reason": "Unsupported Software Delivery Harness Hook event."}, 0
    wire_errors = _hook_wire_errors(hook_input, event)
    if wire_errors:
        if active:
            return _hook_continuation_exhausted(
                verification, "Hook wire remained invalid: %s" % "; ".join(wire_errors)
            )
        return {"decision": "block", "reason": "Hook wire is invalid: %s" % "; ".join(wire_errors)}, 0
    status = verification.get("overall_status", "FAIL")
    guard = None
    effective_verification = verification
    if event == "subagentstop":
        guard = _evaluate_subagent_stop(hook_input, verification)
        status = guard.get("task_status", "BLOCKED")
        effective_verification = dict(verification)
        effective_verification["hook_guard"] = {
            "errors": guard.get("errors", []),
            "changed": guard.get("changed", {}),
            "snapshot_sha256": guard.get("snapshot_sha256"),
            "task_status": status,
            "task_status_details": guard.get("task_status_details", []),
            "project_status": verification.get("overall_status"),
        }
        if guard.get("errors"):
            try:
                action, count = _hook_state_transition(hook_input, effective_verification, active)
            except (HarnessError, OSError) as exc:
                detail = "Subagent guard could not persist bounded retry state: %s" % exc
                if active:
                    detail = _finalize_or_detail(
                        hook_input, effective_verification, guard, "BLOCKED", detail
                    )
                return _hook_continuation_exhausted(effective_verification, detail)
            summary = "; ".join(guard["errors"])
            if action in {"unchanged", "exhausted"}:
                detail = (
                    "Subagent authorization/summary violations are %s; stop retrying and report BLOCKED: %s"
                    % ("unchanged" if action == "unchanged" else "over the retry limit", summary)
                )
                detail = _finalize_or_detail(
                    hook_input, effective_verification, guard, "BLOCKED", detail
                )
                return _hook_continuation_exhausted(effective_verification, detail)
            return {
                "decision": "block",
                "reason": (
                    "SubagentStop authorization/summary protocol failed: %s. "
                    "Repair attempt %d of 3 must revert unauthorized changes and return all six fields."
                ) % (summary, count),
            }, 0
        if status == "PASS":
            detail = _finalize_or_detail(
                hook_input,
                effective_verification,
                guard,
                "PASS",
                "Harness verified the correlated authorization, exact file diff, and current evidence",
            )
            return {"systemMessage": detail}, 0
        detail = _finalize_or_detail(
            hook_input,
            effective_verification,
            guard,
            status,
            "Truthful independent non-PASS subagent result accepted without a redundant repair retry",
        )
        return _hook_continuation_exhausted(effective_verification, detail)
    if status == "PASS":
        return {}, 0
    if event == "stop":
        report_errors = _truthful_stop_report_errors(hook_input, effective_verification)
        if not report_errors:
            return _hook_continuation_exhausted(
                effective_verification,
                "Truthful non-PASS Harness report accepted; no redundant repair continuation is required.",
            )
    if event == "subagentstop":
        reason = (
            "Authorized subagent task scope is %s (project status is %s). See %s"
            % (
                status,
                verification.get("overall_status", "FAIL"),
                verification.get("run_dir") or verification.get("run_id"),
            )
        )
    else:
        reason = "Software Delivery Harness is %s; release_allowed=false. See %s" % (
            status,
            verification.get("run_dir") or verification.get("run_id"),
        )
    try:
        action, count = _hook_state_transition(hook_input, effective_verification, active)
    except (HarnessError, OSError) as exc:
        detail = "Harness could not persist bounded Hook continuation state: %s" % exc
        if event == "subagentstop" and active:
            detail = _finalize_or_detail(
                hook_input, effective_verification, guard, status, detail
            )
        return _hook_continuation_exhausted(effective_verification, detail)
    if action in {"unchanged", "exhausted"}:
        detail = (
            "Harness failures are unchanged after continuation; stop retrying and report the truthful %s result."
            % status
            if action == "unchanged"
            else "Harness failure changes exceeded the bounded continuation limit; report the truthful %s result."
            % status
        )
        if event == "subagentstop":
            detail = _finalize_or_detail(
                hook_input, effective_verification, guard, status, detail
            )
        return _hook_continuation_exhausted(effective_verification, detail)
    suffix = " Continue attempt %d of 3 to repair or produce a truthful failure report." % count
    if event == "stop":
        suffix += " Required terminal report: %s." % _truthful_stop_report_template(status)
    if event == "subagentstop":
        suffix += " The authorization and six-field handoff are valid, but the machine result still prevents PASS."
    return {"decision": "block", "reason": reason + "." + suffix}, 0
