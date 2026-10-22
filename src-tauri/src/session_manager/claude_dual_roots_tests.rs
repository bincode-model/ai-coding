//! Independent regressions for TC-CLAUDE-DUAL-ROOTS, TC-CLAUDE-DUAL-WATCH,
//! and TC-CLAUDE-GG-RESUME. Ordinary tests use explicit roots under TempDir;
//! they never change HOME, CLAUDE_CONFIG_DIR, CODEX_HOME, or application state.
//! The ignored real-history test requires a separate, explicit manual opt-in.

use std::collections::{BTreeSet, HashMap};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::time::{Duration, Instant, UNIX_EPOCH};

use notify::{Config, Event, EventKind, RecommendedWatcher, Watcher};
use serde_json::{json, Value};
use tempfile::TempDir;

use super::claude_roots::{self, ClaudeRoot, ClaudeSource};
use super::providers::{claude, codex};
use super::resume;
use super::watcher::{self, WatchRoot, WatchState};
use super::{session_file_stat_with_roots, SessionMessage, SessionMeta};

const ID_A: &str = "01a08128-e54f-7352-98f2-3ac0816362ae";
const ID_B: &str = "a871768c-12ab-4dce-89ef-1234567890ab";
const ID_C: &str = "b871768c-12ab-4dce-89ef-1234567890ab";
const USER_TEXT: &str = "保留原始问题 — user's text\nsecond line";
const ASSISTANT_TEXT: &str = "完整答复：中文、emoji 🧪 与 apostrophe's 都保留。";
const CONFIG_LIMIT: usize = 1024 * 1024;
// These are failure deadlines, not assumptions about filesystem delivery time.
const EVENT_DEADLINE: Duration = Duration::from_secs(10);
const READY_POLL: Duration = Duration::from_millis(50);

struct HomeFixture {
    _temp: TempDir,
    home: PathBuf,
    configured: PathBuf,
    cwd: PathBuf,
}

impl HomeFixture {
    fn new() -> Self {
        let temp = tempfile::tempdir().expect("isolated test directory");
        let home = temp.path().join("owner's home with spaces");
        let cwd = temp.path().join("project's working directory");
        fs::create_dir_all(&home).unwrap();
        fs::create_dir_all(&cwd).unwrap();
        let configured = home.join("custom Claude's profile");
        Self {
            _temp: temp,
            home,
            configured,
            cwd,
        }
    }

    fn legacy(&self) -> PathBuf {
        self.configured.join("projects")
    }

    fn gg(&self) -> PathBuf {
        self.home.join(".claude-gg-dmit-metered/profile/projects")
    }

    fn config(&self) -> PathBuf {
        self.home
            .join(".claude-gg-dmit-metered/config.private.json")
    }

    fn archive(&self, name: &str) -> PathBuf {
        self.home.join(name).join("profile/projects")
    }

    fn mirror(&self) -> PathBuf {
        self.home
            .join(".codex/vendor_imports/claude_chats/raw/projects")
    }

    fn roots(&self) -> Vec<ClaudeRoot> {
        claude_roots::resolve_for_home(&self.home, &self.configured)
    }

    fn desired(&self) -> Vec<(String, PathBuf)> {
        watcher::desired_roots_for_home(&self.home, &self.configured, &[])
    }

    fn session(&self, root: &Path, id: &str) -> PathBuf {
        let path = root.join("project's logs").join(format!("{id}.jsonl"));
        write_bytes(&path, &transcript(id, &self.cwd));
        path
    }

    fn wrapper(&self) -> PathBuf {
        let path = self.home.join(".claude-gg-dmit/bin/claude-gg");
        // This is an inert fixture. No test executes either this or real GG.
        write_bytes(&path, b"#!/bin/sh\nexit 99\n");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        }
        path
    }
}

fn write_bytes(path: &Path, bytes: &[u8]) {
    fs::create_dir_all(path.parent().expect("fixture parent")).unwrap();
    fs::write(path, bytes).unwrap();
}

fn transcript(id: &str, cwd: &Path) -> Vec<u8> {
    // Exercise metadata filtering, malformed lines, CRLF, Unicode, text blocks,
    // and Claude's tool-result role without changing any existing parser tests.
    let records = [
        json!({"type":"file-history-snapshot","snapshot":{}}),
        json!({"type":"user","sessionId":id,"cwd":cwd,"timestamp":"2026-10-01T00:00:00Z",
            "message":{"role":"user","content":USER_TEXT}}),
        json!({"type":"assistant","sessionId":id,"timestamp":"2026-10-01T00:00:01Z",
            "message":{"role":"assistant","content":[{"type":"text","text":ASSISTANT_TEXT}]}}),
        json!({"isMeta":true,"message":{"role":"user","content":"hidden metadata"}}),
        json!({"message":{"role":"assistant","content":[{"type":"tool_use","name":"Read","input":{}}]}}),
        json!({"message":{"role":"user","content":[{"type":"tool_result","content":"synthetic tool output"}]}}),
    ];
    let mut bytes = records
        .iter()
        .map(Value::to_string)
        .collect::<Vec<_>>()
        .join("\r\n")
        .into_bytes();
    bytes.extend_from_slice(b"\r\nnot valid json\r\n\r\n");
    bytes
}

fn scanned_paths(sessions: &[SessionMeta]) -> BTreeSet<PathBuf> {
    sessions
        .iter()
        .map(|meta| PathBuf::from(meta.source_path.as_deref().expect("source path")))
        .collect()
}

fn assert_fixture_messages(messages: &[SessionMessage]) {
    let actual: Vec<_> = messages
        .iter()
        .map(|message| (message.role.as_str(), message.content.as_str()))
        .collect();
    assert_eq!(
        actual,
        [
            ("user", USER_TEXT),
            ("assistant", ASSISTANT_TEXT),
            ("assistant", "[Tool: Read]"),
            ("tool", "synthetic tool output"),
        ]
    );
    assert_eq!(messages[0].ts, Some(1_790_812_800_000));
    assert_eq!(messages[1].ts, Some(1_790_812_801_000));
}

fn assert_read_stat_unchanged(source: &Path, roots: &[PathBuf]) {
    let before = fs::read(source).unwrap();
    let metadata = fs::metadata(source).unwrap();
    let messages = claude::load_messages_with_roots(source, roots).unwrap();
    assert_fixture_messages(&messages);
    let stat = session_file_stat_with_roots("claude", source, roots)
        .unwrap()
        .expect("existing source stat");
    assert_eq!(stat.size, before.len() as u64);
    assert_eq!(
        stat.mtime_ms,
        metadata
            .modified()
            .unwrap()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis() as i64
    );
    assert_eq!(fs::read(source).unwrap(), before);
    assert_eq!(
        fs::metadata(source).unwrap().modified().unwrap(),
        metadata.modified().unwrap()
    );
}

fn assert_rejected_source(source: &Path, roots: &[PathBuf]) {
    assert!(claude_roots::validate_source(source, roots).is_err());
    assert!(claude::load_messages_with_roots(source, roots).is_err());
    // The stat API deliberately returns None for absent files. Existing denied
    // files, unlike absence, must return an error and never expose a stat.
    if source.exists() {
        assert!(session_file_stat_with_roots("claude", source, roots).is_err());
    } else {
        assert!(session_file_stat_with_roots("claude", source, roots)
            .unwrap()
            .is_none());
    }
}

// REQ-CLAUDE-DUAL-ROOTS / TC-CLAUDE-DUAL-ROOTS

#[test]
fn configured_legacy_has_priority_and_excludes_other_archives_and_mirror() {
    let fixture = HomeFixture::new();
    let legacy = fixture.session(&fixture.legacy(), ID_A);
    let gg = fixture.session(&fixture.gg(), ID_A);
    fixture.session(&fixture.home.join(".claude/projects"), ID_B);
    fixture.session(
        &fixture.archive("Claude-Code-archive-20261021-120000"),
        ID_B,
    );
    fixture.session(&fixture.mirror(), ID_C);

    let roots = fixture.roots();
    assert_eq!(roots.len(), 2);
    assert_eq!(
        roots[0],
        ClaudeRoot {
            path: fixture.legacy(),
            source: ClaudeSource::Configured
        }
    );
    assert_eq!(
        roots[1],
        ClaudeRoot {
            path: fixture.gg(),
            source: ClaudeSource::CurrentGg
        }
    );
    let sessions = claude::scan_sessions_with_roots(&fixture.home, &roots);
    assert_eq!(
        sessions.len(),
        2,
        "different files with the same UUID are distinct histories"
    );
    assert!(sessions
        .iter()
        .all(|meta| meta.session_id == ID_A && meta.provider_id == "claude"));
    assert_eq!(
        scanned_paths(&sessions),
        BTreeSet::from([legacy.canonicalize().unwrap(), gg.canonicalize().unwrap()])
    );
    for source in [&legacy, &gg] {
        assert_read_stat_unchanged(source, &claude_roots::paths(&roots));
    }
}

#[test]
fn identical_uuids_in_distinct_roots_keep_their_own_message_content() {
    let fixture = HomeFixture::new();
    let legacy = fixture.session(&fixture.legacy(), ID_A);
    let gg = fixture.session(&fixture.gg(), ID_A);
    for (source, reply) in [(&legacy, "legacy-only reply"), (&gg, "GG-only reply")] {
        let mut file = OpenOptions::new().append(true).open(source).unwrap();
        writeln!(
            file,
            "{}",
            json!({"sessionId":ID_A,"message":{"role":"assistant","content":reply}})
        )
        .unwrap();
    }
    let legacy_bytes = fs::read(&legacy).unwrap();
    let gg_bytes = fs::read(&gg).unwrap();
    let roots = fixture.roots();
    let paths = claude_roots::paths(&roots);
    let sessions = claude::scan_sessions_with_roots(&fixture.home, &roots);
    assert_eq!(sessions.len(), 2);
    for (source, expected, excluded) in [
        (&legacy, "legacy-only reply", "GG-only reply"),
        (&gg, "GG-only reply", "legacy-only reply"),
    ] {
        let messages = claude::load_messages_with_roots(source, &paths).unwrap();
        assert_eq!(messages.last().unwrap().content, expected);
        assert!(!messages
            .iter()
            .any(|message| message.content.contains(excluded)));
        let stat = session_file_stat_with_roots("claude", source, &paths)
            .unwrap()
            .unwrap();
        assert_eq!(stat.size, fs::metadata(source).unwrap().len());
    }
    assert_eq!(fs::read(legacy).unwrap(), legacy_bytes);
    assert_eq!(fs::read(gg).unwrap(), gg_bytes);
}

#[test]
fn public_read_stat_and_search_isolate_legacy_and_gg_histories() {
    use super::search::{self, SessionRef, SessionSearchMode};

    const CHILD_HOME: &str = "AI_CODING_DUAL_ROOT_PUBLIC_API_TEST_CHILD_HOME";
    const CHILD_DONE: &str = "dual-root-public-api-isolated-child-complete";
    let Some(child_home) = std::env::var_os(CHILD_HOME) else {
        let dir = tempfile::Builder::new()
            .prefix("dual-root-public-api-test-")
            .tempdir()
            .unwrap();
        let home = dir.path().canonicalize().unwrap();
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "session_manager::claude_dual_roots_tests::public_read_stat_and_search_isolate_legacy_and_gg_histories",
                "--nocapture",
                "--test-threads=1",
            ])
            .env(CHILD_HOME, &home)
            .env("CC_SWITCH_TEST_HOME", &home)
            .output()
            .expect("launch isolated dual-root public API regression");
        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(output.status.success(), "{stdout}\n{stderr}");
        assert!(
            stdout.contains(CHILD_DONE),
            "child assertions did not finish: {stdout}"
        );
        return;
    };
    let home = PathBuf::from(
        std::env::var_os("CC_SWITCH_TEST_HOME").expect("explicit isolated child home"),
    );
    assert_eq!(home, PathBuf::from(child_home));
    assert!(home.is_absolute() && home.is_dir());
    assert!(home
        .file_name()
        .unwrap()
        .to_str()
        .unwrap()
        .starts_with("dual-root-public-api-test-"));
    assert_eq!(crate::config::get_home_dir(), home);
    assert_eq!(crate::config::get_claude_config_dir(), home.join(".claude"));

    const LEGACY_TEXT: &str = "霜林松影映雪峰";
    const LEGACY_FUZZY: &str = "霜林 松影 映雪峰";
    const GG_TEXT: &str = "潮汐珊瑚映蓝湾";
    const GG_FUZZY: &str = "潮汐 珊瑚 映蓝湾";
    const APPENDED_TEXT: &str = "星际灯塔跃迁航程";
    const APPENDED_FUZZY: &str = "星际 灯塔 跃迁航程";
    const OUTSIDE_TEXT: &str = "越界竹海清风拂面";
    const OUTSIDE_FUZZY: &str = "越界 竹海 清风拂面";
    let cwd = home.join("synthetic project");
    fs::create_dir_all(&cwd).unwrap();
    let legacy = home
        .join(".claude/projects/synthetic-project")
        .join(format!("{ID_A}.jsonl"));
    let gg = home
        .join(".claude-gg-dmit-metered/profile/projects/synthetic-project")
        .join(format!("{ID_A}.jsonl"));
    let outside = home.join("outside-projects").join(format!("{ID_A}.jsonl"));
    let reply_line = |reply: &str| {
        format!(
            "{}\n",
            json!({"type":"assistant","sessionId":ID_A,
                "message":{"role":"assistant","content":reply}})
        )
    };
    let make_source = |path: &Path, reply: &str| {
        let mut bytes = transcript(ID_A, &cwd);
        bytes.extend_from_slice(reply_line(reply).as_bytes());
        write_bytes(path, &bytes);
        bytes
    };
    let legacy_bytes = make_source(&legacy, LEGACY_TEXT);
    let mut gg_bytes = make_source(&gg, GG_TEXT);
    let outside_bytes = make_source(&outside, OUTSIDE_TEXT);
    let ignored_aliases = [
        legacy.parent().unwrap().join("agent-alias.jsonl"),
        gg.parent().unwrap().join("journal.jsonl"),
        home.join(".claude-gg-dmit-metered/profile/projects/subagents")
            .join(format!("{ID_A}.jsonl")),
    ];
    for (source, alias) in [
        (&legacy, &ignored_aliases[0]),
        (&gg, &ignored_aliases[1]),
        (&gg, &ignored_aliases[2]),
    ] {
        fs::create_dir_all(alias.parent().unwrap()).unwrap();
        #[cfg(unix)]
        std::os::unix::fs::symlink(source, alias).unwrap();
        // Hard links also exercise ignored input names without requiring
        // symlink privileges on non-Unix test hosts.
        #[cfg(not(unix))]
        fs::hard_link(source, alias).unwrap();
    }

    // Use discovered defaults and the same public entry points as the app;
    // supplying explicit roots here would conceal a resolver integration bug.
    let sessions = claude::scan_sessions();
    let legacy_source = legacy.canonicalize().unwrap();
    let gg_source = gg.canonicalize().unwrap();
    let expected_paths = BTreeSet::from([legacy_source.clone(), gg_source.clone()]);
    assert_eq!(sessions.len(), 2);
    assert!(sessions
        .iter()
        .all(|meta| meta.provider_id == "claude" && meta.session_id == ID_A));
    assert_eq!(scanned_paths(&sessions), expected_paths);
    let items: Vec<_> = sessions
        .iter()
        .map(|meta| SessionRef {
            provider_id: meta.provider_id.clone(),
            session_id: meta.session_id.clone(),
            source_path: meta.source_path.clone().unwrap(),
        })
        .collect();
    let assert_public_read = |source: &Path, replies: &[&str], bytes: &[u8]| {
        let source_str = source.to_str().unwrap();
        let messages = super::load_messages("claude", source_str).unwrap();
        assert_eq!(messages.len(), 4 + replies.len());
        assert_fixture_messages(&messages[..4]);
        for (message, reply) in messages[4..].iter().zip(replies) {
            assert_eq!(message.role, "assistant");
            assert_eq!(message.content, *reply);
        }
        let stat = super::session_file_stat("claude", source_str)
            .unwrap()
            .expect("public stat for the discovered source");
        assert_eq!(stat.size, bytes.len() as u64);
        assert_eq!(
            stat.mtime_ms,
            fs::metadata(source)
                .unwrap()
                .modified()
                .unwrap()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_millis() as i64
        );
        assert_eq!(fs::read(source).unwrap(), bytes);
    };
    let assert_hit = |query: &str, mode: SessionSearchMode, source: &Path, text: &str| {
        let hits = search::search_contents(&items, query, 0, mode);
        assert_eq!(hits.len(), 1, "{mode:?}: {query}");
        assert_eq!(hits[0].provider_id, "claude");
        assert_eq!(hits[0].session_id, ID_A);
        assert_eq!(Path::new(&hits[0].source_path), source);
        assert_eq!(hits[0].match_count, 1);
        assert!(hits[0].snippet.contains(text));
        for other in items
            .iter()
            .filter(|item| Path::new(&item.source_path) != source)
        {
            assert!(
                search::search_contents(std::slice::from_ref(other), query, 0, mode).is_empty()
            );
        }
    };
    assert_public_read(&legacy_source, &[LEGACY_TEXT], &legacy_bytes);
    assert_public_read(&gg_source, &[GG_TEXT], &gg_bytes);
    for (source, text, fuzzy) in [
        (&legacy_source, LEGACY_TEXT, LEGACY_FUZZY),
        (&gg_source, GG_TEXT, GG_FUZZY),
    ] {
        assert_hit(text, SessionSearchMode::Exact, source, text);
        assert!(search::search_contents(&items, fuzzy, 0, SessionSearchMode::Exact).is_empty());
        assert_hit(fuzzy, SessionSearchMode::Fuzzy, source, text);
    }
    for (mode, query) in [
        (SessionSearchMode::Exact, "保留原始问题"),
        (SessionSearchMode::Fuzzy, "保留 原始 问题"),
    ] {
        let hits = search::search_contents(&items, query, 0, mode);
        assert_eq!(hits.len(), 2, "same UUID must not merge source paths");
        assert_eq!(
            hits.iter()
                .map(|hit| PathBuf::from(&hit.source_path))
                .collect::<BTreeSet<_>>(),
            expected_paths
        );
    }
    for (mode, query) in [
        (SessionSearchMode::Exact, APPENDED_TEXT),
        (SessionSearchMode::Fuzzy, APPENDED_FUZZY),
    ] {
        assert!(search::search_contents(&items, query, 0, mode).is_empty());
    }
    assert_eq!(fs::read(&legacy).unwrap(), legacy_bytes);
    assert_eq!(fs::read(&gg).unwrap(), gg_bytes);

    // Retain the same SessionRefs and warmed cache across a synthetic append.
    // No Claude or GG executable is involved.
    let appended = reply_line(APPENDED_TEXT);
    OpenOptions::new()
        .append(true)
        .open(&gg)
        .unwrap()
        .write_all(appended.as_bytes())
        .unwrap();
    gg_bytes.extend_from_slice(appended.as_bytes());
    assert_hit(
        APPENDED_TEXT,
        SessionSearchMode::Exact,
        &gg_source,
        APPENDED_TEXT,
    );
    assert_hit(
        APPENDED_FUZZY,
        SessionSearchMode::Fuzzy,
        &gg_source,
        APPENDED_TEXT,
    );
    for (source, text, fuzzy) in [
        (&legacy_source, LEGACY_TEXT, LEGACY_FUZZY),
        (&gg_source, GG_TEXT, GG_FUZZY),
    ] {
        assert_hit(text, SessionSearchMode::Exact, source, text);
        assert_hit(fuzzy, SessionSearchMode::Fuzzy, source, text);
    }
    for (source, text, fuzzy) in [
        (&outside, OUTSIDE_TEXT, OUTSIDE_FUZZY),
        (&ignored_aliases[0], LEGACY_TEXT, LEGACY_FUZZY),
        (&ignored_aliases[1], GG_TEXT, GG_FUZZY),
        (&ignored_aliases[2], GG_TEXT, GG_FUZZY),
    ] {
        let source_str = source.to_str().unwrap();
        assert!(super::load_messages("claude", source_str).is_err());
        assert!(super::session_file_stat("claude", source_str).is_err());
        let denied = [SessionRef {
            provider_id: "claude".into(),
            session_id: ID_A.into(),
            source_path: source_str.into(),
        }];
        assert!(search::search_contents(&denied, text, 0, SessionSearchMode::Exact).is_empty());
        assert!(search::search_contents(&denied, fuzzy, 0, SessionSearchMode::Fuzzy).is_empty());
    }
    assert_eq!(scanned_paths(&claude::scan_sessions()), expected_paths);
    assert_public_read(&legacy_source, &[LEGACY_TEXT], &legacy_bytes);
    assert_public_read(&gg_source, &[GG_TEXT, APPENDED_TEXT], &gg_bytes);
    assert_eq!(fs::read(&outside).unwrap(), outside_bytes);
    println!("{CHILD_DONE}");
}

#[test]
fn latest_archive_requires_a_real_timestamp_and_projects_directory() {
    let fixture = HomeFixture::new();
    let older = fixture.archive("Claude-Code-archive-20261020-235959");
    let chosen = fixture.archive("Claude-Code-archive-20261021-090001");
    fixture.session(&older, ID_B);
    let selected = fixture.session(&chosen, ID_A);
    let gg = fixture.session(&fixture.gg(), ID_A);
    fixture.session(&fixture.mirror(), ID_C);
    for malformed in [
        "Claude-Code-archive-latest",
        "Claude-Code-archive-99999999-999999",
        "Claude-Code-archive-20261301-010101",
        "Claude-Code-archive-20260230-010101",
        "Claude-Code-archive-20261022-250000",
        "Claude-Code-archive-20261022-006100",
        "Claude-Code-archive-20261022-120000-backup",
        "Claude-Code-archive-20261022120000",
        "Claude-Code-archive-2026102-120000",
    ] {
        fixture.session(&fixture.archive(malformed), ID_C);
    }
    fs::create_dir_all(
        fixture
            .home
            .join("Claude-Code-archive-20261022-120000/profile"),
    )
    .unwrap();
    write_bytes(
        &fixture.archive("Claude-Code-archive-20261023-120000"),
        b"not a directory",
    );

    let roots = fixture.roots();
    assert_eq!(roots.len(), 2);
    assert_eq!(
        roots[0],
        ClaudeRoot {
            path: chosen,
            source: ClaudeSource::Archive
        }
    );
    assert_eq!(roots[1].source, ClaudeSource::CurrentGg);
    let sessions = claude::scan_sessions_with_roots(&fixture.home, &roots);
    assert_eq!(sessions.len(), 2);
    assert_eq!(
        scanned_paths(&sessions),
        BTreeSet::from([selected.canonicalize().unwrap(), gg.canonicalize().unwrap()])
    );
    assert!(sessions.iter().all(|meta| meta.session_id == ID_A));
    for source in [&selected, &gg] {
        assert_read_stat_unchanged(source, &claude_roots::paths(&roots));
    }
}

#[test]
fn older_mirror_is_used_only_when_no_configured_or_valid_archive_exists() {
    let fixture = HomeFixture::new();
    let mirror = fixture.session(&fixture.mirror(), ID_A);
    fixture.session(&fixture.archive("Claude-Code-archive-not-a-date"), ID_B);
    let roots = fixture.roots();
    assert_eq!(roots.len(), 2);
    assert_eq!(
        roots[0],
        ClaudeRoot {
            path: fixture.mirror(),
            source: ClaudeSource::Archive
        }
    );
    assert_eq!(roots[1].path, fixture.gg());
    assert!(!roots[1].path.exists());
    let sessions = claude::scan_sessions_with_roots(&fixture.home, &roots);
    assert_eq!(sessions.len(), 1);
    assert_eq!(
        scanned_paths(&sessions),
        BTreeSet::from([mirror.canonicalize().unwrap()])
    );
    assert_read_stat_unchanged(&mirror, &claude_roots::paths(&roots));
}

#[test]
fn missing_roots_are_logical_sources_without_filesystem_mutations() {
    let fixture = HomeFixture::new();
    let roots = fixture.roots();
    assert_eq!(roots.len(), 2);
    assert_eq!(roots[0].path, fixture.legacy());
    assert_eq!(roots[1].path, fixture.gg());
    assert!(claude::scan_sessions_with_roots(&fixture.home, &roots).is_empty());
    assert!(claude_roots::collect_session_files(&claude_roots::paths(&roots)).is_empty());
    for root in &roots {
        assert!(!root.path.exists());
        assert_rejected_source(
            &root.path.join(format!("{ID_A}.jsonl")),
            &claude_roots::paths(&roots),
        );
    }
    assert_eq!(fs::read_dir(&fixture.home).unwrap().count(), 0);
}

#[test]
fn bounded_profile_field_selects_the_current_gg_root_without_creating_it() {
    let fixture = HomeFixture::new();
    let profile = fixture.home.join("custom GG's profile with spaces");
    let config = fixture.config();
    let json = json!({"profile":profile, "unrelated":{"synthetic":"ignored field"}}).to_string();
    write_bytes(&config, json.as_bytes());
    assert_eq!(
        claude_roots::read_gg_profile(&config),
        Some(profile.clone())
    );
    let roots = fixture.roots();
    assert_eq!(roots.len(), 2);
    assert_eq!(roots[1].path, profile.join("projects"));
    assert!(!profile.exists());
    assert_eq!(fs::read(&config).unwrap(), json.as_bytes());

    // A valid document at the byte limit is accepted. One extra byte must
    // trigger the default even though the JSON itself remains valid.
    let mut exact = json.into_bytes();
    exact.resize(CONFIG_LIMIT, b' ');
    fs::write(&config, &exact).unwrap();
    assert_eq!(claude_roots::current_gg_profile(&fixture.home), profile);
    exact.push(b' ');
    fs::write(&config, &exact).unwrap();
    assert!(claude_roots::read_gg_profile(&config).is_none());
    assert_eq!(
        claude_roots::current_gg_profile(&fixture.home).join("projects"),
        fixture.gg()
    );
    assert_eq!(fs::read(&config).unwrap(), exact);
}

#[test]
fn invalid_missing_or_oversized_profile_values_use_the_default() {
    let fixture = HomeFixture::new();
    let config = fixture.config();
    assert!(claude_roots::read_gg_profile(&config).is_none());
    assert_eq!(
        claude_roots::current_gg_profile(&fixture.home).join("projects"),
        fixture.gg()
    );
    let invalid = [
        "not json".to_string(),
        "{}".to_string(),
        json!({"profile":null}).to_string(),
        json!({"profile":7}).to_string(),
        json!({"profile":[]}).to_string(),
        json!({"profile":""}).to_string(),
        json!({"profile":"relative/profile"}).to_string(),
        json!({"profile":"~/profile"}).to_string(),
        json!({"profile":"/"}).to_string(),
        json!({"profile":"/safe/../escape"}).to_string(),
        json!({"profile":"/safe/line\nprofile"}).to_string(),
        json!({"profile":"/safe/null\u{0}profile"}).to_string(),
        json!({"profile":format!("/{}", "p".repeat(4096))}).to_string(),
    ];
    for (index, bytes) in invalid.iter().enumerate() {
        write_bytes(&config, bytes.as_bytes());
        assert!(
            claude_roots::read_gg_profile(&config).is_none(),
            "invalid profile case {index}"
        );
        assert_eq!(
            claude_roots::current_gg_profile(&fixture.home).join("projects"),
            fixture.gg()
        );
        assert_eq!(fs::read(&config).unwrap(), bytes.as_bytes());
    }
    fs::remove_file(&config).unwrap();
    fs::create_dir(&config).unwrap();
    assert!(claude_roots::read_gg_profile(&config).is_none());
}

#[cfg(unix)]
#[test]
fn private_config_symlinks_are_not_followed() {
    use std::os::unix::fs::symlink;
    let fixture = HomeFixture::new();
    let target = fixture.home.join("unrelated.json");
    let profile = fixture.home.join("other-profile");
    write_bytes(&target, json!({"profile":profile}).to_string().as_bytes());
    fs::create_dir_all(fixture.config().parent().unwrap()).unwrap();
    symlink(&target, fixture.config()).unwrap();
    assert!(claude_roots::read_gg_profile(&fixture.config()).is_none());
    assert_eq!(
        claude_roots::current_gg_profile(&fixture.home).join("projects"),
        fixture.gg()
    );
}

#[test]
fn scan_read_and_stat_filter_subagents_journals_and_non_jsonl_files() {
    let fixture = HomeFixture::new();
    let main = fixture.session(&fixture.legacy(), ID_A);
    let gg = fixture.session(&fixture.gg(), ID_A);
    let mut excluded = Vec::new();
    for root in [fixture.legacy(), fixture.gg()] {
        for name in [
            "project/subagents/nested/main.jsonl",
            "project/tool-results/result.jsonl",
            "project/workflows/run/main.jsonl",
            "project/agent-child.jsonl",
            "project/journal.jsonl",
            "project/notes.txt",
            "project/uppercase.JSONL",
        ] {
            let path = root.join(name);
            write_bytes(&path, &transcript(ID_B, &fixture.cwd));
            excluded.push(path);
        }
    }
    let outside = fixture.session(&fixture.configured.join("projects-sibling"), ID_C);
    excluded.push(outside);
    let roots = fixture.roots();
    let paths = claude_roots::paths(&roots);
    let sessions = claude::scan_sessions_with_roots(&fixture.home, &roots);
    assert_eq!(
        scanned_paths(&sessions),
        BTreeSet::from([main.canonicalize().unwrap(), gg.canonicalize().unwrap()])
    );
    for path in excluded {
        let bytes = fs::read(&path).unwrap();
        assert_rejected_source(&path, &paths);
        assert_eq!(fs::read(&path).unwrap(), bytes);
    }
    assert_rejected_source(&fixture.legacy(), &paths);
    assert_read_stat_unchanged(&main, &paths);
    assert_read_stat_unchanged(&gg, &paths);
}

#[cfg(unix)]
#[test]
fn canonical_root_and_file_aliases_deduplicate_without_losing_distinct_uuid_files() {
    use std::os::unix::fs::symlink;
    let fixture = HomeFixture::new();
    let gg = fixture.session(&fixture.gg(), ID_A);
    let second = fixture
        .gg()
        .join("another-project")
        .join(format!("{ID_A}.jsonl"));
    write_bytes(&second, &transcript(ID_A, &fixture.cwd));
    fs::create_dir_all(&fixture.configured).unwrap();
    symlink(fixture.gg(), fixture.legacy()).unwrap();
    symlink(&gg, fixture.gg().join("file-alias.jsonl")).unwrap();
    symlink(gg.parent().unwrap(), fixture.gg().join("directory-alias")).unwrap();
    fixture.wrapper();

    let roots = fixture.roots();
    assert_eq!(
        roots.len(),
        1,
        "physical aliases collapse to the safe GG source"
    );
    assert_eq!(roots[0].source, ClaudeSource::CurrentGg);
    let sessions = claude::scan_sessions_with_roots(&fixture.home, &roots);
    assert_eq!(sessions.len(), 2);
    assert!(sessions.iter().all(|meta| meta.session_id == ID_A));
    assert_eq!(
        scanned_paths(&sessions),
        BTreeSet::from([gg.canonicalize().unwrap(), second.canonicalize().unwrap()])
    );
    assert_read_stat_unchanged(
        &fixture.gg().join("file-alias.jsonl"),
        &claude_roots::paths(&roots),
    );
    let prepared =
        resume::prepare_claude_for_home(&fixture.home, &fixture.configured, ID_A, &gg).unwrap();
    assert!(prepared.command.contains("claude-gg"));
    assert!(!prepared.command.contains("CLAUDE_CONFIG_DIR"));
}

#[cfg(unix)]
#[test]
fn symlink_escapes_loops_and_links_into_ignored_targets_are_rejected() {
    use std::os::unix::fs::symlink;
    let fixture = HomeFixture::new();
    let main = fixture.session(&fixture.legacy(), ID_A);
    let outside = fixture.session(&fixture.home.join("outside"), ID_B);
    let ignored = fixture.legacy().join("project/subagents/child.jsonl");
    write_bytes(&ignored, &transcript(ID_B, &fixture.cwd));
    let root = fixture.legacy();
    symlink(&outside, root.join("escape.jsonl")).unwrap();
    symlink(outside.parent().unwrap(), root.join("escape-directory")).unwrap();
    symlink(&ignored, root.join("masked-subagent.jsonl")).unwrap();
    symlink(&root, root.join("loop-to-root")).unwrap();
    symlink("cycle-b", root.join("cycle-a")).unwrap();
    symlink("cycle-a", root.join("cycle-b")).unwrap();
    symlink(root.join("missing-target"), root.join("broken.jsonl")).unwrap();
    symlink(&main, root.join("valid-internal.jsonl")).unwrap();
    let roots = fixture.roots();
    let paths = claude_roots::paths(&roots);
    assert_eq!(
        scanned_paths(&claude::scan_sessions_with_roots(&fixture.home, &roots)),
        BTreeSet::from([main.canonicalize().unwrap()])
    );
    for path in [
        root.join("escape.jsonl"),
        root.join("escape-directory")
            .join(outside.file_name().unwrap()),
        root.join("masked-subagent.jsonl"),
        root.join("cycle-a/record.jsonl"),
        root.join("broken.jsonl"),
    ] {
        assert_rejected_source(&path, &paths);
    }
    assert_read_stat_unchanged(&root.join("valid-internal.jsonl"), &paths);
    assert_eq!(fs::read(&outside).unwrap(), transcript(ID_B, &fixture.cwd));
    assert!(claude_roots::canonicalize_with_missing(&root.join("broken.jsonl")).is_none());
    assert!(claude_roots::canonicalize_with_missing(&root.join("cycle-a/record.jsonl")).is_none());
    assert!(
        claude_roots::canonicalize_with_missing(&root.join("../outside/record.jsonl")).is_none()
    );
    assert_eq!(
        claude_roots::canonicalize_with_missing(&root.join("new-project/new.jsonl")),
        Some(root.canonicalize().unwrap().join("new-project/new.jsonl"))
    );
}

#[cfg(unix)]
#[test]
fn ignored_alias_names_do_not_bypass_read_and_stat_filters() {
    use std::os::unix::fs::symlink;
    let fixture = HomeFixture::new();
    let main = fixture.session(&fixture.legacy(), ID_A);
    let root = fixture.legacy();
    symlink(&main, root.join("agent-alias.jsonl")).unwrap();
    symlink(&main, root.join("journal.jsonl")).unwrap();
    symlink(main.parent().unwrap(), root.join("subagents")).unwrap();
    let roots = fixture.roots();
    let paths = claude_roots::paths(&roots);
    assert_eq!(
        scanned_paths(&claude::scan_sessions_with_roots(&fixture.home, &roots)),
        BTreeSet::from([main.canonicalize().unwrap()])
    );
    // A valid internal link is allowed above. These names explicitly denote
    // excluded records, so canonicalization must not erase their exclusion.
    for alias in [
        root.join("agent-alias.jsonl"),
        root.join("journal.jsonl"),
        root.join("subagents").join(main.file_name().unwrap()),
    ] {
        assert_rejected_source(&alias, &paths);
    }
}

// REQ-CLAUDE-GG-RESUME / TC-CLAUDE-GG-RESUME

fn shell_literal(value: &str, windows: bool) -> String {
    if windows {
        format!("'{}'", value.replace('\'', "''"))
    } else {
        format!("'{}'", value.replace('\'', "'\\''"))
    }
}

fn expected_gg_command(wrapper: &Path, id: &str) -> String {
    format!(
        "{}{} --resume {}",
        if cfg!(windows) { "& " } else { "" },
        shell_literal(&wrapper.to_string_lossy(), cfg!(windows)),
        shell_literal(id, cfg!(windows))
    )
}

#[cfg(unix)]
fn assert_shell_arguments(command: &str, expected: &[&str]) {
    // Parse a synthetic command into argv with shell builtins only. Do not
    // invoke the wrapper, a Claude binary, or any command from real history.
    let output = std::process::Command::new("/bin/sh")
        .arg("-c")
        .arg(format!("set -- {command}; printf '%s\\000' \"$@\""))
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "synthetic command must be valid shell syntax"
    );
    let actual: Vec<_> = output
        .stdout
        .split(|byte| *byte == 0)
        .filter(|word| !word.is_empty())
        .map(|word| std::str::from_utf8(word).unwrap())
        .collect();
    assert_eq!(actual, expected);
}

#[test]
fn gg_resume_and_metadata_use_only_existing_wrapper_with_the_selected_uuid() {
    let fixture = HomeFixture::new();
    let profile = fixture.home.join("current GG's profile");
    write_bytes(
        &fixture.config(),
        json!({"profile":profile}).to_string().as_bytes(),
    );
    let source = fixture.session(&profile.join("projects"), ID_A);
    let wrapper = fixture.wrapper();
    let wrapper_bytes = fs::read(&wrapper).unwrap();
    let config_bytes = fs::read(fixture.config()).unwrap();
    let source_bytes = fs::read(&source).unwrap();

    let prepared =
        resume::prepare_claude_for_home(&fixture.home, &fixture.configured, ID_A, &source).unwrap();
    assert_eq!(prepared.command, expected_gg_command(&wrapper, ID_A));
    assert_eq!(prepared.cwd.as_deref(), fixture.cwd.to_str());
    for forbidden in [
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "ANTHROPIC_",
        "--continue",
        "claude --resume",
        "env ",
    ] {
        assert!(
            !prepared.command.contains(forbidden),
            "GG must choose its own configuration"
        );
    }
    #[cfg(unix)]
    assert_shell_arguments(
        &prepared.command,
        &[wrapper.to_str().unwrap(), "--resume", ID_A],
    );

    let sessions = claude::scan_sessions_with_roots(&fixture.home, &fixture.roots());
    assert_eq!(sessions.len(), 1);
    assert_eq!(
        sessions[0].resume_command.as_deref(),
        Some(prepared.command.as_str())
    );
    assert_eq!(fs::read(&wrapper).unwrap(), wrapper_bytes);
    assert_eq!(fs::read(fixture.config()).unwrap(), config_bytes);
    assert_eq!(fs::read(&source).unwrap(), source_bytes);
}

#[test]
fn gg_command_quotes_apostrophes_and_uuid_for_posix_and_powershell() {
    let wrapper = Path::new("/isolated/owner's home/bin/claude-gg");
    let posix = resume::build_gg_command(wrapper, ID_A, false);
    assert_eq!(posix, "'/isolated/owner'\\''s home/bin/claude-gg' --resume '01a08128-e54f-7352-98f2-3ac0816362ae'");
    let windows = resume::build_gg_command(wrapper, ID_A, true);
    assert_eq!(
        windows,
        "& '/isolated/owner''s home/bin/claude-gg' --resume '01a08128-e54f-7352-98f2-3ac0816362ae'"
    );
    #[cfg(unix)]
    assert_shell_arguments(&posix, &[wrapper.to_str().unwrap(), "--resume", ID_A]);
}

#[test]
fn missing_or_non_executable_wrapper_never_falls_back_to_direct_claude() {
    let fixture = HomeFixture::new();
    let source = fixture.session(&fixture.gg(), ID_A);
    let check_denied = || {
        let error =
            resume::prepare_claude_for_home(&fixture.home, &fixture.configured, ID_A, &source)
                .unwrap_err();
        assert!(error.contains("包装入口") && error.contains("仍可查看"));
        let sessions = claude::scan_sessions_with_roots(&fixture.home, &fixture.roots());
        assert_eq!(sessions.len(), 1);
        assert!(sessions[0].resume_command.is_none());
    };
    check_denied();
    let wrapper = fixture.home.join(".claude-gg-dmit/bin/claude-gg");
    fs::create_dir_all(&wrapper).unwrap();
    check_denied();
    fs::remove_dir(&wrapper).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        write_bytes(&wrapper, b"#!/bin/sh\nexit 99\n");
        fs::set_permissions(&wrapper, fs::Permissions::from_mode(0o600)).unwrap();
        check_denied();
    }
}

#[test]
fn gg_resume_rejects_invalid_uuid_mismatched_source_missing_source_and_missing_cwd() {
    let fixture = HomeFixture::new();
    let source = fixture.session(&fixture.gg(), ID_A);
    fixture.wrapper();
    for invalid in [
        "",
        "--continue",
        "--dangerously-skip-permissions",
        "id'; echo unsafe",
        "not-a-uuid",
    ] {
        let error =
            resume::prepare_claude_for_home(&fixture.home, &fixture.configured, invalid, &source)
                .unwrap_err();
        assert!(error.contains("UUID"));
    }
    let error = resume::prepare_claude_for_home(&fixture.home, &fixture.configured, ID_B, &source)
        .unwrap_err();
    assert!(error.contains("不一致"));
    let outside = fixture.session(&fixture.home.join("outside"), ID_A);
    assert!(
        resume::prepare_claude_for_home(&fixture.home, &fixture.configured, ID_A, &outside)
            .is_err()
    );
    assert!(resume::prepare_claude_for_home(
        &fixture.home,
        &fixture.configured,
        ID_A,
        &fixture.gg().join("missing.jsonl")
    )
    .is_err());
    assert!(
        resume::prepare_with_roots("claude", ID_A, &source, &[fixture.gg()]).is_err(),
        "generic provider helper cannot bypass source-aware GG routing"
    );
    assert!(resume::prepare_claude_with_roots(
        &fixture.home,
        ID_A,
        &source,
        &[ClaudeRoot {
            path: fixture.legacy(),
            source: ClaudeSource::Configured
        }]
    )
    .is_err());
    let no_cwd = fixture.gg().join("missing-cwd.jsonl");
    write_bytes(
        &no_cwd,
        &transcript(ID_B, &fixture.home.join("missing workspace")),
    );
    let error = resume::prepare_claude_for_home(&fixture.home, &fixture.configured, ID_B, &no_cwd)
        .unwrap_err();
    assert!(error.contains("工作目录已不存在"));
    assert_read_stat_unchanged(&source, &claude_roots::paths(&fixture.roots()));
}

#[test]
fn invalid_uuid_in_saved_metadata_never_becomes_an_executable_preview() {
    let fixture = HomeFixture::new();
    let source = fixture.gg().join(format!("{ID_A}.jsonl"));
    write_bytes(&source, &transcript("id'; echo unsafe", &fixture.cwd));
    fixture.wrapper();
    let sessions = claude::scan_sessions_with_roots(&fixture.home, &fixture.roots());
    assert_eq!(
        sessions.len(),
        1,
        "unsafe resume metadata does not hide readable history"
    );
    assert!(sessions[0].resume_command.is_none());
    assert!(
        resume::prepare_claude_for_home(&fixture.home, &fixture.configured, ID_A, &source).is_err()
    );
    assert_read_stat_unchanged(&source, &claude_roots::paths(&fixture.roots()));
}

#[cfg(unix)]
#[test]
fn gg_alias_cannot_turn_an_archived_file_into_a_current_gg_resume() {
    use std::os::unix::fs::symlink;
    let fixture = HomeFixture::new();
    let old = fixture.session(
        &fixture.archive("Claude-Code-archive-20261020-100000"),
        ID_A,
    );
    fs::create_dir_all(fixture.gg()).unwrap();
    let alias = fixture.gg().join("looks-current.jsonl");
    symlink(&old, &alias).unwrap();
    fixture.wrapper();
    let roots = fixture.roots();
    let sessions = claude::scan_sessions_with_roots(&fixture.home, &roots);
    assert_eq!(sessions.len(), 1);
    assert!(sessions[0].resume_command.is_none());
    // Both provider roots permit viewing the original archive, but its
    // canonical source still determines which resume route is forbidden.
    assert_read_stat_unchanged(&alias, &claude_roots::paths(&roots));
    let error = resume::prepare_claude_for_home(&fixture.home, &fixture.configured, ID_A, &alias)
        .unwrap_err();
    assert!(error.contains("旧 Claude 历史归档"));
}

#[test]
fn old_archives_are_readable_but_cannot_resume_into_current_gg() {
    let fixture = HomeFixture::new();
    let old = fixture.session(
        &fixture.archive("Claude-Code-archive-20261020-100000"),
        ID_A,
    );
    let gg = fixture.session(&fixture.gg(), ID_A);
    fixture.wrapper();
    let original_old = fs::read(&old).unwrap();
    let roots = fixture.roots();
    let error = resume::prepare_claude_for_home(&fixture.home, &fixture.configured, ID_A, &old)
        .unwrap_err();
    assert!(error.contains("旧 Claude 历史归档") && error.contains("仍可查看"));
    assert!(resume::prepare_claude_for_home(&fixture.home, &fixture.configured, ID_A, &gg).is_ok());
    let sessions = claude::scan_sessions_with_roots(&fixture.home, &roots);
    assert_eq!(sessions.len(), 2);
    let old_canonical = old.canonicalize().unwrap();
    let archived = sessions
        .iter()
        .find(|meta| Path::new(meta.source_path.as_deref().unwrap()) == old_canonical.as_path())
        .unwrap();
    assert!(archived.resume_command.is_none());
    assert_read_stat_unchanged(&old, &claude_roots::paths(&roots));
    assert_eq!(fs::read(&old).unwrap(), original_old);
    assert!(
        !fixture.configured.exists(),
        "reading or preparing history must not copy an archive into a profile"
    );

    let legacy = fixture.session(&fixture.legacy(), ID_A);
    let error = resume::prepare_claude_for_home(&fixture.home, &fixture.configured, ID_A, &legacy)
        .unwrap_err();
    assert!(error.contains("不在当前 Claude GG") && error.contains("仍可查看"));
    let legacy_meta = claude::scan_sessions_with_roots(&fixture.home, &fixture.roots())
        .into_iter()
        .find(|meta| meta.source_path.as_deref() == legacy.canonicalize().unwrap().to_str())
        .unwrap();
    assert!(legacy_meta.resume_command.is_none());
}

#[test]
fn nested_gg_root_takes_precedence_over_configured_legacy_for_resume() {
    let fixture = HomeFixture::new();
    let nested_profile = fixture.legacy().join("nested GG profile");
    write_bytes(
        &fixture.config(),
        json!({"profile":nested_profile}).to_string().as_bytes(),
    );
    let source = fixture.session(&nested_profile.join("projects"), ID_A);
    let wrapper = fixture.wrapper();
    let roots = fixture.roots();
    assert_eq!(roots.len(), 2);
    assert_eq!(
        claude::scan_sessions_with_roots(&fixture.home, &roots).len(),
        1
    );
    let prepared = resume::prepare_claude_with_roots(&fixture.home, ID_A, &source, &roots).unwrap();
    assert_eq!(prepared.command, expected_gg_command(&wrapper, ID_A));
}

#[test]
fn non_gg_configured_claude_and_codex_keep_their_original_resume_behavior() {
    let fixture = HomeFixture::new();
    let legacy = fixture.session(&fixture.legacy(), ID_A);
    assert!(!claude_roots::gg_installed(&fixture.home));
    let prepared =
        resume::prepare_claude_for_home(&fixture.home, &fixture.configured, ID_A, &legacy).unwrap();
    let quoted_home = shell_literal(&fixture.configured.to_string_lossy(), cfg!(windows));
    let expected = if cfg!(windows) {
        format!("$env:CLAUDE_CONFIG_DIR={quoted_home}; claude --resume '{ID_A}'")
    } else {
        format!("env CLAUDE_CONFIG_DIR={quoted_home} claude --resume '{ID_A}'")
    };
    assert_eq!(prepared.command, expected);
    assert_eq!(prepared.cwd.as_deref(), fixture.cwd.to_str());
    let sessions = claude::scan_sessions_with_roots(&fixture.home, &fixture.roots());
    assert_eq!(
        sessions[0].resume_command.as_deref(),
        Some(expected.as_str())
    );
    #[cfg(unix)]
    assert_shell_arguments(
        &prepared.command,
        &[
            "env",
            &format!("CLAUDE_CONFIG_DIR={}", fixture.configured.display()),
            "claude",
            "--resume",
            ID_A,
        ],
    );

    // Provider and flag names inside the quoted home are ordinary path data.
    let codex_home = fixture.home.join("Codex's claude --last custom home");
    let quoted_codex_home = shell_literal(&codex_home.to_string_lossy(), cfg!(windows));
    let quoted_id = shell_literal(ID_B, cfg!(windows));
    let expected_codex = if cfg!(windows) {
        format!(
            "$env:CODEX_HOME={quoted_codex_home}; codex resume -c tui.terminal_resize_reflow_max_rows=0 --no-alt-screen -- {quoted_id}"
        )
    } else {
        format!(
            "env CODEX_HOME={quoted_codex_home} codex resume -c tui.terminal_resize_reflow_max_rows=0 --no-alt-screen -- {quoted_id}"
        )
    };
    for name in ["sessions", "archived_sessions"] {
        let root = codex_home.join(name);
        let source = root.join(format!("rollout-{ID_B}.jsonl"));
        let bytes = format!(
            "{}\n{}\n",
            json!({"type":"session_meta","payload":{"id":ID_B,"source":"cli","cwd":fixture.cwd}}),
            json!({"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Codex preserved reply"}]}})
        );
        write_bytes(&source, bytes.as_bytes());
        let prepared =
            resume::prepare_with_roots("codex", ID_B, &source, std::slice::from_ref(&root))
                .unwrap();
        assert_eq!(prepared.cwd.as_deref(), fixture.cwd.to_str());
        assert_eq!(prepared.command, expected_codex);
        #[cfg(unix)]
        assert_shell_arguments(
            &prepared.command,
            &[
                "env",
                &format!("CODEX_HOME={}", codex_home.display()),
                "codex",
                "resume",
                "-c",
                "tui.terminal_resize_reflow_max_rows=0",
                "--no-alt-screen",
                "--",
                ID_B,
            ],
        );
        assert_eq!(
            codex::load_messages(&source).unwrap()[0].content,
            "Codex preserved reply"
        );
        assert_eq!(
            session_file_stat_with_roots("codex", &source, std::slice::from_ref(&root))
                .unwrap()
                .unwrap()
                .size,
            bytes.len() as u64
        );
        assert!(
            resume::prepare_with_roots("codex", ID_A, &source, std::slice::from_ref(&root))
                .is_err()
        );
        assert!(resume::prepare_with_roots("codex", ID_B, &source, &[fixture.gg()]).is_err());
        assert_eq!(fs::read(&source).unwrap(), bytes.as_bytes());
    }
}

// REQ-CLAUDE-DUAL-WATCH / TC-CLAUDE-DUAL-WATCH. These exercise the real native
// watcher, not a mock; returned changed roots are the list-invalidation signal.
// The Tauri emitter/browser consumer remains independent runtime acceptance.

fn native_watcher() -> (RecommendedWatcher, Receiver<notify::Result<Event>>) {
    let (sender, receiver) = mpsc::channel();
    let watcher = RecommendedWatcher::new(
        move |event| {
            let _ = sender.send(event);
        },
        Config::default().with_follow_symlinks(false),
    )
    .expect("native RecommendedWatcher");
    (watcher, receiver)
}

fn is_source_event(event: &Event, source: &Path, roots: &[WatchRoot]) -> bool {
    if !matches!(
        event.kind,
        EventKind::Create(_) | EventKind::Remove(_) | EventKind::Modify(_) | EventKind::Any
    ) {
        return false;
    }
    if matches!(
        event.kind,
        EventKind::Modify(notify::event::ModifyKind::Metadata(_))
    ) {
        return false;
    }
    let expected = claude_roots::canonicalize_with_missing(source).unwrap();
    event.paths.iter().any(|path| {
        claude_roots::canonicalize_with_missing(path).as_ref() == Some(&expected)
            && watcher::classify_event_path(path, roots).as_deref() == Some("claude")
    })
}

fn receive_source_event(
    receiver: &Receiver<notify::Result<Event>>,
    source: &Path,
    state: &WatchState,
) {
    let deadline = Instant::now() + EVENT_DEADLINE;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        assert!(
            !remaining.is_zero(),
            "native watcher did not deliver the session event before the deadline"
        );
        match receiver.recv_timeout(remaining) {
            Ok(Ok(event)) if is_source_event(&event, source, &state.roots) => return,
            Ok(Ok(_)) => {}
            Ok(Err(error)) => panic!("native watcher failed on isolated fixtures: {error}"),
            Err(RecvTimeoutError::Timeout) => panic!("native watcher session event timed out"),
            Err(RecvTimeoutError::Disconnected) => panic!("native watcher channel disconnected"),
        }
    }
}

fn prime_native_watch(
    receiver: &Receiver<notify::Result<Event>>,
    root: &Path,
    state: &WatchState,
    tag: &str,
) {
    // Some native backends start their stream asynchronously. Establish
    // readiness with a temporary sentinel and bounded recv_timeout polling,
    // rather than relying on a fixed sleep or accepting a fabricated event.
    let probe = root.join(format!("native-ready-{tag}.jsonl"));
    let deadline = Instant::now() + EVENT_DEADLINE;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        assert!(!remaining.is_zero(), "native watch did not become ready");
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&probe)
            .unwrap();
        writeln!(file).unwrap();
        file.flush().unwrap();
        drop(file);
        match receiver.recv_timeout(remaining.min(READY_POLL)) {
            Ok(Ok(event)) if is_source_event(&event, &probe, &state.roots) => {
                fs::remove_file(&probe).unwrap();
                return;
            }
            Ok(Ok(_)) | Err(RecvTimeoutError::Timeout) => {}
            Ok(Err(error)) => panic!("native watcher readiness failed: {error}"),
            Err(RecvTimeoutError::Disconnected) => panic!("native watcher channel disconnected"),
        }
    }
}

fn changed_root(changed: &[WatchRoot], path: &Path) -> bool {
    changed
        .iter()
        .any(|root| root.provider_id == "claude" && root.path == path)
}

#[test]
fn watcher_desired_roots_match_discovery_and_preserve_both_codex_roots() {
    let fixture = HomeFixture::new();
    fixture.session(
        &fixture.archive("Claude-Code-archive-20261020-100000"),
        ID_A,
    );
    let profile = fixture.home.join("GG elsewhere");
    write_bytes(
        &fixture.config(),
        json!({"profile":profile}).to_string().as_bytes(),
    );
    let codex_roots = [
        fixture.home.join(".codex/sessions"),
        fixture.home.join(".codex/archived_sessions"),
    ];
    let desired = watcher::desired_roots_for_home(&fixture.home, &fixture.configured, &codex_roots);
    assert_eq!(desired.len(), 4);
    assert_eq!(
        desired
            .iter()
            .filter(|(provider, _)| provider == "claude")
            .map(|(_, path)| path.clone())
            .collect::<Vec<_>>(),
        claude_roots::paths(&fixture.roots())
    );
    assert_eq!(
        desired
            .iter()
            .filter(|(provider, _)| provider == "codex")
            .map(|(_, path)| path.clone())
            .collect::<Vec<_>>(),
        codex_roots
    );
}

#[test]
fn native_watcher_attaches_missing_gg_after_121_cycles_and_invalidates_existing_sessions() {
    let fixture = HomeFixture::new();
    let legacy = fixture.session(&fixture.legacy(), ID_A);
    let desired = fixture.desired();
    let (mut native, receiver) = native_watcher();
    let mut state = WatchState::default();
    let (remaining, changed) = watcher::sync_watch_roots(&mut native, &mut state, &desired);
    assert_eq!(remaining, 1);
    assert!(changed_root(&changed, &fixture.legacy()));
    for _ in 0..121 {
        let (remaining, changed) = watcher::sync_watch_roots(&mut native, &mut state, &desired);
        assert_eq!(remaining, 1);
        assert!(changed.is_empty());
        assert_eq!(state.roots.len(), 1);
        assert!(
            !fixture.gg().exists(),
            "reconciliation must not create provider directories"
        );
    }
    // A session written before watch attachment needs a root invalidation even
    // when no OS event for that earlier write can be received.
    let preexisting = fixture.session(&fixture.gg(), ID_B);
    let (remaining, changed) = watcher::sync_watch_roots(&mut native, &mut state, &desired);
    assert_eq!(remaining, 0);
    assert_eq!(state.roots.len(), 2);
    assert!(
        changed_root(&changed, &fixture.gg()),
        "attachment must signal list invalidation"
    );
    let sessions = claude::scan_sessions_with_roots(&fixture.home, &fixture.roots());
    assert_eq!(
        scanned_paths(&sessions),
        BTreeSet::from([
            legacy.canonicalize().unwrap(),
            preexisting.canonicalize().unwrap()
        ])
    );
    let (remaining, changed) = watcher::sync_watch_roots(&mut native, &mut state, &desired);
    assert_eq!(remaining, 0);
    assert!(
        changed.is_empty(),
        "unchanged roots must not constantly invalidate lists"
    );

    prime_native_watch(&receiver, &fixture.legacy(), &state, "legacy");
    let mut file = OpenOptions::new().append(true).open(&legacy).unwrap();
    writeln!(
        file,
        "{}",
        json!({"message":{"role":"assistant","content":"new legacy reply"}})
    )
    .unwrap();
    file.flush().unwrap();
    drop(file);
    receive_source_event(&receiver, &legacy, &state);
    prime_native_watch(&receiver, &fixture.gg(), &state, "gg");
    let new_gg = fixture.session(&fixture.gg(), ID_C);
    receive_source_event(&receiver, &new_gg, &state);
}

#[test]
fn native_watcher_reattaches_replaced_and_later_recreated_gg_root() {
    let fixture = HomeFixture::new();
    fixture.session(&fixture.legacy(), ID_A);
    fixture.session(&fixture.gg(), ID_A);
    let desired = fixture.desired();
    let (mut native, receiver) = native_watcher();
    let mut state = WatchState::default();
    assert_eq!(
        watcher::sync_watch_roots(&mut native, &mut state, &desired).0,
        0
    );
    prime_native_watch(&receiver, &fixture.gg(), &state, "original");

    // Keep the renamed inode alive, so replacement cannot accidentally reuse
    // it. Path equality alone is insufficient to retain the old watch.
    let retired = fixture.home.join("retired-projects");
    fs::rename(fixture.gg(), &retired).unwrap();
    let replacement = fixture.session(&fixture.gg(), ID_B);
    let (remaining, changed) = watcher::sync_watch_roots(&mut native, &mut state, &desired);
    assert_eq!(remaining, 0);
    assert_eq!(state.roots.len(), 2);
    assert!(changed_root(&changed, &fixture.gg()));
    assert!(watcher::classify_event_path(&retired.join("old.jsonl"), &state.roots).is_none());
    assert_read_stat_unchanged(&replacement, &claude_roots::paths(&fixture.roots()));
    prime_native_watch(&receiver, &fixture.gg(), &state, "replacement");
    let after_replacement = fixture.session(&fixture.gg(), ID_C);
    receive_source_event(&receiver, &after_replacement, &state);

    fs::rename(fixture.gg(), fixture.home.join("retired-again")).unwrap();
    let (remaining, changed) = watcher::sync_watch_roots(&mut native, &mut state, &desired);
    assert_eq!(remaining, 1);
    assert_eq!(state.roots.len(), 1);
    assert!(changed_root(&changed, &fixture.gg()));
    fixture.session(&fixture.gg(), ID_A);
    let (remaining, changed) = watcher::sync_watch_roots(&mut native, &mut state, &desired);
    assert_eq!(remaining, 0);
    assert!(changed_root(&changed, &fixture.gg()));
    prime_native_watch(&receiver, &fixture.gg(), &state, "recreated");
    let after_recreation = fixture.session(&fixture.gg(), ID_B);
    receive_source_event(&receiver, &after_recreation, &state);
}

#[cfg(unix)]
#[test]
fn native_watcher_deduplicates_aliases_and_rejects_ignored_and_escaping_events() {
    use std::os::unix::fs::symlink;
    let fixture = HomeFixture::new();
    let main = fixture.session(&fixture.legacy(), ID_A);
    let outside = fixture.session(&fixture.home.join("outside"), ID_B);
    let alias = fixture.home.join("watch-alias");
    symlink(fixture.legacy(), &alias).unwrap();
    symlink(&outside, fixture.legacy().join("escape.jsonl")).unwrap();
    symlink(
        outside.parent().unwrap(),
        fixture.legacy().join("escape-dir"),
    )
    .unwrap();
    symlink(&main, fixture.legacy().join("valid-link.jsonl")).unwrap();
    symlink("loop-b", fixture.legacy().join("loop-a")).unwrap();
    symlink("loop-a", fixture.legacy().join("loop-b")).unwrap();
    let desired = [
        ("claude".into(), fixture.legacy()),
        ("claude".into(), alias),
    ];
    let (mut native, receiver) = native_watcher();
    let mut state = WatchState::default();
    let (remaining, changed) = watcher::sync_watch_roots(&mut native, &mut state, &desired);
    assert_eq!(remaining, 0);
    assert_eq!(state.roots.len(), 1);
    assert_eq!(changed.len(), 1);
    for name in [
        "subagents/child.jsonl",
        "tool-results/result.jsonl",
        "workflows/journal.jsonl",
        "agent-child.jsonl",
        "journal.jsonl",
        "notes.txt",
    ] {
        let source = fixture.legacy().join(name);
        write_bytes(&source, &transcript(ID_B, &fixture.cwd));
        assert!(watcher::classify_event_path(&source, &state.roots).is_none());
        fs::remove_file(&source).unwrap();
        assert!(
            watcher::classify_event_path(&source, &state.roots).is_none(),
            "removed ignored records remain excluded"
        );
    }
    for source in [
        outside.clone(),
        fixture.legacy().join("escape.jsonl"),
        fixture
            .legacy()
            .join("escape-dir")
            .join(outside.file_name().unwrap()),
        fixture.legacy().join("loop-a/file.jsonl"),
        fixture.legacy().join("../outside/file.jsonl"),
    ] {
        assert!(watcher::classify_event_path(&source, &state.roots).is_none());
    }
    assert_eq!(
        watcher::classify_event_path(&fixture.legacy().join("valid-link.jsonl"), &state.roots)
            .as_deref(),
        Some("claude")
    );
    let removed = fixture.legacy().join("removed-session.jsonl");
    write_bytes(&removed, &transcript(ID_C, &fixture.cwd));
    fs::remove_file(&removed).unwrap();
    assert_eq!(
        watcher::classify_event_path(&removed, &state.roots).as_deref(),
        Some("claude")
    );
    prime_native_watch(&receiver, &fixture.legacy(), &state, "filtered");
    let new_main = fixture.session(&fixture.legacy(), ID_C);
    receive_source_event(&receiver, &new_main, &state);
    assert_eq!(fs::read(&outside).unwrap(), transcript(ID_B, &fixture.cwd));
}

#[cfg(unix)]
#[test]
fn watcher_does_not_reclassify_ignored_symlink_aliases_as_user_sessions() {
    use std::os::unix::fs::symlink;
    let fixture = HomeFixture::new();
    let main = fixture.session(&fixture.legacy(), ID_A);
    let root = fixture.legacy();
    symlink(&main, root.join("agent-alias.jsonl")).unwrap();
    symlink(&main, root.join("journal.jsonl")).unwrap();
    symlink(main.parent().unwrap(), root.join("subagents")).unwrap();
    let (mut native, _receiver) = native_watcher();
    let mut state = WatchState::default();
    assert_eq!(
        watcher::sync_watch_roots(&mut native, &mut state, &[("claude".into(), root.clone())]).0,
        0
    );
    for alias in [
        root.join("agent-alias.jsonl"),
        root.join("journal.jsonl"),
        root.join("subagents").join(main.file_name().unwrap()),
    ] {
        assert!(
            watcher::classify_event_path(&alias, &state.roots).is_none(),
            "ignored lexical paths remain ignored through internal symlinks"
        );
    }
}

// Separately opted-in read-only real Claude acceptance. No shell, wrapper,
// mutation, search cache, or direct application-config reader is used here.

fn private_result<T, E>(result: Result<T, E>, label: &str) -> T {
    match result {
        Ok(value) => value,
        Err(_) => panic!("{label}"), // Do not expose paths, transcripts or serde errors.
    }
}

fn raw_text_fragments(value: &Value, fragments: &mut Vec<String>) {
    match value {
        Value::String(text) if !text.trim().is_empty() => fragments.push(text.clone()),
        Value::Array(items) => {
            for item in items {
                raw_text_fragments(item, fragments);
            }
        }
        Value::Object(item) => {
            // Tool-only user envelopes are rendered with a different role.
            // Compare the actual user/assistant prose, including prose mixed
            // with tool blocks, without reimplementing the product formatter.
            if matches!(
                item.get("type").and_then(Value::as_str),
                Some("tool_use" | "tool_result" | "toolCall")
            ) {
                return;
            }
            for key in ["text", "input_text", "output_text"] {
                if let Some(text) = item.get(key).and_then(Value::as_str) {
                    if !text.trim().is_empty() {
                        fragments.push(text.to_owned());
                    }
                    return;
                }
            }
            if let Some(content) = item.get("content") {
                raw_text_fragments(content, fragments);
            }
        }
        _ => {}
    }
}

fn contains_fragments_in_order(content: &str, fragments: &[String]) -> bool {
    let mut rest = content;
    for fragment in fragments {
        let Some(offset) = rest.find(fragment.as_str()) else {
            return false;
        };
        rest = &rest[offset + fragment.len()..];
    }
    true
}

fn compare_raw_user_assistant_content(bytes: &[u8], loaded: &[SessionMessage]) -> (usize, usize) {
    let mut counts = [0usize; 2];
    let mut next_loaded = 0;
    for line in bytes.split(|byte| *byte == b'\n') {
        let Ok(record) = serde_json::from_slice::<Value>(line) else {
            continue;
        };
        if record.get("isMeta").and_then(Value::as_bool) == Some(true) {
            continue;
        }
        let Some(message) = record.get("message") else {
            continue;
        };
        let Some(role) = message.get("role").and_then(Value::as_str) else {
            continue;
        };
        let role_index = match role {
            "user" => 0,
            "assistant" => 1,
            _ => continue,
        };
        let mut fragments = Vec::new();
        raw_text_fragments(&message["content"], &mut fragments);
        if fragments.is_empty() {
            continue;
        }
        counts[role_index] += 1;
        let relative = loaded[next_loaded..].iter().position(|actual| {
            actual.role == role && contains_fragments_in_order(&actual.content, &fragments)
        });
        // Boolean assertion deliberately avoids printing private message text.
        assert!(
            relative.is_some(),
            "raw user/assistant content was omitted, changed, duplicated away, or reordered"
        );
        next_loaded += relative.unwrap() + 1;
    }
    (counts[0], counts[1])
}

#[test]
#[ignore = "manual read-only acceptance only: set AI_CODING_CLAUDE_ACCEPTANCE_HOME and explicitly select this test with --ignored --exact; skipping is NOT_RUN"]
fn read_only_real_claude_dual_roots_acceptance() {
    let home = PathBuf::from(
        std::env::var_os("AI_CODING_CLAUDE_ACCEPTANCE_HOME")
            .expect("explicit acceptance home is required; absence is not a pass"),
    );
    assert!(
        home.is_absolute() && home.is_dir(),
        "acceptance home must be an existing absolute directory"
    );
    // Explicit-home default legacy configuration. Only the product's bounded
    // profile-field reader may inspect config.private.json; no auth/settings
    // files, environment overrides, or application preferences are loaded.
    let configured = home.join(".claude");
    let roots = claude_roots::resolve_for_home(&home, &configured);
    assert_eq!(
        roots.len(),
        2,
        "this acceptance scenario requires two distinct logical sources"
    );
    assert!(matches!(
        roots[0].source,
        ClaudeSource::Configured | ClaudeSource::Archive
    ));
    assert!(roots[1].source == ClaudeSource::CurrentGg);
    assert!(
        roots[0].path.is_dir(),
        "real legacy history must be available"
    );
    let paths = claude_roots::paths(&roots);
    let canonical_roots: Vec<_> = roots
        .iter()
        .map(|root| root.path.canonicalize().ok())
        .collect();
    let sessions = claude::scan_sessions_with_roots(&home, &roots);
    assert!(
        !sessions.is_empty(),
        "real acceptance needs saved legacy sessions"
    );
    let mut counts: HashMap<&str, usize> = HashMap::new();
    let mut seen = BTreeSet::new();
    let mut raw_user = 0;
    let mut raw_assistant = 0;
    let mut loaded_user = 0;
    let mut loaded_assistant = 0;
    let mut checked_bytes = 0u64;
    for meta in &sessions {
        let source = Path::new(
            meta.source_path
                .as_deref()
                .expect("scanned source is required"),
        );
        let canonical = private_result(
            claude_roots::validate_source(source, &paths),
            "scanned source failed root validation",
        );
        assert!(
            seen.insert(canonical.clone()),
            "canonical source was duplicated"
        );
        let in_gg = canonical_roots[1]
            .as_ref()
            .is_some_and(|root| canonical.starts_with(root));
        let category = if in_gg {
            "current_gg"
        } else if roots[0].source == ClaudeSource::Archive {
            "legacy_archive"
        } else {
            "legacy_configured"
        };
        if !in_gg {
            assert!(
                canonical_roots[0]
                    .as_ref()
                    .is_some_and(|root| canonical.starts_with(root)),
                "legacy session escaped its root"
            );
        }
        *counts.entry(category).or_default() += 1;
        let bytes = private_result(fs::read(&canonical), "read-only transcript snapshot failed");
        let before_mtime =
            private_result(fs::metadata(&canonical), "transcript metadata unavailable")
                .modified()
                .ok();
        let messages = private_result(
            claude::load_messages_with_roots(&canonical, &paths),
            "real transcript could not be loaded",
        );
        let (user, assistant) = compare_raw_user_assistant_content(&bytes, &messages);
        raw_user += user;
        raw_assistant += assistant;
        loaded_user += messages
            .iter()
            .filter(|message| message.role == "user")
            .count();
        loaded_assistant += messages
            .iter()
            .filter(|message| message.role == "assistant")
            .count();
        let stat = private_result(
            session_file_stat_with_roots("claude", &canonical, &paths),
            "allowed source stat failed",
        )
        .expect("saved source stat must exist");
        assert!(
            stat.size == bytes.len() as u64,
            "stat size differs from raw source"
        );
        if let Some(modified) = before_mtime.and_then(|time| time.duration_since(UNIX_EPOCH).ok()) {
            assert!(
                stat.mtime_ms == modified.as_millis() as i64,
                "stat mtime differs from raw source"
            );
        }
        assert!(
            session_file_stat_with_roots("claude", &canonical, &[]).is_err(),
            "stat ignored its allowed-root boundary"
        );
        assert!(
            claude::load_messages_with_roots(&canonical, &[]).is_err(),
            "reader ignored its allowed-root boundary"
        );

        let prepared =
            resume::prepare_claude_with_roots(&home, &meta.session_id, &canonical, &roots);
        if in_gg {
            if let Some(command) = &meta.resume_command {
                assert!(
                    command
                        == &expected_gg_command(&claude_roots::gg_wrapper(&home), &meta.session_id),
                    "GG metadata uses an unsafe resume route"
                );
                assert!(
                    uuid::Uuid::parse_str(&meta.session_id).is_ok(),
                    "resume metadata contains a non-UUID identifier"
                );
            }
        } else if roots[0].source == ClaudeSource::Archive || claude_roots::gg_installed(&home) {
            assert!(
                meta.resume_command.is_none(),
                "old history advertises an unsafe resume command"
            );
            assert!(
                prepared.is_err(),
                "old history must not resume into current GG"
            );
        }
        if let Ok(prepared) = prepared {
            assert!(
                meta.resume_command.as_ref() == Some(&prepared.command),
                "metadata and prepared resume disagree"
            );
            assert!(
                prepared.cwd == meta.project_dir,
                "prepared working directory differs from saved metadata"
            );
            if in_gg {
                assert!(
                    prepared.command
                        == expected_gg_command(&claude_roots::gg_wrapper(&home), &meta.session_id),
                    "prepared GG command bypasses the existing wrapper"
                );
            }
        }
        let wrong_id = if meta.session_id == ID_A { ID_B } else { ID_A };
        assert!(
            resume::prepare_claude_with_roots(&home, wrong_id, &canonical, &roots).is_err(),
            "resume accepted a different source UUID"
        );
        assert!(
            private_result(fs::read(&canonical), "source reread failed") == bytes,
            "read-only acceptance observed changed transcript bytes"
        );
        assert!(
            private_result(fs::metadata(&canonical), "source restat failed")
                .modified()
                .ok()
                == before_mtime,
            "read-only acceptance observed changed source mtime"
        );
        checked_bytes += bytes.len() as u64;
    }
    let archive = counts.get("legacy_archive").copied().unwrap_or(0);
    let configured = counts.get("legacy_configured").copied().unwrap_or(0);
    let gg = counts.get("current_gg").copied().unwrap_or(0);
    assert!(
        archive + configured > 0,
        "no real legacy records were examined"
    );
    assert!(
        raw_user > 0 && raw_assistant > 0,
        "real acceptance needs both user and assistant prose"
    );
    assert!(loaded_user >= raw_user && loaded_assistant >= raw_assistant);
    // GG's logical root may be absent or empty. That is an explicit supported
    // state, not an ignored failure or a requirement for creating user files.
    println!("READ_ONLY_CLAUDE_ACCEPTANCE roots=2 legacy_archive={archive} legacy_configured={configured} current_gg={gg} current_gg_root_present={} scanned={} raw_user_text={raw_user} raw_assistant_text={raw_assistant} loaded_user={loaded_user} loaded_assistant={loaded_assistant} checked_bytes={checked_bytes}", usize::from(roots[1].path.is_dir()), sessions.len());
}
