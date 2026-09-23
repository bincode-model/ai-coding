use std::collections::HashMap;
use std::fs::File;
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use regex::Regex;
use rusqlite::Connection;
use serde::Deserialize;
use serde_json::Value;

use crate::codex_config::{get_codex_config_dir, read_codex_config_text};
use crate::codex_state_db::codex_state_db_paths;
use crate::session_manager::{SessionMessage, SessionMeta};

use super::utils::{
    extract_text, parse_timestamp_to_ms, path_basename, read_head_tail_lines, truncate_summary,
    TITLE_MAX_CHARS,
};

const PROVIDER_ID: &str = "codex";
const CODEX_SESSION_INDEX_FILENAME: &str = "session_index.jsonl";
const VSCODE_CONTEXT_PREFIX: &str = "# Context from my IDE setup:";
const CODEX_REQUEST_MARKER: &str = "my request for codex";

static UUID_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
        .unwrap()
});

#[derive(Deserialize)]
struct SessionIndexEntry {
    id: String,
    thread_name: String,
}

pub fn scan_sessions() -> Vec<SessionMeta> {
    let roots = session_roots();
    scan_sessions_in_roots(&roots)
}

pub fn session_roots() -> Vec<PathBuf> {
    let config_dir = get_codex_config_dir();
    vec![
        config_dir.join("sessions"),
        config_dir.join("archived_sessions"),
    ]
}

fn scan_sessions_in_roots(roots: &[PathBuf]) -> Vec<SessionMeta> {
    let thread_titles = load_thread_titles();
    scan_sessions_in_roots_with_titles(roots, &thread_titles)
}

fn scan_sessions_in_roots_with_titles(
    roots: &[PathBuf],
    thread_titles: &HashMap<String, String>,
) -> Vec<SessionMeta> {
    let mut files = Vec::new();
    for root in roots {
        collect_jsonl_files(root, &mut files);
    }

    let mut sessions = Vec::new();
    for path in files {
        if let Some(meta) = parse_session_with_titles(&path, thread_titles) {
            sessions.push(meta);
        }
    }

    sessions
}

fn load_thread_titles() -> HashMap<String, String> {
    let config_dir = get_codex_config_dir();
    let config_text = read_codex_config_text().unwrap_or_default();
    let db_paths = codex_state_db_paths(&config_dir, &config_text);
    load_thread_titles_from_paths(&config_dir.join(CODEX_SESSION_INDEX_FILENAME), &db_paths)
}

fn load_thread_titles_from_paths(
    session_index_path: &Path,
    db_paths: &[PathBuf],
) -> HashMap<String, String> {
    let mut titles = load_thread_titles_from_session_index(session_index_path);
    for db_path in db_paths {
        titles.extend(load_thread_titles_from_db(db_path));
    }
    titles
}

fn load_thread_titles_from_session_index(index_path: &Path) -> HashMap<String, String> {
    if !index_path.exists() {
        return HashMap::new();
    }

    let file = match File::open(index_path) {
        Ok(file) => file,
        Err(err) => {
            log::warn!(
                "Failed to open Codex session index {}: {err}",
                index_path.display()
            );
            return HashMap::new();
        }
    };

    let reader = BufReader::new(file);
    let mut titles = HashMap::new();
    for line in reader.lines() {
        let line = match line {
            Ok(line) => line,
            Err(_) => continue,
        };
        let Ok(entry) = serde_json::from_str::<SessionIndexEntry>(line.trim()) else {
            continue;
        };
        let id = entry.id.trim();
        let title = entry.thread_name.trim();
        if !id.is_empty() && !title.is_empty() {
            titles.insert(id.to_string(), title.to_string());
        }
    }

    titles
}

fn load_thread_titles_from_db(db_path: &Path) -> HashMap<String, String> {
    if !db_path.exists() {
        return HashMap::new();
    }

    let conn = match Connection::open_with_flags(
        db_path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
    ) {
        Ok(conn) => conn,
        Err(err) => {
            log::warn!(
                "Failed to open Codex state database {}: {err}",
                db_path.display()
            );
            return HashMap::new();
        }
    };
    // Codex keeps this DB open and write-locked while running; without a busy
    // timeout a read during a write fails immediately and titles silently drop.
    if let Err(err) = conn.busy_timeout(Duration::from_secs(2)) {
        log::warn!(
            "Failed to set Codex state database busy timeout for {}: {err}",
            db_path.display()
        );
        return HashMap::new();
    }

    // Mirror Codex's own `distinct_thread_metadata_title`: keep a title only
    // when it differs from the first user message. Push the comparison into SQL
    // (NULL-safe) so we never SELECT the unbounded `first_user_message` blob —
    // it can grow large enough to OOM (openai/codex#29007).
    let mut stmt = match conn.prepare(
        "SELECT id, title FROM threads \
         WHERE title <> '' \
         AND (first_user_message IS NULL OR TRIM(title) <> TRIM(first_user_message))",
    ) {
        Ok(stmt) => stmt,
        Err(err) => {
            log::warn!(
                "Failed to prepare Codex thread title query for {}: {err}",
                db_path.display()
            );
            return HashMap::new();
        }
    };

    let rows = match stmt.query_map([], |row| {
        let id: String = row.get(0)?;
        let title: String = row.get(1)?;
        Ok((id, title))
    }) {
        Ok(rows) => rows,
        Err(err) => {
            log::warn!(
                "Failed to query Codex thread titles from {}: {err}",
                db_path.display()
            );
            return HashMap::new();
        }
    };

    rows.flatten()
        .filter_map(|(id, title)| {
            let id = id.trim();
            let title = title.trim();
            if id.is_empty() || title.is_empty() {
                None
            } else {
                Some((id.to_string(), title.to_string()))
            }
        })
        .collect()
}

pub fn load_messages(path: &Path) -> Result<Vec<SessionMessage>, String> {
    let file = File::open(path).map_err(|e| format!("Failed to open session file: {e}"))?;
    let reader = BufReader::new(file);
    let mut messages = MessageAccumulator::default();
    let mut has_original_history = false;
    let mut turn_id = None;

    for line in reader.lines() {
        let line = match line {
            Ok(value) => value,
            Err(_) => continue,
        };
        let value: Value = match serde_json::from_str(&line) {
            Ok(parsed) => parsed,
            Err(_) => continue,
        };

        let payload = value.get("payload").unwrap_or(&value);
        if let Some(id) = payload.get("turn_id").and_then(Value::as_str) {
            turn_id = Some(id.to_string());
        }
        if value.get("type").and_then(Value::as_str) == Some("compacted") {
            if !has_original_history {
                if let Some(history) = payload.get("replacement_history").and_then(Value::as_array)
                {
                    // Forked rollouts can start with a compacted snapshot. It
                    // restores only missing history; never replace original turns.
                    let mut setup: Vec<_> = messages
                        .entries
                        .into_iter()
                        .filter(|entry| entry.sources & RecordSource::Replacement as u8 == 0)
                        .collect();
                    messages = MessageAccumulator::default();
                    for item in history {
                        if let Some(message) = visible_item(item, None) {
                            messages.push(message, RecordSource::Replacement, turn_id.clone());
                        }
                    }
                    // Keep leading instructions when the snapshot does not
                    // already contain them, without repeating snapshot copies.
                    setup.retain(|entry| {
                        !messages.entries.iter().any(|replacement| {
                            replacement.item.message.role == entry.item.message.role
                                && replacement.item.message.content == entry.item.message.content
                        })
                    });
                    setup.append(&mut messages.entries);
                    messages.entries = setup;
                }
            }
            continue;
        }
        if let Some((message, source)) = visible_record(&value) {
            // Setup instructions alone are not a conversation transcript.
            // A fork can store them before its only surviving history snapshot.
            has_original_history |=
                !matches!(message.message.role.as_str(), "system" | "developer");
            messages.push(message, source, turn_id.clone());
        }
    }

    Ok(messages
        .entries
        .into_iter()
        .map(|entry| entry.item.message)
        .collect())
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum RecordSource {
    Response = 1,
    Event = 2,
    Completed = 4,
    Replacement = 8,
}

struct VisibleItem {
    message: SessionMessage,
    id: Option<String>,
    is_tool_call: bool,
}

struct MessageEntry {
    item: VisibleItem,
    sources: u8,
    turn_id: Option<String>,
}

#[derive(Default)]
struct MessageAccumulator {
    entries: Vec<MessageEntry>,
}

impl MessageAccumulator {
    fn push(&mut self, item: VisibleItem, source: RecordSource, turn_id: Option<String>) {
        let bit = source as u8;
        // Pair mirrored representations once, within the current conversation
        // turn. Never globally deduplicate text: repeated user prompts and
        // identical tool output in later turns are legitimate history.
        for entry in self.entries.iter_mut().rev() {
            if entry.turn_id != turn_id || entry.sources & RecordSource::Replacement as u8 != 0 {
                break;
            }
            let previous = &entry.item;
            if previous.message.role != item.message.role {
                if item.message.role == "user" || previous.message.role == "user" {
                    break;
                }
                continue;
            }
            if entry.sources & bit != 0 {
                // An intervening representation from the same source is a new
                // occurrence, even when it contains exactly the same text.
                break;
            }
            let ids_compatible = match (&previous.id, &item.id) {
                (Some(left), Some(right)) => left == right,
                _ => true,
            };
            if ids_compatible
                && previous.is_tool_call == item.is_tool_call
                && previous.message.content == item.message.content
            {
                entry.sources |= bit;
                if source == RecordSource::Response {
                    entry.item = item;
                }
                return;
            }
        }
        self.entries.push(MessageEntry {
            item,
            sources: bit,
            turn_id,
        });
    }
}

fn visible_record(value: &Value) -> Option<(VisibleItem, RecordSource)> {
    let ts = value.get("timestamp").and_then(parse_timestamp_to_ms);
    let payload = value.get("payload").unwrap_or(value);
    let (item, source) = match value.get("type").and_then(Value::as_str)? {
        "response_item" => (payload, RecordSource::Response),
        "event_msg" => match payload.get("type").and_then(Value::as_str) {
            Some("item_completed" | "itemCompleted") => {
                (payload.get("item")?, RecordSource::Completed)
            }
            Some("user_message" | "agent_message") => (payload, RecordSource::Event),
            _ => return None,
        },
        "item_completed" | "itemCompleted" => (payload.get("item")?, RecordSource::Completed),
        _ => return None,
    };
    visible_item(item, ts).map(|message| (message, source))
}

fn visible_item(item: &Value, ts: Option<i64>) -> Option<VisibleItem> {
    let kind = item.get("type").and_then(Value::as_str)?;
    let mut is_tool_call = false;
    let (role, content) = match kind {
        "message" => (
            item.get("role")
                .and_then(Value::as_str)
                .unwrap_or("unknown")
                .to_string(),
            item.get("content").map(extract_text).unwrap_or_default(),
        ),
        "user_message" | "userMessage" | "agent_message" | "agentMessage" => {
            let role = if matches!(kind, "user_message" | "userMessage") {
                "user"
            } else {
                "assistant"
            };
            let content = item
                .get("message")
                .or_else(|| item.get("text"))
                .or_else(|| item.get("content"))
                .map(extract_text)
                .unwrap_or_default();
            (role.to_string(), content)
        }
        "function_call" | "custom_tool_call" | "functionCall" | "customToolCall" => {
            is_tool_call = true;
            let name = item
                .get("name")
                .and_then(Value::as_str)
                .unwrap_or("unknown");
            let input = item
                .get("arguments")
                .or_else(|| item.get("input"))
                .map(tool_text)
                .unwrap_or_default();
            let content = if input.is_empty() {
                format!("[Tool: {name}]")
            } else {
                format!("[Tool: {name}]\n{input}")
            };
            ("assistant".to_string(), content)
        }
        "function_call_output"
        | "custom_tool_call_output"
        | "functionCallOutput"
        | "customToolCallOutput" => (
            "tool".to_string(),
            item.get("output").map(tool_text).unwrap_or_default(),
        ),
        // Do not invent text for encrypted reasoning, binary attachments, or
        // unknown event kinds. Only completed, stored visible content is read.
        _ => return None,
    };
    if content.trim().is_empty() {
        return None;
    }
    Some(VisibleItem {
        message: SessionMessage { role, content, ts },
        id: item
            .get("call_id")
            .or_else(|| item.get("id"))
            .and_then(Value::as_str)
            .map(str::to_string),
        is_tool_call,
    })
}

fn tool_text(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::String(text) => text.clone(),
        Value::Array(items) => items
            .iter()
            .map(|item| {
                // Plain text blocks have a useful readable representation.
                // Keep other structured output intact, including mixed arrays.
                if item.as_object().is_some_and(|fields| {
                    fields
                        .keys()
                        .all(|key| matches!(key.as_str(), "type" | "text"))
                        && matches!(
                            fields.get("type").and_then(Value::as_str),
                            Some("text" | "input_text" | "output_text")
                        )
                        && fields.get("text").is_some_and(Value::is_string)
                }) {
                    extract_text(item)
                } else {
                    tool_text(item)
                }
            })
            .collect::<Vec<_>>()
            .join("\n"),
        _ => value.to_string(),
    }
}

pub fn delete_session(_root: &Path, path: &Path, session_id: &str) -> Result<bool, String> {
    let meta = parse_session(path)
        .ok_or_else(|| format!("Failed to parse Codex session metadata: {}", path.display()))?;

    if meta.session_id != session_id {
        return Err(format!(
            "Codex session ID mismatch: expected {session_id}, found {}",
            meta.session_id
        ));
    }

    std::fs::remove_file(path).map_err(|e| {
        format!(
            "Failed to delete Codex session file {}: {e}",
            path.display()
        )
    })?;

    Ok(true)
}

pub(crate) fn parse_session(path: &Path) -> Option<SessionMeta> {
    parse_session_with_titles(path, &HashMap::new())
}

#[derive(Clone, Default)]
struct ScannedMetadata {
    session_id: Option<String>,
    project_dir: Option<String>,
    created_at: Option<i64>,
    first_user_message: Option<String>,
    is_subagent: bool,
    last_active_at: Option<i64>,
    summary: Option<String>,
}

impl ScannedMetadata {
    fn observe(&mut self, value: &Value) {
        if self.created_at.is_none() {
            self.created_at = value.get("timestamp").and_then(parse_timestamp_to_ms);
        }
        if value.get("type").and_then(Value::as_str) == Some("session_meta") {
            if let Some(payload) = value.get("payload") {
                // A resumed/forked rollout may contain earlier metadata from a
                // different session. The final metadata describes its live owner.
                if let Some(id) = payload
                    .get("id")
                    .and_then(Value::as_str)
                    .filter(|id| !id.is_empty())
                {
                    self.session_id = Some(id.to_string());
                    self.is_subagent = is_subagent_source(payload.get("source"));
                }
                if let Some(cwd) = payload.get("cwd").and_then(Value::as_str) {
                    self.project_dir = Some(cwd.to_string());
                }
                if self.created_at.is_none() {
                    self.created_at = payload.get("timestamp").and_then(parse_timestamp_to_ms);
                }
            }
        }
        if self.first_user_message.is_none() {
            if let Some((item, _)) = visible_record(value) {
                if item.message.role == "user" {
                    self.first_user_message =
                        title_candidate_from_user_message(&item.message.content)
                            .map(|title| truncate_summary(&title, TITLE_MAX_CHARS));
                }
            }
        }
    }
}

#[derive(Clone, PartialEq, Eq)]
struct FileStamp {
    len: u64,
    modified: Option<SystemTime>,
    created: Option<SystemTime>,
}

impl FileStamp {
    fn from_metadata(meta: &std::fs::Metadata) -> Self {
        Self {
            len: meta.len(),
            modified: meta.modified().ok(),
            created: meta.created().ok(),
        }
    }
}

#[derive(Clone)]
struct CachedMetadata {
    stamp: FileStamp,
    // Offset/state include only complete lines; an in-progress JSON record is
    // reread after the CLI appends the rest of it.
    offset: u64,
    committed: ScannedMetadata,
    visible: ScannedMetadata,
    anchor: Vec<u8>,
}

static METADATA_CACHE: LazyLock<Mutex<HashMap<PathBuf, CachedMetadata>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

// Only deserialize the small metadata fields after a title has been found.
// Serde skips tool inputs/outputs without allocating their potentially huge
// strings while still finding metadata anywhere in a resumed rollout.
#[derive(Deserialize)]
struct MetadataRecord {
    #[serde(rename = "type")]
    record_type: String,
    timestamp: Option<Value>,
    payload: MetadataPayload,
}

#[derive(Deserialize)]
struct MetadataPayload {
    id: Option<String>,
    cwd: Option<String>,
    timestamp: Option<Value>,
    source: Option<Value>,
}

fn observe_metadata_only(metadata: &mut ScannedMetadata, bytes: &[u8]) {
    let Ok(record) = serde_json::from_slice::<MetadataRecord>(bytes) else {
        return;
    };
    if metadata.created_at.is_none() {
        metadata.created_at = record.timestamp.as_ref().and_then(parse_timestamp_to_ms);
    }
    if record.record_type != "session_meta" {
        return;
    }
    if let Some(id) = record.payload.id.filter(|id| !id.is_empty()) {
        metadata.session_id = Some(id);
        metadata.is_subagent = is_subagent_source(record.payload.source.as_ref());
    }
    if let Some(cwd) = record.payload.cwd {
        metadata.project_dir = Some(cwd);
    }
    if metadata.created_at.is_none() {
        metadata.created_at = record
            .payload
            .timestamp
            .as_ref()
            .and_then(parse_timestamp_to_ms);
    }
}

fn metadata_anchor(file: &mut File, offset: u64) -> std::io::Result<Vec<u8>> {
    let count = offset.min(256) as usize;
    file.seek(SeekFrom::Start(offset - count as u64))?;
    let mut anchor = vec![0; count];
    file.read_exact(&mut anchor)?;
    Ok(anchor)
}

fn scan_metadata(path: &Path) -> std::io::Result<(ScannedMetadata, FileStamp)> {
    let mut file = File::open(path)?;
    let stamp = FileStamp::from_metadata(&file.metadata()?);
    let previous = METADATA_CACHE
        .lock()
        .ok()
        .and_then(|map| map.get(path).cloned());
    if let Some(cached) = previous.as_ref().filter(|cached| cached.stamp == stamp) {
        return Ok((cached.visible.clone(), stamp));
    }
    let mut committed = ScannedMetadata::default();
    let mut offset = 0;
    if let Some(cached) = previous
        .filter(|cached| stamp.len > cached.stamp.len && stamp.created == cached.stamp.created)
    {
        // Verify the previous read boundary before treating a larger file as
        // appended. Truncation/replacement otherwise starts a fresh scan.
        if metadata_anchor(&mut file, cached.offset)? == cached.anchor {
            committed = cached.committed;
            offset = cached.offset;
        }
    }
    file.seek(SeekFrom::Start(offset))?;
    let mut reader = BufReader::new((&mut file).take(stamp.len - offset));
    let mut bytes = Vec::new();
    let mut visible = committed.clone();
    loop {
        bytes.clear();
        let count = reader.read_until(b'\n', &mut bytes)?;
        if count == 0 {
            break;
        }
        if visible.first_user_message.is_some() {
            observe_metadata_only(&mut visible, &bytes);
        } else if let Ok(value) = serde_json::from_slice::<Value>(&bytes) {
            visible.observe(&value);
        }
        if bytes.last() == Some(&b'\n') {
            offset += count as u64;
            committed = visible.clone();
        } else {
            break;
        }
    }
    drop(reader);
    let anchor = metadata_anchor(&mut file, offset)?;
    let (_, tail) = read_head_tail_lines(path, 0, 30)?;
    // Cache tail data with the file stamp as well: unchanged large records must
    // not be parsed again on each filesystem watcher refresh.
    visible.last_active_at = None;
    visible.summary = None;
    for line in tail.iter().rev() {
        let Ok(value) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        if visible.last_active_at.is_none() {
            visible.last_active_at = value.get("timestamp").and_then(parse_timestamp_to_ms);
        }
        if visible.summary.is_none() {
            if let Some((item, _)) = visible_record(&value) {
                if !item.is_tool_call && item.message.role != "tool" {
                    visible.summary = Some(truncate_summary(&item.message.content, 160));
                }
            }
        }
        if visible.last_active_at.is_some() && visible.summary.is_some() {
            break;
        }
    }
    visible.last_active_at = visible.last_active_at.or_else(|| {
        stamp
            .modified
            .and_then(|time| time.duration_since(UNIX_EPOCH).ok())
            .map(|duration| duration.as_millis() as i64)
    });
    if let Ok(mut map) = METADATA_CACHE.lock() {
        const MAX_CACHED_SESSIONS: usize = 4096;
        if map.len() >= MAX_CACHED_SESSIONS && !map.contains_key(path) {
            if let Some(evicted) = map.keys().next().cloned() {
                map.remove(&evicted);
            }
        }
        map.insert(
            path.to_path_buf(),
            CachedMetadata {
                stamp: stamp.clone(),
                offset,
                committed,
                visible: visible.clone(),
                anchor,
            },
        );
    }
    Ok((visible, stamp))
}

fn parse_session_with_titles(
    path: &Path,
    thread_titles: &HashMap<String, String>,
) -> Option<SessionMeta> {
    let (metadata, _) = scan_metadata(path).ok()?;
    if metadata.is_subagent {
        return None;
    }
    let ScannedMetadata {
        session_id,
        project_dir,
        created_at,
        first_user_message,
        last_active_at,
        summary,
        ..
    } = metadata;

    let session_id = session_id.or_else(|| infer_session_id_from_filename(path));
    let session_id = session_id?;

    let title = thread_titles
        .get(&session_id)
        .map(|t| truncate_summary(t, TITLE_MAX_CHARS))
        .or_else(|| first_user_message.map(|t| truncate_summary(&t, TITLE_MAX_CHARS)))
        .or_else(|| {
            project_dir
                .as_deref()
                .and_then(path_basename)
                .map(|v| v.to_string())
        });

    Some(SessionMeta {
        provider_id: PROVIDER_ID.to_string(),
        session_id: session_id.clone(),
        title,
        summary,
        project_dir,
        created_at,
        last_active_at,
        source_path: Some(path.to_string_lossy().to_string()),
        resume_command: Some(format!("codex resume {session_id}")),
    })
}

fn is_subagent_source(source: Option<&Value>) -> bool {
    source
        .and_then(|value| value.as_object())
        .map(|source| source.contains_key("subagent"))
        .unwrap_or(false)
}

fn title_candidate_from_user_message(text: &str) -> Option<String> {
    let trimmed = text.trim();
    if trimmed.is_empty()
        || trimmed.starts_with("# AGENTS.md")
        || trimmed.starts_with("<environment_context>")
    {
        return None;
    }

    if trimmed.starts_with(VSCODE_CONTEXT_PREFIX) {
        return extract_codex_prompt_from_ide_context(trimmed);
    }

    Some(trimmed.to_string())
}

fn extract_codex_prompt_from_ide_context(text: &str) -> Option<String> {
    let normalized = text.replace("\r\n", "\n");
    let lines = normalized.lines().collect::<Vec<_>>();

    // VS Code injects the real prompt as the LAST "## My request for Codex:"
    // section, so keep the final matching heading. Earlier matches can be
    // headings that live inside the active selection / open file content.
    // Trade-off: if the request body itself repeats the heading, the title
    // truncates to its trailing part (rare; covered by tests below).
    let mut prompt: Option<String> = None;
    for (index, line) in lines.iter().enumerate() {
        let Some(inline_prompt) = codex_request_heading_payload(line) else {
            continue;
        };

        if !inline_prompt.is_empty() {
            prompt = Some(inline_prompt.to_string());
            continue;
        }

        let following_prompt = lines[index + 1..].join("\n").trim().to_string();
        prompt = (!following_prompt.is_empty()).then_some(following_prompt);
    }

    prompt
}

fn codex_request_heading_payload(line: &str) -> Option<&str> {
    let trimmed = line.trim();
    if !trimmed.starts_with('#') {
        return None;
    }

    let heading = trimmed.trim_start_matches('#').trim_start();
    let lowered = heading.to_ascii_lowercase();
    if !lowered.starts_with(CODEX_REQUEST_MARKER) {
        return None;
    }

    let suffix = heading[CODEX_REQUEST_MARKER.len()..].trim_start();
    if suffix.is_empty() {
        return Some("");
    }

    let Some(separator) = suffix.chars().next() else {
        return Some("");
    };
    if !matches!(separator, ':' | '：' | '-' | '—') {
        return None;
    }

    Some(
        suffix
            .trim_start_matches(|c: char| c.is_whitespace() || matches!(c, ':' | '：' | '-' | '—'))
            .trim(),
    )
}

fn infer_session_id_from_filename(path: &Path) -> Option<String> {
    let file_name = path.file_name()?.to_string_lossy();
    UUID_RE.find(&file_name).map(|mat| mat.as_str().to_string())
}

fn collect_jsonl_files(root: &Path, files: &mut Vec<PathBuf>) {
    if !root.exists() {
        return;
    }

    let entries = match std::fs::read_dir(root) {
        Ok(entries) => entries,
        Err(_) => return,
    };

    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            collect_jsonl_files(&path, files);
        } else if path.extension().and_then(|ext| ext.to_str()) == Some("jsonl") {
            files.push(path);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::codex_state_db::CODEX_STATE_DB_FILENAME;
    use serde_json::json;
    use tempfile::tempdir;

    fn write_records(path: &Path, records: &[Value]) {
        let text = records
            .iter()
            .map(Value::to_string)
            .collect::<Vec<_>>()
            .join("\n");
        std::fs::write(path, format!("{text}\n")).unwrap();
    }

    fn response_message(role: &str, text: &str) -> Value {
        json!({"type":"response_item", "payload":{"type":"message", "role":role, "content":[{"type":"text","text":text}]}})
    }

    fn event_message(role: &str, text: &str) -> Value {
        let kind = if role == "user" {
            "user_message"
        } else {
            "agent_message"
        };
        json!({"type":"event_msg", "payload":{"type":kind,"message":text}})
    }

    #[test]
    fn mirrored_messages_are_paired_without_dropping_repeated_turns() {
        let temp = tempdir().unwrap();
        let path = temp.path().join("mirrors.jsonl");
        let mut records = Vec::new();
        for turn in ["turn-one", "turn-two"] {
            records.extend([
                json!({"type":"event_msg","payload":{"type":"task_started","turn_id":turn}}),
                event_message("user", "再试一次"),
                response_message("user", "再试一次"),
                response_message("assistant", "完成"),
                event_message("assistant", "完成"),
                json!({"type":"event_msg","payload":{"type":"item_completed","item":{"type":"agentMessage","text":"完成"}}}),
            ]);
        }
        write_records(&path, &records);
        let messages = load_messages(&path).unwrap();
        assert_eq!(messages.len(), 4);
        assert_eq!(
            messages
                .iter()
                .map(|m| m.content.as_str())
                .collect::<Vec<_>>(),
            ["再试一次", "完成", "再试一次", "完成"]
        );
    }

    #[test]
    fn same_source_repeats_and_unmirrored_event_turns_remain_visible() {
        let temp = tempdir().unwrap();
        let path = temp.path().join("repeat.jsonl");
        write_records(
            &path,
            &[
                event_message("user", "repeat"),
                event_message("user", "repeat"),
                event_message("assistant", "ok"),
                response_message("user", "repeat"),
                response_message("user", "repeat"),
                response_message("assistant", "ok"),
            ],
        );
        let messages = load_messages(&path).unwrap();
        assert_eq!(messages.len(), 6);
    }

    #[test]
    fn custom_tools_keep_arguments_and_structured_outputs() {
        let temp = tempdir().unwrap();
        let path = temp.path().join("tools.jsonl");
        write_records(
            &path,
            &[
                json!({"type":"response_item","payload":{"type":"custom_tool_call","name":"apply_patch","call_id":"patch-one","input":"*** Begin Patch\n完整补丁\n*** End Patch"}}),
                json!({"type":"response_item","payload":{"type":"custom_tool_call_output","call_id":"patch-one","output":[{"type":"text","text":"Success"}]}}),
                json!({"type":"response_item","payload":{"type":"function_call","name":"shell","arguments":"{\"cmd\":\"pwd\"}"}}),
                json!({"type":"response_item","payload":{"type":"function_call_output","output":{"exit_code":0,"text":"工作目录","details":"完整结构"}}}),
                json!({"type":"response_item","payload":{"type":"function_call_output","output":[{"type":"text","text":"可读文本"},{"exit_code":1,"stderr":"不能丢失"}]}}),
            ],
        );
        let messages = load_messages(&path).unwrap();
        assert_eq!(messages.len(), 5);
        assert!(messages[0].content.contains("完整补丁"));
        assert_eq!(messages[1].content, "Success");
        assert!(messages[2].content.contains("\"cmd\":\"pwd\""));
        assert!(messages[3].content.contains("工作目录"));
        assert!(messages[3].content.contains("完整结构"));
        assert!(messages[4].content.contains("可读文本"));
        assert!(messages[4].content.contains("不能丢失"));
    }

    #[test]
    fn completed_item_messages_are_loaded_and_mirrored_once() {
        let temp = tempdir().unwrap();
        let path = temp.path().join("completed.jsonl");
        write_records(
            &path,
            &[
                json!({"type":"event_msg","payload":{"type":"item_completed","item":{"type":"userMessage","content":[{"type":"input_text","text":"只有完成事件"}]}}}),
                json!({"type":"item_completed","payload":{"item":{"type":"message","role":"assistant","content":"结果"}}}),
                response_message("assistant", "结果"),
                json!({"type":"response_item","payload":{"type":"reasoning","encrypted_content":"not-visible"}}),
            ],
        );
        let messages = load_messages(&path).unwrap();
        assert_eq!(messages.len(), 2);
        assert_eq!(messages[0].content, "只有完成事件");
        assert_eq!(messages[1].content, "结果");
    }

    #[test]
    fn compacted_snapshot_restores_missing_history_without_replacing_original_turns() {
        let temp = tempdir().unwrap();
        let path = temp.path().join("compacted.jsonl");
        let compacted = json!({"type":"compacted","payload":{"replacement_history":[
            {"type":"message","role":"user","content":"早期问题"},
            {"type":"message","role":"assistant","content":"早期答复"}
        ]}});
        write_records(
            &path,
            &[compacted.clone(), response_message("user", "继续")],
        );
        assert_eq!(
            load_messages(&path)
                .unwrap()
                .iter()
                .map(|m| m.content.as_str())
                .collect::<Vec<_>>(),
            ["早期问题", "早期答复", "继续"]
        );

        write_records(
            &path,
            &[
                response_message("developer", "启动指令"),
                compacted.clone(),
                response_message("user", "继续"),
            ],
        );
        assert_eq!(
            load_messages(&path)
                .unwrap()
                .iter()
                .map(|m| m.content.as_str())
                .collect::<Vec<_>>(),
            ["启动指令", "早期问题", "早期答复", "继续"]
        );

        write_records(
            &path,
            &[
                response_message("user", "原始问题"),
                compacted,
                response_message("assistant", "最新答复"),
            ],
        );
        assert_eq!(
            load_messages(&path)
                .unwrap()
                .iter()
                .map(|m| m.content.as_str())
                .collect::<Vec<_>>(),
            ["原始问题", "最新答复"]
        );
    }

    #[test]
    fn resumed_subagent_uses_final_metadata_beyond_head_window() {
        let temp = tempdir().unwrap();
        let path = temp.path().join("resumed.jsonl");
        let mut records = vec![
            json!({"type":"session_meta","payload":{"id":"old-child","cwd":"/old","source":{"subagent":{}}}}),
            event_message("user", "恢复后的问题"),
        ];
        records
            .extend((0..40).map(|_| json!({"type":"event_msg","payload":{"type":"token_count"}})));
        records.push(json!({"type":"session_meta","payload":{"id":"live-id","cwd":"/current","source":"cli"}}));
        records.push(event_message("user", "恢复后的问题"));
        write_records(&path, &records);
        let session = parse_session(&path).unwrap();
        assert_eq!(session.session_id, "live-id");
        assert_eq!(session.project_dir.as_deref(), Some("/current"));
        assert_eq!(
            session.resume_command.as_deref(),
            Some("codex resume live-id")
        );
        assert_eq!(session.title.as_deref(), Some("恢复后的问题"));
    }

    #[test]
    fn cached_metadata_handles_appends_partial_records_and_replacement() {
        use std::io::Write;
        let temp = tempdir().unwrap();
        let path = temp.path().join("changing.jsonl");
        write_records(
            &path,
            &[json!({"type":"session_meta","payload":{"id":"first","source":"cli"}})],
        );
        assert_eq!(parse_session(&path).unwrap().session_id, "first");
        let partial = "{\"type\":\"session_meta\",\"payload\":{\"id\":\"second\",\"source\":";
        let mut file = std::fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap();
        file.write_all(partial.as_bytes()).unwrap();
        assert_eq!(parse_session(&path).unwrap().session_id, "first");
        file.write_all(b"\"cli\"}}\n").unwrap();
        assert_eq!(parse_session(&path).unwrap().session_id, "second");
        drop(file);

        write_records(
            &path,
            &[
                json!({"type":"session_meta","payload":{"id":"replacement","cwd":"/replacement","source":{"subagent":{}}}}),
            ],
        );
        assert!(parse_session(&path).is_none());
    }

    #[test]
    fn missing_record_timestamps_fall_back_to_file_mtime() {
        let temp = tempdir().unwrap();
        let path = temp.path().join("no-timestamps.jsonl");
        write_records(
            &path,
            &[json!({"type":"session_meta","payload":{"id":"mtime-id","source":"cli"}})],
        );
        let expected = path
            .metadata()
            .unwrap()
            .modified()
            .unwrap()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis() as i64;
        assert_eq!(parse_session(&path).unwrap().last_active_at, Some(expected));
    }

    fn write_codex_session(path: &Path, session_id: &str, message: &str) {
        std::fs::write(
            path,
            format!(
                "{{\"timestamp\":\"2026-03-06T21:50:12Z\",\"type\":\"session_meta\",\"payload\":{{\"id\":\"{session_id}\",\"cwd\":\"/tmp/project\"}}}}\n\
                 {{\"timestamp\":\"2026-03-06T21:50:13Z\",\"type\":\"response_item\",\"payload\":{{\"type\":\"message\",\"role\":\"user\",\"content\":\"{message}\"}}}}\n",
            ),
        )
        .expect("write session");
    }

    #[test]
    fn scan_sessions_in_roots_includes_active_and_archived_files() {
        let temp = tempdir().expect("tempdir");
        let active = temp.path().join("sessions");
        let archived = temp.path().join("archived_sessions");
        std::fs::create_dir_all(&active).expect("active dir");
        std::fs::create_dir_all(&archived).expect("archived dir");

        write_codex_session(&active.join("active.jsonl"), "active-id", "Active session");
        write_codex_session(
            &archived.join("archived.jsonl"),
            "archived-id",
            "Archived session",
        );

        let sessions = scan_sessions_in_roots(&[active, archived]);
        let ids = sessions
            .into_iter()
            .map(|session| session.session_id)
            .collect::<Vec<_>>();

        assert!(ids.contains(&"active-id".to_string()));
        assert!(ids.contains(&"archived-id".to_string()));
    }

    #[test]
    fn delete_session_removes_jsonl_file() {
        let temp = tempdir().expect("tempdir");
        let path = temp
            .path()
            .join("rollout-2026-03-06T21-50-12-019cc369-bd7c-7891-b371-7b20b4fe0b18.jsonl");
        std::fs::write(
            &path,
            concat!(
                "{\"timestamp\":\"2026-03-06T21:50:12Z\",\"type\":\"session_meta\",\"payload\":{\"id\":\"019cc369-bd7c-7891-b371-7b20b4fe0b18\",\"cwd\":\"/tmp/project\"}}\n",
                "{\"timestamp\":\"2026-03-06T21:50:13Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"user\",\"content\":\"hello\"}}\n"
            ),
        )
        .expect("write session");

        delete_session(temp.path(), &path, "019cc369-bd7c-7891-b371-7b20b4fe0b18")
            .expect("delete session");

        assert!(!path.exists());
    }

    #[test]
    fn parse_session_uses_first_user_message_as_title() {
        let temp = tempdir().expect("tempdir");
        let path = temp.path().join("session.jsonl");
        std::fs::write(
            &path,
            concat!(
                "{\"timestamp\":\"2026-03-06T21:50:12Z\",\"type\":\"session_meta\",\"payload\":{\"id\":\"test-id\",\"cwd\":\"/tmp/project\"}}\n",
                "{\"timestamp\":\"2026-03-06T21:50:13Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"user\",\"content\":\"How do I deploy?\"}}\n",
                "{\"timestamp\":\"2026-03-06T21:50:14Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"assistant\",\"content\":\"Here is how...\"}}\n"
            ),
        )
        .expect("write");

        let meta = parse_session(&path).unwrap();
        assert_eq!(meta.title.as_deref(), Some("How do I deploy?"));
    }

    #[test]
    fn parse_session_prefers_thread_title() {
        let temp = tempdir().expect("tempdir");
        let path = temp.path().join("session.jsonl");
        std::fs::write(
            &path,
            concat!(
                "{\"timestamp\":\"2026-03-06T21:50:12Z\",\"type\":\"session_meta\",\"payload\":{\"id\":\"test-id\",\"cwd\":\"/tmp/project\"}}\n",
                "{\"timestamp\":\"2026-03-06T21:50:13Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"user\",\"content\":\"How do I deploy?\"}}\n"
            ),
        )
        .expect("write");

        let mut thread_titles = HashMap::new();
        thread_titles.insert(
            "test-id".to_string(),
            "Renamed deployment thread".to_string(),
        );

        let meta = parse_session_with_titles(&path, &thread_titles).unwrap();
        assert_eq!(meta.title.as_deref(), Some("Renamed deployment thread"));
    }

    #[test]
    fn load_thread_titles_from_state_db_trims_and_filters_titles() {
        let temp = tempdir().expect("tempdir");
        let db_path = temp.path().join(CODEX_STATE_DB_FILENAME);
        let conn = Connection::open(&db_path).expect("open sqlite db");
        conn.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT NOT NULL, first_user_message TEXT NOT NULL)",
            [],
        )
        .expect("create threads table");
        conn.execute(
            "INSERT INTO threads (id, title, first_user_message) VALUES (?1, ?2, ?3)",
            ("thread-1", "  Renamed Codex thread  ", "First prompt"),
        )
        .expect("insert renamed thread");
        conn.execute(
            "INSERT INTO threads (id, title, first_user_message) VALUES (?1, ?2, ?3)",
            ("thread-2", "   ", "First prompt"),
        )
        .expect("insert blank thread");
        conn.execute(
            "INSERT INTO threads (id, title, first_user_message) VALUES (?1, ?2, ?3)",
            ("thread-3", "  First prompt  ", "First prompt"),
        )
        .expect("insert first-message title");
        drop(conn);

        let titles = load_thread_titles_from_db(&db_path);

        assert_eq!(
            titles.get("thread-1").map(String::as_str),
            Some("Renamed Codex thread")
        );
        assert!(!titles.contains_key("thread-2"));
        assert!(!titles.contains_key("thread-3"));
    }

    #[test]
    fn load_thread_titles_from_state_db_keeps_title_when_first_user_message_null() {
        let temp = tempdir().expect("tempdir");
        let db_path = temp.path().join(CODEX_STATE_DB_FILENAME);
        let conn = Connection::open(&db_path).expect("open sqlite db");
        // Codex stores first_user_message as a nullable column (Option<String>);
        // a renamed thread can have a title before any first message is synced.
        conn.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT NOT NULL, first_user_message TEXT)",
            [],
        )
        .expect("create threads table");
        conn.execute(
            "INSERT INTO threads (id, title, first_user_message) VALUES (?1, ?2, NULL)",
            ("thread-1", "Renamed thread"),
        )
        .expect("insert renamed thread without first message");
        conn.execute(
            "INSERT INTO threads (id, title, first_user_message) VALUES (?1, ?2, ?3)",
            ("thread-2", "First prompt", "First prompt"),
        )
        .expect("insert first-message title");
        drop(conn);

        let titles = load_thread_titles_from_db(&db_path);

        // Kept: title present and no first message to compare against.
        assert_eq!(
            titles.get("thread-1").map(String::as_str),
            Some("Renamed thread")
        );
        // Filtered: title equals the first user message.
        assert!(!titles.contains_key("thread-2"));
    }

    #[test]
    fn load_thread_titles_from_session_index_uses_latest_name() {
        let temp = tempdir().expect("tempdir");
        let index_path = temp.path().join(CODEX_SESSION_INDEX_FILENAME);
        std::fs::write(
            &index_path,
            concat!(
                "{\"id\":\"thread-1\",\"thread_name\":\"Old name\",\"updated_at\":\"2026-07-01T00:00:00Z\"}\n",
                "{\"id\":\"thread-2\",\"thread_name\":\"   \",\"updated_at\":\"2026-07-01T00:00:00Z\"}\n",
                "not json\n",
                "{\"id\":\"thread-1\",\"thread_name\":\"  New name  \",\"updated_at\":\"2026-07-02T00:00:00Z\"}\n"
            ),
        )
        .expect("write session index");

        let titles = load_thread_titles_from_session_index(&index_path);

        assert_eq!(titles.get("thread-1").map(String::as_str), Some("New name"));
        assert!(!titles.contains_key("thread-2"));
    }

    #[test]
    fn load_thread_titles_prefers_state_db_explicit_title_over_session_index() {
        let temp = tempdir().expect("tempdir");
        let index_path = temp.path().join(CODEX_SESSION_INDEX_FILENAME);
        std::fs::write(
            &index_path,
            concat!(
                "{\"id\":\"thread-1\",\"thread_name\":\"Legacy name\",\"updated_at\":\"2026-07-01T00:00:00Z\"}\n",
                "{\"id\":\"thread-2\",\"thread_name\":\"Legacy fallback\",\"updated_at\":\"2026-07-01T00:00:00Z\"}\n"
            ),
        )
        .expect("write session index");

        let db_path = temp.path().join(CODEX_STATE_DB_FILENAME);
        let conn = Connection::open(&db_path).expect("open sqlite db");
        conn.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT NOT NULL, first_user_message TEXT NOT NULL)",
            [],
        )
        .expect("create threads table");
        conn.execute(
            "INSERT INTO threads (id, title, first_user_message) VALUES (?1, ?2, ?3)",
            ("thread-1", "SQLite name", "First prompt"),
        )
        .expect("insert sqlite title");
        conn.execute(
            "INSERT INTO threads (id, title, first_user_message) VALUES (?1, ?2, ?3)",
            ("thread-2", "First prompt", "First prompt"),
        )
        .expect("insert first-message sqlite title");
        drop(conn);

        let titles = load_thread_titles_from_paths(&index_path, &[db_path]);

        assert_eq!(
            titles.get("thread-1").map(String::as_str),
            Some("SQLite name")
        );
        assert_eq!(
            titles.get("thread-2").map(String::as_str),
            Some("Legacy fallback")
        );
    }

    #[test]
    fn parse_session_skips_agents_md_injection() {
        let temp = tempdir().expect("tempdir");
        let path = temp.path().join("session.jsonl");
        std::fs::write(
            &path,
            concat!(
                "{\"timestamp\":\"2026-03-06T21:50:12Z\",\"type\":\"session_meta\",\"payload\":{\"id\":\"test-id\",\"cwd\":\"/tmp/project\"}}\n",
                "{\"timestamp\":\"2026-03-06T21:50:13Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"developer\",\"content\":\"<permissions>\"}}\n",
                "{\"timestamp\":\"2026-03-06T21:50:13Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"user\",\"content\":\"# AGENTS.md instructions for /tmp/project\\n<INSTRUCTIONS>Do stuff</INSTRUCTIONS>\"}}\n",
                "{\"timestamp\":\"2026-03-06T21:50:14Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"user\",\"content\":\"Fix the login bug\"}}\n"
            ),
        )
        .expect("write");

        let meta = parse_session(&path).unwrap();
        // Should skip AGENTS.md injection and use the real user message
        assert_eq!(meta.title.as_deref(), Some("Fix the login bug"));
    }

    #[test]
    fn parse_session_skips_subagent_sessions() {
        let temp = tempdir().expect("tempdir");
        let path = temp.path().join("session.jsonl");
        std::fs::write(
            &path,
            concat!(
                "{\"timestamp\":\"2026-04-28T10:00:00Z\",\"type\":\"session_meta\",\"payload\":{\"id\":\"subagent-id\",\"cwd\":\"/tmp/project\",\"originator\":\"codex-tui\",\"source\":{\"subagent\":{\"thread_spawn\":{\"parent_thread_id\":\"parent-id\",\"depth\":1,\"agent_role\":\"explorer\"}}}}}\n",
                "{\"timestamp\":\"2026-04-28T10:00:01Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"user\",\"content\":\"Inspect the project\"}}\n"
            ),
        )
        .expect("write");

        assert!(parse_session(&path).is_none());
    }

    #[test]
    fn parse_session_skips_environment_context_injection() {
        let temp = tempdir().expect("tempdir");
        let path = temp.path().join("session.jsonl");
        std::fs::write(
            &path,
            concat!(
                "{\"timestamp\":\"2026-03-06T21:50:12Z\",\"type\":\"session_meta\",\"payload\":{\"id\":\"test-id\",\"cwd\":\"/tmp/project\"}}\n",
                "{\"timestamp\":\"2026-03-06T21:50:13Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"user\",\"content\":\"<environment_context>\\n  <cwd>/tmp/project</cwd>\\n</environment_context>\"}}\n",
                "{\"timestamp\":\"2026-03-06T21:50:14Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"user\",\"content\":\"Fix the login bug\"}}\n"
            ),
        )
        .expect("write");

        let meta = parse_session(&path).unwrap();
        // Should skip environment_context injection and use the real user message
        assert_eq!(meta.title.as_deref(), Some("Fix the login bug"));
    }

    #[test]
    fn parse_session_extracts_vscode_ide_request_as_title() {
        let temp = tempdir().expect("tempdir");
        let path = temp.path().join("session.jsonl");
        std::fs::write(
            &path,
            concat!(
                "{\"timestamp\":\"2026-03-06T21:50:12Z\",\"type\":\"session_meta\",\"payload\":{\"id\":\"test-id\",\"cwd\":\"/tmp/project\"}}\n",
                "{\"timestamp\":\"2026-03-06T21:50:13Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"user\",\"content\":\"# Context from my IDE setup:\\n\\n## Active file: src/main.ts\\n\\n## My request for Codex:\\nFix the session title preview\"}}\n"
            ),
        )
        .expect("write");

        let meta = parse_session(&path).unwrap();
        assert_eq!(meta.title.as_deref(), Some("Fix the session title preview"));
    }

    #[test]
    fn parse_session_extracts_inline_vscode_ide_request_as_title() {
        let temp = tempdir().expect("tempdir");
        let path = temp.path().join("session.jsonl");
        std::fs::write(
            &path,
            concat!(
                "{\"timestamp\":\"2026-03-06T21:50:12Z\",\"type\":\"session_meta\",\"payload\":{\"id\":\"test-id\",\"cwd\":\"/tmp/project\"}}\n",
                "{\"timestamp\":\"2026-03-06T21:50:13Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"user\",\"content\":\"# Context from my IDE setup:\\n\\n## My request for Codex: Fix the TOC preview\"}}\n"
            ),
        )
        .expect("write");

        let meta = parse_session(&path).unwrap();
        assert_eq!(meta.title.as_deref(), Some("Fix the TOC preview"));
    }

    #[test]
    fn parse_session_ignores_marker_mentions_before_request_heading() {
        let temp = tempdir().expect("tempdir");
        let path = temp.path().join("session.jsonl");
        std::fs::write(
            &path,
            concat!(
                "{\"timestamp\":\"2026-03-06T21:50:12Z\",\"type\":\"session_meta\",\"payload\":{\"id\":\"test-id\",\"cwd\":\"/tmp/project\"}}\n",
                "{\"timestamp\":\"2026-03-06T21:50:13Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"user\",\"content\":\"# Context from my IDE setup:\\n\\n## Active selection:\\nMy request for Codex: not the prompt\\n\\n## My request for Codex:\\nUse the real request heading\"}}\n"
            ),
        )
        .expect("write");

        let meta = parse_session(&path).unwrap();
        assert_eq!(meta.title.as_deref(), Some("Use the real request heading"));
    }

    #[test]
    fn parse_session_uses_last_request_heading_when_selection_has_one() {
        let temp = tempdir().expect("tempdir");
        let path = temp.path().join("session.jsonl");
        std::fs::write(
            &path,
            concat!(
                "{\"timestamp\":\"2026-03-06T21:50:12Z\",\"type\":\"session_meta\",\"payload\":{\"id\":\"test-id\",\"cwd\":\"/tmp/project\"}}\n",
                "{\"timestamp\":\"2026-03-06T21:50:13Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"user\",\"content\":\"# Context from my IDE setup:\\n\\n## Active selection: docs/codex-format.md\\n## My request for Codex:\\nselected document content, not the real request\\n\\n## My request for Codex:\\nUse the last request heading\"}}\n"
            ),
        )
        .expect("write");

        let meta = parse_session(&path).unwrap();
        assert_eq!(meta.title.as_deref(), Some("Use the last request heading"));
    }

    // Known limitation: the IDE marker is matched purely by text, so a
    // "## My request for Codex:" line inside the real request body is treated as
    // a new boundary and only the trailing part is kept. This pins the
    // best-effort behavior; fully fixing it needs structured IDE section data
    // that the Codex VS Code context does not provide.
    #[test]
    fn parse_session_keeps_trailing_part_when_request_body_repeats_heading() {
        let temp = tempdir().expect("tempdir");
        let path = temp.path().join("session.jsonl");
        std::fs::write(
            &path,
            concat!(
                "{\"timestamp\":\"2026-03-06T21:50:12Z\",\"type\":\"session_meta\",\"payload\":{\"id\":\"test-id\",\"cwd\":\"/tmp/project\"}}\n",
                "{\"timestamp\":\"2026-03-06T21:50:13Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"user\",\"content\":\"# Context from my IDE setup:\\n\\n## Active file: foo.ts\\n\\n## My request for Codex:\\nDocument the format, for example:\\n## My request for Codex:\\nand the rest follows.\"}}\n"
            ),
        )
        .expect("write");

        let meta = parse_session(&path).unwrap();
        assert_eq!(meta.title.as_deref(), Some("and the rest follows."));
    }

    #[test]
    fn parse_session_skips_vscode_ide_context_without_request() {
        let temp = tempdir().expect("tempdir");
        let path = temp.path().join("session.jsonl");
        std::fs::write(
            &path,
            concat!(
                "{\"timestamp\":\"2026-03-06T21:50:12Z\",\"type\":\"session_meta\",\"payload\":{\"id\":\"test-id\",\"cwd\":\"/tmp/project\"}}\n",
                "{\"timestamp\":\"2026-03-06T21:50:13Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"user\",\"content\":\"# Context from my IDE setup:\\n\\n## Active file: src/main.ts\"}}\n",
                "{\"timestamp\":\"2026-03-06T21:50:14Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"user\",\"content\":\"Fix the login bug\"}}\n"
            ),
        )
        .expect("write");

        let meta = parse_session(&path).unwrap();
        assert_eq!(meta.title.as_deref(), Some("Fix the login bug"));
    }

    #[test]
    fn parse_session_falls_back_to_dir_basename() {
        let temp = tempdir().expect("tempdir");
        let path = temp.path().join("session.jsonl");
        std::fs::write(
            &path,
            concat!(
                "{\"timestamp\":\"2026-03-06T21:50:12Z\",\"type\":\"session_meta\",\"payload\":{\"id\":\"test-id\",\"cwd\":\"/tmp/my-project\"}}\n",
                "{\"timestamp\":\"2026-03-06T21:50:13Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"assistant\",\"content\":\"Hello\"}}\n"
            ),
        )
        .expect("write");

        let meta = parse_session(&path).unwrap();
        // No user message → falls back to dir basename
        assert_eq!(meta.title.as_deref(), Some("my-project"));
    }

    #[test]
    fn parse_session_truncates_long_title() {
        let temp = tempdir().expect("tempdir");
        let path = temp.path().join("session.jsonl");
        let long_msg = "a".repeat(200);
        std::fs::write(
            &path,
            format!(
                "{{\"timestamp\":\"2026-03-06T21:50:12Z\",\"type\":\"session_meta\",\"payload\":{{\"id\":\"test-id\",\"cwd\":\"/tmp/p\"}}}}\n\
                 {{\"timestamp\":\"2026-03-06T21:50:13Z\",\"type\":\"response_item\",\"payload\":{{\"type\":\"message\",\"role\":\"user\",\"content\":\"{long_msg}\"}}}}\n",
            ),
        )
        .expect("write");

        let meta = parse_session(&path).unwrap();
        let title = meta.title.unwrap();
        assert!(title.len() <= TITLE_MAX_CHARS + 3); // +3 for "..."
        assert!(title.ends_with("..."));
    }

    #[test]
    fn load_messages_includes_function_call_and_output() {
        let temp = tempdir().expect("tempdir");
        let path = temp.path().join("session.jsonl");
        std::fs::write(
            &path,
            concat!(
                "{\"timestamp\":\"2026-03-06T21:50:12Z\",\"type\":\"session_meta\",\"payload\":{\"id\":\"test-id\",\"cwd\":\"/tmp\"}}\n",
                "{\"timestamp\":\"2026-03-06T21:50:13Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"user\",\"content\":\"list files\"}}\n",
                "{\"timestamp\":\"2026-03-06T21:50:14Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"function_call\",\"name\":\"shell\",\"arguments\":\"{\\\"cmd\\\":[\\\"ls\\\"]}\",\"call_id\":\"call_1\"}}\n",
                "{\"timestamp\":\"2026-03-06T21:50:15Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"function_call_output\",\"call_id\":\"call_1\",\"output\":\"file1.txt\\nfile2.txt\"}}\n",
                "{\"timestamp\":\"2026-03-06T21:50:16Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"assistant\",\"content\":[{\"type\":\"output_text\",\"text\":\"Done.\"}]}}\n",
            ),
        )
        .expect("write");

        let msgs = load_messages(&path).expect("load");
        assert_eq!(msgs.len(), 4);

        assert_eq!(msgs[0].role, "user");
        assert_eq!(msgs[0].content, "list files");

        assert_eq!(msgs[1].role, "assistant");
        assert!(msgs[1].content.contains("[Tool: shell]"));

        assert_eq!(msgs[2].role, "tool");
        assert!(msgs[2].content.contains("file1.txt"));

        assert_eq!(msgs[3].role, "assistant");
        assert_eq!(msgs[3].content, "Done.");
    }
}
