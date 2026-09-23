//! 会话正文全文检索
//!
//! 会话列表页的元数据与聊天正文都支持两种匹配：
//! - 精准：完整短语的大小写不敏感子串匹配；
//! - 模糊：在精准匹配之外，允许标点/空格差异、分散关键词，以及较长
//!   查询的有序双字片段近似匹配。
//!
//! 正文按 mtime + size 缓存在内存中。SQLite 来源无法用 mtime 判断，
//! 使用 60 秒 TTL。正文完整搜索，缓存按总字节数和最近使用时间淘汰，
//! 超大会话使用进程期匿名临时索引，避免每次输入重新解析 JSON。

use std::collections::HashMap;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock, Weak};
use std::time::{Duration, Instant, SystemTime};

use serde::{Deserialize, Serialize};

use super::load_messages;

/// 缓存限制不影响正文检索范围；超出单条预算的正文照常完整搜索。
const MAX_CACHE_BYTES: usize = 64 * 1024 * 1024;
const MAX_CACHE_ENTRY_BYTES: usize = 8 * 1024 * 1024;
const MAX_CACHE_ENTRIES: usize = 512;
/// SQLite 来源的缓存有效期。
const SQLITE_TTL: Duration = Duration::from_secs(60);
/// 片段前后各保留的字符数。
const SNIPPET_CONTEXT_CHARS: usize = 60;
/// 单会话最多统计的命中次数。
const MAX_MATCH_COUNT: usize = 99;
/// 并行解析的线程数上限。
const MAX_WORKERS: usize = 2;
const MAX_INDEX_BYTES: usize = 512 * 1024 * 1024;
const MAX_INDEX_ENTRIES: usize = 4096;
// Small histories can exhaust macOS's per-process descriptor limit long before
// the byte/entry budgets. Leave ample room for SQLite, app settings and sessions.
const MAX_INDEX_FILES: usize = 24;

#[derive(Clone)]
pub struct SearchRequest {
    cancelled: Arc<AtomicBool>,
}

impl SearchRequest {
    fn new() -> Self {
        Self {
            cancelled: Arc::new(AtomicBool::new(false)),
        }
    }

    pub fn is_cancelled(&self) -> bool {
        self.cancelled.load(Ordering::Relaxed)
    }
}

#[derive(Default)]
struct RequestControl {
    active: Option<(Option<String>, SearchRequest)>,
}

impl RequestControl {
    fn begin(&mut self, id: Option<String>) -> SearchRequest {
        if let Some((_, request)) = self.active.take() {
            request.cancelled.store(true, Ordering::Relaxed);
        }
        let request = SearchRequest::new();
        self.active = Some((id, request.clone()));
        request
    }

    fn cancel(&mut self, id: &str) {
        if let Some((Some(active_id), request)) = &self.active {
            if active_id == id {
                request.cancelled.store(true, Ordering::Relaxed);
            }
        }
    }
}

static REQUESTS: OnceLock<Mutex<RequestControl>> = OnceLock::new();

pub fn begin_request(id: Option<String>) -> SearchRequest {
    REQUESTS
        .get_or_init(|| Mutex::new(RequestControl::default()))
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .begin(id)
}

pub fn cancel_request(id: &str) {
    REQUESTS
        .get_or_init(|| Mutex::new(RequestControl::default()))
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .cancel(id);
}

#[derive(Debug, Clone, Copy, Default, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum SessionSearchMode {
    Exact,
    #[default]
    Fuzzy,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionRef {
    pub provider_id: String,
    pub session_id: String,
    pub source_path: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionSearchHit {
    pub provider_id: String,
    pub session_id: String,
    pub source_path: String,
    pub match_count: usize,
    pub score: usize,
    pub snippet: String,
}

struct CachedText {
    /// 保留文件 mtime 的原始精度，避免同毫秒、同长度修改继续命中旧正文。
    fingerprint: Option<(SystemTime, u64)>,
    loaded_at: Instant,
    last_used: Instant,
    /// 小写化后的正文（中文不受影响，英文实现大小写不敏感匹配）。
    text_lc: Arc<str>,
}

type CacheKey = (String, String);

struct TextCache {
    entries: HashMap<CacheKey, CachedText>,
    bytes: usize,
    max_bytes: usize,
    max_entry_bytes: usize,
    max_entries: usize,
}

impl Default for TextCache {
    fn default() -> Self {
        Self {
            entries: HashMap::new(),
            bytes: 0,
            max_bytes: MAX_CACHE_BYTES,
            max_entry_bytes: MAX_CACHE_ENTRY_BYTES,
            max_entries: MAX_CACHE_ENTRIES,
        }
    }
}

impl TextCache {
    fn remove(&mut self, key: &CacheKey) {
        if let Some(entry) = self.entries.remove(key) {
            self.bytes -= entry.text_lc.len();
        }
    }

    fn get_fresh(
        &mut self,
        key: &CacheKey,
        fingerprint: Option<(SystemTime, u64)>,
        is_sqlite: bool,
    ) -> Option<Arc<str>> {
        if let Some(entry) = self.entries.get_mut(key) {
            let fresh = if is_sqlite {
                entry.loaded_at.elapsed() < SQLITE_TTL
            } else {
                entry.fingerprint == fingerprint && fingerprint.is_some()
            };
            if fresh {
                entry.last_used = Instant::now();
                return Some(Arc::clone(&entry.text_lc));
            }
        }
        self.remove(key);
        None
    }

    fn insert(&mut self, key: CacheKey, entry: CachedText) {
        self.remove(&key);
        let bytes = entry.text_lc.len();
        if bytes > self.max_entry_bytes || bytes > self.max_bytes || self.max_entries == 0 {
            return;
        }
        while self.bytes.saturating_add(bytes) > self.max_bytes
            || self.entries.len() >= self.max_entries
        {
            let Some(oldest) = self
                .entries
                .iter()
                .min_by_key(|(_, cached)| cached.last_used)
                .map(|(key, _)| key.clone())
            else {
                break;
            };
            self.remove(&oldest);
        }
        self.bytes += bytes;
        self.entries.insert(key, entry);
    }
}

static CACHE: OnceLock<Mutex<TextCache>> = OnceLock::new();

fn cache() -> &'static Mutex<TextCache> {
    CACHE.get_or_init(|| Mutex::new(TextCache::default()))
}

fn file_fingerprint(path: &str) -> Option<(SystemTime, u64)> {
    let meta = std::fs::metadata(Path::new(path)).ok()?;
    Some((meta.modified().ok()?, meta.len()))
}

/// A Bloom filter can reject an impossible exact phrase, but never confirms a
/// hit. All candidates still go through the original full-text matcher.
struct TextSynopsis {
    bits: Box<[u64; 1024]>,
}

impl TextSynopsis {
    fn new(text: &str) -> Self {
        let mut synopsis = Self {
            bits: Box::new([0; 1024]),
        };
        for gram in text.as_bytes().windows(3) {
            let hash = Self::hash(gram);
            synopsis.bits[(hash as usize >> 6) & 1023] |= 1 << (hash & 63);
        }
        synopsis
    }

    fn hash(gram: &[u8]) -> u32 {
        let value = (u32::from(gram[0]) << 16) | (u32::from(gram[1]) << 8) | u32::from(gram[2]);
        value.wrapping_mul(0x9e37_79b1).rotate_left(13)
    }

    fn might_match(&self, query: &str) -> bool {
        query.as_bytes().windows(3).all(|gram| {
            let hash = Self::hash(gram);
            self.bits[(hash as usize >> 6) & 1023] & (1 << (hash & 63)) != 0
        })
    }
}

struct IndexedText {
    fingerprint: Option<(SystemTime, u64)>,
    loaded_at: Instant,
    last_used: Instant,
    synopsis: TextSynopsis,
    // tempfile() creates a private, unnamed/delete-on-close file. No chat text
    // is written to the project, provider directory, or a persistent index.
    file: Option<Arc<Mutex<File>>>,
    bytes: usize,
}

#[derive(Default)]
struct TextIndex {
    entries: HashMap<CacheKey, IndexedText>,
    bytes: usize,
    files: usize,
    // Invalidated/deleted entries can still have an in-flight reader. Count
    // their file until the final reader drops it, even outside the map.
    retired: Vec<(Weak<Mutex<File>>, usize)>,
}

impl TextIndex {
    fn remove(&mut self, key: &CacheKey) {
        if let Some(entry) = self.entries.remove(key) {
            if let Some(file) = entry.file {
                if Arc::strong_count(&file) == 1 {
                    self.bytes -= entry.bytes;
                    self.files -= 1;
                } else {
                    self.retired.push((Arc::downgrade(&file), entry.bytes));
                }
            }
        }
    }

    fn reserve_file(&mut self, bytes: usize) -> bool {
        let allocated = &mut self.bytes;
        let files = &mut self.files;
        self.retired.retain(|(file, bytes)| {
            if file.strong_count() == 0 {
                *allocated -= bytes;
                *files -= 1;
                false
            } else {
                true
            }
        });
        if bytes > MAX_INDEX_BYTES {
            return false;
        }
        while self.bytes.saturating_add(bytes) > MAX_INDEX_BYTES || self.files >= MAX_INDEX_FILES {
            let Some(oldest) = self
                .entries
                .values_mut()
                .filter(|entry| {
                    entry
                        .file
                        .as_ref()
                        .is_some_and(|file| Arc::strong_count(file) == 1)
                })
                .min_by_key(|entry| entry.last_used)
            else {
                return false;
            };
            // Keep its cheap synopsis even when the disk payload is evicted.
            // A file currently being read cannot be evicted: its live handle
            // would otherwise keep consuming space outside the budget.
            oldest.file = None;
            self.bytes -= oldest.bytes;
            self.files -= 1;
        }
        true
    }

    fn insert(&mut self, key: CacheKey, mut entry: IndexedText) {
        self.remove(&key);
        while self.entries.len() >= MAX_INDEX_ENTRIES {
            let oldest = self
                .entries
                .iter()
                .min_by_key(|(_, entry)| entry.last_used)
                .map(|(key, _)| key.clone());
            if let Some(oldest) = oldest {
                self.remove(&oldest);
            } else {
                break;
            }
        }
        if entry.file.is_some() && !self.reserve_file(entry.bytes) {
            entry.file = None;
        }
        if entry.file.is_some() {
            self.bytes += entry.bytes;
            self.files += 1;
        }
        self.entries.insert(key, entry);
    }

    // The caller keeps the index lock from reservation through insertion.
    // Failed creation/writes close the temporary file and consume no budget.
    fn prepare_file(
        &mut self,
        text: &str,
        create_file: impl FnOnce() -> std::io::Result<File>,
    ) -> Option<Arc<Mutex<File>>> {
        if !self.reserve_file(text.len()) {
            return None;
        }
        let mut file = create_file().ok()?;
        file.write_all(text.as_bytes()).ok()?;
        Some(Arc::new(Mutex::new(file)))
    }
}

static INDEX: OnceLock<Mutex<TextIndex>> = OnceLock::new();

fn index() -> &'static Mutex<TextIndex> {
    INDEX.get_or_init(|| Mutex::new(TextIndex::default()))
}

enum IndexLookup {
    Miss,
    Stale,
    Impossible,
    File(Arc<Mutex<File>>),
}

fn lookup_index(
    key: &CacheKey,
    fingerprint: Option<(SystemTime, u64)>,
    is_sqlite: bool,
    query: &str,
    mode: SessionSearchMode,
) -> IndexLookup {
    let Ok(mut index) = index().lock() else {
        return IndexLookup::Miss;
    };
    if let Some(entry) = index.entries.get_mut(key) {
        let fresh = if is_sqlite {
            entry.loaded_at.elapsed() < SQLITE_TTL
        } else {
            fingerprint.is_some() && fingerprint == entry.fingerprint
        };
        if fresh {
            entry.last_used = Instant::now();
            if mode == SessionSearchMode::Exact && !entry.synopsis.might_match(query) {
                return IndexLookup::Impossible;
            }
            return entry
                .file
                .as_ref()
                .map(|file| IndexLookup::File(Arc::clone(file)))
                .unwrap_or(IndexLookup::Miss);
        }
    }
    let stale = index.entries.contains_key(key);
    index.remove(key);
    if stale {
        IndexLookup::Stale
    } else {
        IndexLookup::Miss
    }
}

fn store_index(key: CacheKey, fingerprint: Option<(SystemTime, u64)>, text: &str) {
    let synopsis = TextSynopsis::new(text);
    let now = Instant::now();
    if let Ok(mut index) = index().lock() {
        index.remove(&key);
        // Reserve before writing, so even temporary writes respect the disk
        // budget. The two search workers cannot over-allocate concurrently.
        let file = index.prepare_file(text, tempfile::tempfile);
        index.insert(
            key,
            IndexedText {
                fingerprint,
                loaded_at: now,
                last_used: now,
                synopsis,
                file,
                bytes: text.len(),
            },
        );
    }
}

/// 把会话消息拼成一段可检索的纯文本（小写）。
fn build_text(provider_id: &str, source_path: &str) -> Option<String> {
    #[cfg(test)]
    {
        let mut counts = PARSE_COUNTS
            .get_or_init(|| Mutex::new(HashMap::new()))
            .lock()
            .unwrap();
        *counts
            .entry((provider_id.to_string(), source_path.to_string()))
            .or_default() += 1;
    }
    let messages = load_messages(provider_id, source_path).ok()?;
    let mut text = String::new();
    for message in messages {
        text.push_str(&message.content.to_lowercase());
        text.push('\n');
    }
    Some(text)
}

#[cfg(test)]
static PARSE_COUNTS: OnceLock<Mutex<HashMap<CacheKey, usize>>> = OnceLock::new();

/// 共享完整正文，搜索期间不持有全局锁，也不复制整个缓存字符串。
fn fresh_text(
    item: &SessionRef,
    query: &str,
    mode: SessionSearchMode,
    request: &SearchRequest,
) -> Option<Arc<str>> {
    if request.is_cancelled() {
        return None;
    }
    let is_sqlite = item.source_path.starts_with("sqlite:");
    let fingerprint = if is_sqlite {
        None
    } else {
        file_fingerprint(&item.source_path)
    };

    let key = (item.provider_id.clone(), item.source_path.clone());
    let indexed = lookup_index(&key, fingerprint, is_sqlite, query, mode);
    if matches!(indexed, IndexLookup::Impossible) {
        return None;
    }
    if let Ok(mut cache) = cache().lock() {
        if matches!(indexed, IndexLookup::Stale) {
            cache.remove(&key);
        }
        if let Some(text) = cache.get_fresh(&key, fingerprint, is_sqlite) {
            return Some(text);
        }
    }

    let disk_text = if let IndexLookup::File(file) = indexed {
        file.lock().ok().and_then(|mut file| {
            file.seek(SeekFrom::Start(0)).ok()?;
            let mut text = String::new();
            file.read_to_string(&mut text).ok()?;
            Some(text)
        })
    } else {
        None
    };
    let text_lc: Arc<str> = if let Some(text) = disk_text {
        text.into()
    } else {
        let text = build_text(&item.provider_id, &item.source_path)?;
        // Preserve work already paid for if a new keystroke arrived during a
        // provider's synchronous parse; its next query can reuse this index.
        store_index(key.clone(), fingerprint, &text);
        text.into()
    };
    if let Ok(mut cache) = cache().lock() {
        let now = Instant::now();
        cache.insert(
            key,
            CachedText {
                fingerprint,
                loaded_at: now,
                last_used: now,
                text_lc: Arc::clone(&text_lc),
            },
        );
    }
    Some(text_lc)
}

const EXACT_READ_CHUNK: usize = 64 * 1024;

/// Scan normalized UTF-8 without allocating the whole indexed conversation.
/// Keep an overlap large enough for any needle, including needles larger than
/// one read block. The global cursor preserves non-overlapping match counts.
fn exact_matches_in_file(
    file: &mut File,
    needle: &str,
    request: &SearchRequest,
) -> std::io::Result<Option<(usize, String)>> {
    if needle.is_empty() {
        return Ok(None);
    }
    file.seek(SeekFrom::Start(0))?;
    let mut buffer = Vec::new();
    let mut chunk = vec![0; EXACT_READ_CHUNK];
    let mut offset = 0usize;
    let mut cursor = 0usize;
    let mut first = None;
    let mut count = 0usize;
    loop {
        if request.is_cancelled() {
            return Ok(None);
        }
        let read = file.read(&mut chunk)?;
        buffer.extend_from_slice(&chunk[..read]);
        let end = match std::str::from_utf8(&buffer) {
            Ok(_) => buffer.len(),
            Err(error) if error.error_len().is_none() && read != 0 => error.valid_up_to(),
            Err(error) => return Err(std::io::Error::new(std::io::ErrorKind::InvalidData, error)),
        };
        let text = std::str::from_utf8(&buffer[..end])
            .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
        while cursor.saturating_sub(offset) <= end {
            let Some(relative) = text[cursor.saturating_sub(offset)..].find(needle) else {
                break;
            };
            let position = cursor + relative;
            first.get_or_insert(position);
            count += 1;
            cursor = position + needle.len();
            if count == MAX_MATCH_COUNT {
                break;
            }
        }
        if read == 0 || count == MAX_MATCH_COUNT {
            break;
        }
        let mut discard = end.saturating_sub(needle.len().saturating_sub(1));
        while !text.is_char_boundary(discard) {
            discard -= 1;
        }
        buffer.drain(..discard);
        offset += discard;
        cursor = cursor.max(offset);
    }
    let Some(first) = first else {
        return Ok(None);
    };
    if request.is_cancelled() {
        return Ok(None);
    }
    let file_len = file.metadata()?.len() as usize;
    let context_bytes = SNIPPET_CONTEXT_CHARS * 4;
    let start = first.saturating_sub(context_bytes);
    let end = (first + needle.len())
        .saturating_add(context_bytes)
        .min(file_len);
    let mut bytes = vec![0; end - start];
    file.seek(SeekFrom::Start(start as u64))?;
    file.read_exact(&mut bytes)?;
    let leading = bytes
        .iter()
        .take_while(|byte| **byte & 0xc0 == 0x80)
        .count();
    let valid_end = match std::str::from_utf8(&bytes[leading..]) {
        Ok(_) => bytes.len(),
        Err(error) if error.error_len().is_none() => leading + error.valid_up_to(),
        Err(error) => return Err(std::io::Error::new(std::io::ErrorKind::InvalidData, error)),
    };
    let text = std::str::from_utf8(&bytes[leading..valid_end])
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
    let relative = first - start - leading;
    let mut snippet = snippet_around(text, relative, needle.len());
    if start + leading > 0 && text[..relative].chars().count() <= SNIPPET_CONTEXT_CHARS {
        snippet.insert(0, '…');
    }
    if start + valid_end < file_len
        && text[relative + needle.len()..].chars().count() <= SNIPPET_CONTEXT_CHARS
    {
        snippet.push('…');
    }
    Ok(Some((count, snippet)))
}

/// Outer None asks the caller to parse/rebuild an absent or unreadable index.
fn indexed_exact_match(
    item: &SessionRef,
    needle: &str,
    request: &SearchRequest,
) -> Option<Option<(usize, String)>> {
    let is_sqlite = item.source_path.starts_with("sqlite:");
    let fingerprint = if is_sqlite {
        None
    } else {
        file_fingerprint(&item.source_path)
    };
    let key = (item.provider_id.clone(), item.source_path.clone());
    match lookup_index(
        &key,
        fingerprint,
        is_sqlite,
        needle,
        SessionSearchMode::Exact,
    ) {
        IndexLookup::Impossible => Some(None),
        IndexLookup::File(file) => {
            if let Ok(mut cache) = cache().lock() {
                if let Some(text) = cache.get_fresh(&key, fingerprint, is_sqlite) {
                    drop(cache);
                    return Some(find_exact_matches(&text, needle));
                }
            }
            let mut file = file.lock().ok()?;
            exact_matches_in_file(&mut file, needle, request).ok()
        }
        IndexLookup::Stale => {
            if let Ok(mut cache) = cache().lock() {
                cache.remove(&key);
            }
            None
        }
        IndexLookup::Miss => None,
    }
}

fn count_occurrences(text: &str, needle: &str) -> usize {
    if needle.is_empty() {
        return 0;
    }

    let mut count = 0usize;
    let mut cursor = 0usize;
    while let Some(pos) = text[cursor..].find(needle) {
        count += 1;
        cursor += pos + needle.len();
        if count >= MAX_MATCH_COUNT || cursor >= text.len() {
            break;
        }
    }
    count
}

fn snippet_around(text: &str, first: usize, match_len: usize) -> String {
    let mut start = first;
    let mut taken = 0usize;
    while start > 0 && taken < SNIPPET_CONTEXT_CHARS {
        start -= 1;
        while !text.is_char_boundary(start) {
            start -= 1;
        }
        taken += 1;
    }

    let mut end = first + match_len;
    taken = 0;
    while end < text.len() && taken < SNIPPET_CONTEXT_CHARS {
        end += 1;
        while end < text.len() && !text.is_char_boundary(end) {
            end += 1;
        }
        taken += 1;
    }

    let mut snippet: String = text[start..end]
        .chars()
        .map(|c| {
            if c == '\n' || c == '\r' || c == '\t' {
                ' '
            } else {
                c
            }
        })
        .collect();
    snippet = snippet.split_whitespace().collect::<Vec<_>>().join(" ");
    if start > 0 {
        snippet.insert(0, '…');
    }
    if end < text.len() {
        snippet.push('…');
    }
    snippet
}

/// 完整短语的大小写不敏感子串匹配。
pub(crate) fn find_exact_matches(text_lc: &str, needle_lc: &str) -> Option<(usize, String)> {
    let first = text_lc.find(needle_lc)?;
    let count = count_occurrences(text_lc, needle_lc);
    Some((count, snippet_around(text_lc, first, needle_lc.len())))
}

fn compact_search_text(text: &str) -> String {
    text.chars().filter(|ch| ch.is_alphanumeric()).collect()
}

fn split_search_terms(query: &str) -> Vec<&str> {
    query
        .split(|ch: char| !ch.is_alphanumeric())
        .filter(|term| !term.is_empty())
        .collect()
}

fn snippet_for_candidates(text: &str, candidates: &[&str]) -> String {
    for candidate in candidates {
        if let Some((_, snippet)) = find_exact_matches(text, candidate) {
            return snippet;
        }
    }

    let preview: String = text.chars().take(SNIPPET_CONTEXT_CHARS * 2).collect();
    if preview.chars().count() < text.chars().count() {
        format!("{preview}…")
    } else {
        preview
    }
}

fn ordered_bigram_match(text: &str, query: &str) -> Option<(usize, usize, Vec<String>)> {
    let query_chars: Vec<char> = query.chars().collect();
    if query_chars.len() < 4 {
        return None;
    }

    let bigrams: Vec<String> = query_chars
        .windows(2)
        .map(|window| window.iter().collect())
        .collect();
    let mut cursor = 0usize;
    let mut first = None;
    let mut last = 0usize;
    let mut matched = 0usize;
    let mut matched_bigrams = Vec::new();

    for bigram in &bigrams {
        let Some(relative) = text[cursor..].find(bigram) else {
            continue;
        };
        let absolute = cursor + relative;
        first.get_or_insert(absolute);
        last = absolute + bigram.len();
        matched += 1;
        matched_bigrams.push(bigram.clone());
        let advance = text[absolute..]
            .chars()
            .next()
            .map(char::len_utf8)
            .unwrap_or(1);
        cursor = absolute + advance;
    }

    let total = bigrams.len();
    if matched * 100 < total * 60 {
        return None;
    }

    let span = last.saturating_sub(first?);
    let max_span = query.len().saturating_mul(6).saturating_add(64);
    if span > max_span {
        return None;
    }

    Some((matched, total, matched_bigrams))
}

fn find_fuzzy_matches(text_lc: &str, needle_lc: &str) -> Option<(usize, usize, String)> {
    if let Some((count, snippet)) = find_exact_matches(text_lc, needle_lc) {
        return Some((count, 1_000_000 + count, snippet));
    }

    let compact_query = compact_search_text(needle_lc);
    if compact_query.is_empty() {
        return None;
    }
    let compact_text = compact_search_text(text_lc);

    if compact_text.contains(&compact_query) {
        let count = count_occurrences(&compact_text, &compact_query);
        let terms = split_search_terms(needle_lc);
        let snippet = snippet_for_candidates(text_lc, &terms);
        return Some((count, 900_000 + count, snippet));
    }

    let terms = split_search_terms(needle_lc);
    if terms.len() > 1 {
        let mut matched_terms = Vec::new();
        let mut match_count = 0usize;
        for term in &terms {
            let count = count_occurrences(text_lc, term);
            if count > 0 {
                matched_terms.push(*term);
                match_count = match_count.saturating_add(count);
            }
        }

        let required = (terms.len() * 3).div_ceil(5);
        if matched_terms.len() >= required {
            let snippet = snippet_for_candidates(text_lc, &matched_terms);
            let score = 700_000 + matched_terms.len() * 1_000 + match_count.min(999);
            return Some((match_count.min(MAX_MATCH_COUNT), score, snippet));
        }
    }

    let (matched, total, matched_bigrams) = ordered_bigram_match(&compact_text, &compact_query)?;
    let matched_refs: Vec<&str> = matched_bigrams.iter().map(String::as_str).collect();
    let snippet = snippet_for_candidates(text_lc, &matched_refs);
    let score = 400_000 + matched * 10_000 / total.max(1);
    Some((matched.min(MAX_MATCH_COUNT), score, snippet))
}

/// 在给定会话集合中检索正文。结果按相关性、命中次数排序。
#[cfg(test)]
pub fn search_contents(
    items: &[SessionRef],
    query: &str,
    limit: usize,
    mode: SessionSearchMode,
) -> Vec<SessionSearchHit> {
    search_contents_with_request(items, query, limit, mode, &SearchRequest::new())
        .unwrap_or_default()
}

pub fn search_contents_with_request(
    items: &[SessionRef],
    query: &str,
    limit: usize,
    mode: SessionSearchMode,
    request: &SearchRequest,
) -> Result<Vec<SessionSearchHit>, String> {
    if request.is_cancelled() {
        return Err("Session search cancelled".into());
    }
    let needle_lc = query.trim().to_lowercase();
    if needle_lc.is_empty() || items.is_empty() {
        return Ok(Vec::new());
    }

    let workers = MAX_WORKERS.min(items.len()).max(1);
    let chunk_size = items.len().div_ceil(workers);
    let mut hits: Vec<SessionSearchHit> = std::thread::scope(|scope| {
        let handles: Vec<_> = items
            .chunks(chunk_size)
            .map(|chunk| {
                let needle = needle_lc.clone();
                scope.spawn(move || {
                    let mut local = Vec::new();
                    for item in chunk {
                        if request.is_cancelled() {
                            break;
                        }
                        if mode == SessionSearchMode::Exact {
                            if let Some(matched) = indexed_exact_match(item, &needle, request) {
                                if let Some((match_count, snippet)) = matched {
                                    local.push(SessionSearchHit {
                                        provider_id: item.provider_id.clone(),
                                        session_id: item.session_id.clone(),
                                        source_path: item.source_path.clone(),
                                        match_count,
                                        score: 1_000_000 + match_count,
                                        snippet,
                                    });
                                }
                                continue;
                            }
                        }
                        let Some(text_lc) = fresh_text(item, &needle, mode, request) else {
                            continue;
                        };
                        if request.is_cancelled() {
                            break;
                        }
                        let matched = match mode {
                            SessionSearchMode::Exact => find_exact_matches(&text_lc, &needle)
                                .map(|(count, snippet)| (count, 1_000_000 + count, snippet)),
                            SessionSearchMode::Fuzzy => find_fuzzy_matches(&text_lc, &needle),
                        };
                        if let Some((match_count, score, snippet)) = matched {
                            local.push(SessionSearchHit {
                                provider_id: item.provider_id.clone(),
                                session_id: item.session_id.clone(),
                                source_path: item.source_path.clone(),
                                match_count,
                                score,
                                snippet,
                            });
                        }
                    }
                    local
                })
            })
            .collect();
        handles
            .into_iter()
            .filter_map(|handle| handle.join().ok())
            .flatten()
            .collect()
    });

    // A cancelled search is never reported as a complete partial result set.
    if request.is_cancelled() {
        return Err("Session search cancelled".into());
    }

    hits.sort_by(|a, b| {
        b.score
            .cmp(&a.score)
            .then_with(|| b.match_count.cmp(&a.match_count))
            .then_with(|| a.session_id.cmp(&b.session_id))
    });
    if limit > 0 && hits.len() > limit {
        hits.truncate(limit);
    }
    Ok(hits)
}

/// 清掉某个来源的缓存（删除会话时调用，避免命中已删除文件）。
pub fn evict(source_path: &str) {
    if let Ok(mut cache) = cache().lock() {
        let keys: Vec<_> = cache
            .entries
            .keys()
            .filter(|(_, path)| path == source_path)
            .cloned()
            .collect();
        for key in keys {
            cache.remove(&key);
        }
    }
    if let Ok(mut index) = index().lock() {
        let keys: Vec<_> = index
            .entries
            .keys()
            .filter(|(_, path)| path == source_path)
            .cloned()
            .collect();
        for key in keys {
            index.remove(&key);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::io::Write;

    fn append_codex_message(path: &Path, role: &str, content: &str) {
        let mut file = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .unwrap();
        writeln!(
            file,
            "{}",
            json!({"type":"response_item","payload":{"type":"message","role":role,"content":content}})
        )
        .unwrap();
    }

    fn session_ref(path: &Path, provider_id: &str) -> SessionRef {
        SessionRef {
            provider_id: provider_id.to_string(),
            session_id: "same-session".to_string(),
            source_path: path.to_string_lossy().to_string(),
        }
    }

    #[cfg(unix)]
    #[test]
    fn thousand_sessions_remain_searchable_under_low_fd_limit() {
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "session_manager::search::tests::low_fd_search_child",
                "--ignored",
                "--nocapture",
                "--test-threads=1",
            ])
            .env("AI_CODING_LOW_FD_TEST_CHILD", "1")
            .output()
            .unwrap();
        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr);
        println!("{stdout}{stderr}");
        assert!(
            output.status.success(),
            "isolated low-FD search regression failed"
        );
    }

    #[cfg(unix)]
    #[test]
    #[ignore = "Runs only inside the isolated low-FD subprocess test"]
    fn low_fd_search_child() {
        if std::env::var("AI_CODING_LOW_FD_TEST_CHILD").as_deref() != Ok("1") {
            return;
        }
        let dir = tempfile::tempdir().unwrap();
        let mut items = Vec::new();
        for number in 0..1000 {
            let path = dir.path().join(format!("synthetic-{number}.jsonl"));
            append_codex_message(&path, "user", "用户的完整精准问题");
            append_codex_message(&path, "assistant", "AI 的完整精准回复");
            let mut item = session_ref(&path, "codex");
            item.session_id = format!("synthetic-{number}");
            items.push(item);
        }
        let mut limits = libc::rlimit {
            rlim_cur: 0,
            rlim_max: 0,
        };
        // This test is launched in a dedicated subprocess. Never lower the
        // application's, parent test process's, or machine-wide descriptor limit.
        assert_eq!(
            unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, &mut limits) },
            0
        );
        limits.rlim_cur = limits.rlim_cur.min(128);
        assert_eq!(unsafe { libc::setrlimit(libc::RLIMIT_NOFILE, &limits) }, 0);
        let first = search_contents(&items, "用户的完整精准问题", 0, SessionSearchMode::Exact);
        let second = search_contents(&items, "AI 的完整精准回复", 0, SessionSearchMode::Exact);
        let mut probes = Vec::new();
        let mut open_error = None;
        for _ in 0..32 {
            match File::open(&items[0].source_path) {
                Ok(file) => probes.push(file),
                Err(error) => {
                    open_error = Some(error.to_string());
                    break;
                }
            }
        }
        let database_path = dir.path().join("probe.sqlite");
        let sqlite_result = rusqlite::Connection::open(&database_path).and_then(|connection| {
            connection.execute("CREATE TABLE probe (value TEXT NOT NULL)", [])?;
            connection.execute("INSERT INTO probe (value) VALUES ('still available')", [])?;
            drop(connection);
            let reopened = rusqlite::Connection::open(&database_path)?;
            reopened.query_row("SELECT value FROM probe", [], |row| row.get::<_, String>(0))
        });
        let settings_result = std::fs::write(
            dir.path().join("settings-probe.json"),
            "{\"synthetic\":true}",
        );
        let index = index().lock().unwrap();
        let descriptors = index
            .entries
            .values()
            .filter(|entry| entry.file.is_some())
            .count()
            + index
                .retired
                .iter()
                .filter(|(file, _)| file.strong_count() > 0)
                .count();
        println!("low_fd_limit={} sessions=1000 first_hits={} second_hits={} cached_descriptors={descriptors} probe_files={} open_error={open_error:?}", limits.rlim_cur, first.len(), second.len(), probes.len());
        println!(
            "sqlite_ok={} settings_write_ok={}",
            sqlite_result.is_ok(),
            settings_result.is_ok()
        );
        assert_eq!(first.len(), 1000);
        assert_eq!(second.len(), 1000);
        assert!(first
            .iter()
            .chain(second.iter())
            .all(|hit| hit.match_count == 1));
        assert!(
            descriptors <= 24,
            "search must retain at most 24 descriptors"
        );
        assert_eq!(
            probes.len(),
            32,
            "unrelated application files must still open"
        );
        assert_eq!(sqlite_result.unwrap(), "still available");
        settings_result.expect("unrelated settings files must remain writable");
    }

    fn parse_count(item: &SessionRef) -> usize {
        PARSE_COUNTS
            .get_or_init(|| Mutex::new(HashMap::new()))
            .lock()
            .unwrap()
            .get(&(item.provider_id.clone(), item.source_path.clone()))
            .copied()
            .unwrap_or(0)
    }

    #[test]
    fn newest_request_cancels_previous_work_and_old_cleanup_cannot_cancel_it() {
        let mut control = RequestControl::default();
        let first = control.begin(Some("first".into()));
        let second = control.begin(Some("second".into()));
        assert!(first.is_cancelled());
        assert!(!second.is_cancelled());
        control.cancel("first");
        assert!(!second.is_cancelled());
        control.cancel("second");
        assert!(second.is_cancelled());
    }

    #[test]
    fn cancelled_request_does_not_parse_or_return_partial_hits() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("cancelled.jsonl");
        append_codex_message(&path, "assistant", "完整答案");
        let item = session_ref(&path, "codex");
        let request = SearchRequest::new();
        request.cancelled.store(true, Ordering::Relaxed);
        let result = search_contents_with_request(
            std::slice::from_ref(&item),
            "完整答案",
            0,
            SessionSearchMode::Exact,
            &request,
        );
        assert_eq!(result.unwrap_err(), "Session search cancelled");
        assert_eq!(parse_count(&item), 0);
    }

    #[test]
    fn synopsis_never_rejects_a_stored_unicode_substring() {
        let text = "hello, 完整的AI答复😊\nİSTANBUL combining e\u{301}!".to_lowercase();
        let synopsis = TextSynopsis::new(&text);
        let mut boundaries: Vec<_> = text.char_indices().map(|(index, _)| index).collect();
        boundaries.push(text.len());
        for (index, start) in boundaries.iter().enumerate() {
            for end in &boundaries[index..] {
                assert!(synopsis.might_match(&text[*start..*end]));
            }
        }
        assert!(!synopsis.might_match("完全不存在的搜索词"));
    }

    fn streamed_match(text: &str, needle: &str) -> Option<(usize, String)> {
        let mut file = tempfile::tempfile().unwrap();
        file.write_all(text.as_bytes()).unwrap();
        exact_matches_in_file(&mut file, needle, &SearchRequest::new()).unwrap()
    }

    #[test]
    fn streaming_exact_preserves_unicode_counts_and_snippets_across_block_edges() {
        let needle = "跨块😊完整答案";
        for offset in EXACT_READ_CHUNK - 16..EXACT_READ_CHUNK + 4 {
            let text = format!(
                "{}中{needle}{}\n{needle}\t结尾",
                "a".repeat(offset),
                "上下文😊".repeat(80)
            );
            assert_eq!(
                streamed_match(&text, needle),
                find_exact_matches(&text, needle),
                "offset={offset}"
            );
        }
        for text in [
            format!("{}hello{}", "😊".repeat(100), "😊".repeat(100)),
            "简短hello结尾".into(),
        ] {
            assert_eq!(
                streamed_match(&text, "hello"),
                find_exact_matches(&text, "hello")
            );
        }
    }

    #[test]
    fn streaming_exact_supports_needles_larger_than_a_block_and_nonoverlapping_limits() {
        let needle = format!("开头{}尾部", "中".repeat(EXACT_READ_CHUNK));
        let text = format!("{}{}中{}", "😊".repeat(100), needle, needle);
        assert_eq!(
            streamed_match(&text, &needle),
            find_exact_matches(&text, &needle)
        );
        let text = format!("{}aaaaa", "x".repeat(EXACT_READ_CHUNK - 2));
        assert_eq!(
            streamed_match(&text, "aaa"),
            find_exact_matches(&text, "aaa")
        );
        let text = "a".repeat(EXACT_READ_CHUNK * 2);
        assert_eq!(
            streamed_match(&text, "aaa"),
            find_exact_matches(&text, "aaa")
        );
        assert_eq!(streamed_match(&text, "aaa").unwrap().0, MAX_MATCH_COUNT);
        assert!(streamed_match(&text, "没有命中").is_none());
    }

    #[test]
    fn streaming_exact_stops_cancelled_work_before_reading() {
        let request = SearchRequest::new();
        request.cancelled.store(true, Ordering::Relaxed);
        let mut file = tempfile::tempfile().unwrap();
        file.write_all("完整答案".as_bytes()).unwrap();
        assert!(exact_matches_in_file(&mut file, "完整答案", &request)
            .unwrap()
            .is_none());
        assert_eq!(file.stream_position().unwrap(), 0);
    }

    #[test]
    fn expired_sqlite_index_also_invalidates_a_more_recent_memory_copy() {
        let dir = tempfile::tempdir().unwrap();
        let item = SessionRef {
            provider_id: "opencode".into(),
            session_id: "ttl-test".into(),
            source_path: format!("sqlite:synthetic/{}", dir.path().display()),
        };
        let key = (item.provider_id.clone(), item.source_path.clone());
        store_index(key.clone(), None, "old answer");
        index()
            .lock()
            .unwrap()
            .entries
            .get_mut(&key)
            .unwrap()
            .loaded_at = Instant::now() - SQLITE_TTL - Duration::from_secs(1);
        cache().lock().unwrap().insert(
            key.clone(),
            CachedText {
                fingerprint: None,
                loaded_at: Instant::now(),
                last_used: Instant::now(),
                text_lc: Arc::from("old answer"),
            },
        );
        assert!(indexed_exact_match(&item, "old answer", &SearchRequest::new()).is_none());
        assert!(!cache().lock().unwrap().entries.contains_key(&key));
        assert!(!index().lock().unwrap().entries.contains_key(&key));
    }

    #[test]
    fn disk_budget_evicts_payload_but_keeps_filter_and_never_evicts_live_readers() {
        let mut index = TextIndex::default();
        let key = ("codex".into(), "budget".into());
        let file = Arc::new(Mutex::new(tempfile::tempfile().unwrap()));
        let reader = Arc::clone(&file);
        let now = Instant::now();
        index.insert(
            key.clone(),
            IndexedText {
                fingerprint: None,
                loaded_at: now,
                last_used: now,
                synopsis: TextSynopsis::new("original answer"),
                file: Some(file),
                bytes: MAX_INDEX_BYTES,
            },
        );
        assert_eq!(index.bytes, MAX_INDEX_BYTES);
        assert!(
            !index.reserve_file(1),
            "a live reader still occupies disk budget"
        );
        drop(reader);
        assert!(index.reserve_file(1));
        assert_eq!(index.bytes, 0);
        let entry = index.entries.get(&key).unwrap();
        assert!(entry.file.is_none());
        assert!(entry.synopsis.might_match("original answer"));
        assert!(!index.reserve_file(MAX_INDEX_BYTES + 1));

        let file = Arc::new(Mutex::new(tempfile::tempfile().unwrap()));
        let reader = Arc::clone(&file);
        index.insert(
            key.clone(),
            IndexedText {
                fingerprint: None,
                loaded_at: now,
                last_used: now,
                synopsis: TextSynopsis::new("updated answer"),
                file: Some(file),
                bytes: MAX_INDEX_BYTES,
            },
        );
        index.remove(&key);
        assert_eq!(index.bytes, MAX_INDEX_BYTES);
        assert!(
            !index.reserve_file(1),
            "removed entries with live readers still count"
        );
        drop(reader);
        assert!(index.reserve_file(1));
        assert_eq!(index.bytes, 0);
    }

    fn indexed_test_entry(text: &str, file: Option<Arc<Mutex<File>>>) -> IndexedText {
        let now = Instant::now();
        IndexedText {
            fingerprint: None,
            loaded_at: now,
            last_used: now,
            synopsis: TextSynopsis::new(text),
            file,
            bytes: text.len(),
        }
    }

    #[test]
    fn descriptor_budget_is_reserved_before_creation_and_keeps_synopses() {
        let mut index = TextIndex::default();
        for number in 0..1000 {
            let file = index.prepare_file("x", tempfile::tempfile).unwrap();
            assert!(
                index.files < MAX_INDEX_FILES,
                "one slot is available before creation"
            );
            index.insert(
                ("codex".into(), format!("{number}")),
                indexed_test_entry("x", Some(file)),
            );
            assert_eq!(index.files, (number + 1).min(MAX_INDEX_FILES));
            assert_eq!(index.bytes, index.files);
        }
        assert_eq!(index.entries.len(), 1000);
        assert_eq!(
            index
                .entries
                .values()
                .filter(|entry| entry.file.is_some())
                .count(),
            MAX_INDEX_FILES
        );
        assert!(index
            .entries
            .values()
            .all(|entry| entry.synopsis.might_match("x")));
        assert!(index
            .prepare_file("x", || Err(std::io::Error::other(
                "synthetic create failure"
            )))
            .is_none());
        assert_eq!(
            index.files,
            MAX_INDEX_FILES - 1,
            "failed creation never charges the reserved slot"
        );
        assert_eq!(index.bytes, MAX_INDEX_FILES - 1);
    }

    #[test]
    fn descriptor_budget_counts_retired_readers_and_replacement_until_they_close() {
        let mut index = TextIndex::default();
        let mut readers = Vec::new();
        for number in 0..MAX_INDEX_FILES {
            let file = index.prepare_file("x", tempfile::tempfile).unwrap();
            readers.push(Arc::clone(&file));
            index.insert(
                ("codex".into(), format!("{number}")),
                indexed_test_entry("x", Some(file)),
            );
        }
        let key = ("codex".into(), "0".into());
        index.remove(&key);
        assert_eq!(index.files, MAX_INDEX_FILES);
        assert_eq!(index.retired.len(), 1);
        let file = index.prepare_file("replacement", || {
            panic!("full budget must not open another file")
        });
        assert!(file.is_none());
        index.insert(key.clone(), indexed_test_entry("replacement", file));
        assert_eq!(index.files, MAX_INDEX_FILES);
        assert!(index.entries[&key].synopsis.might_match("replacement"));
        drop(readers.remove(0));
        let file = index.prepare_file("new", tempfile::tempfile).unwrap();
        assert_eq!(index.files, MAX_INDEX_FILES - 1);
        assert!(index.retired.is_empty());
        index.insert(key, indexed_test_entry("new", Some(file)));
        assert_eq!(index.files, MAX_INDEX_FILES);
        drop(readers);
        let keys: Vec<_> = index.entries.keys().cloned().collect();
        for key in keys {
            index.remove(&key);
        }
        assert_eq!(index.files, 0);
        assert_eq!(index.bytes, 0);
    }

    #[test]
    fn failed_index_creation_or_write_does_not_leak_descriptor_budget() {
        let mut index = TextIndex::default();
        let key = ("codex".into(), "kept".into());
        let file = index.prepare_file("kept", tempfile::tempfile).unwrap();
        index.insert(key.clone(), indexed_test_entry("kept", Some(file)));
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("read-only-handle");
        std::fs::write(&path, "fixture").unwrap();
        for _ in 0..100 {
            assert!(index
                .prepare_file("next", || Err(std::io::Error::other("synthetic failure")))
                .is_none());
            assert!(index.prepare_file("next", || File::open(&path)).is_none());
            assert_eq!(index.files, 1);
            assert_eq!(index.bytes, 4);
        }
        index.remove(&key);
        assert_eq!(index.files, 0);
        assert_eq!(index.bytes, 0);
        let file = index
            .prepare_file("replacement", tempfile::tempfile)
            .unwrap();
        index.insert(key, indexed_test_entry("replacement", Some(file)));
        assert_eq!(index.files, 1);
        assert_eq!(index.bytes, "replacement".len());
    }

    #[test]
    fn evicted_memory_uses_private_disk_index_without_reparsing() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("index.jsonl");
        append_codex_message(&path, "assistant", "first answer 和第二个完整答案");
        let item = session_ref(&path, "codex");
        let items = std::slice::from_ref(&item);
        assert_eq!(
            search_contents(items, "first answer", 0, SessionSearchMode::Exact).len(),
            1
        );
        assert_eq!(parse_count(&item), 1);
        let key = (item.provider_id.clone(), item.source_path.clone());
        cache().lock().unwrap().remove(&key);
        assert_eq!(
            search_contents(items, "第二个完整答案", 0, SessionSearchMode::Exact).len(),
            1
        );
        assert_eq!(parse_count(&item), 1);
        cache().lock().unwrap().remove(&key);
        {
            let mut index = index().lock().unwrap();
            let entry = index.entries.get_mut(&key).unwrap();
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let permissions = entry
                    .file
                    .as_ref()
                    .unwrap()
                    .lock()
                    .unwrap()
                    .metadata()
                    .unwrap()
                    .permissions();
                assert_eq!(permissions.mode() & 0o077, 0);
            }
            entry.file = None;
            let bytes = entry.bytes;
            index.bytes -= bytes;
            index.files -= 1;
        }
        assert!(search_contents(items, "不存在的查找词", 0, SessionSearchMode::Exact).is_empty());
        assert_eq!(
            parse_count(&item),
            1,
            "synopsis should work even after disk eviction"
        );
        // A candidate without a disk payload must still read full source text.
        assert_eq!(
            search_contents(items, "第二个 完整答案", 0, SessionSearchMode::Fuzzy).len(),
            1
        );
        assert_eq!(parse_count(&item), 2);
        evict(&item.source_path);
    }

    #[test]
    #[ignore = "Synthetic 216 MiB source benchmark; run explicitly with --ignored --nocapture"]
    fn benchmark_large_history_search() {
        let dir = tempfile::tempdir().unwrap();
        let filler = "x".repeat(36 * 1024 * 1024);
        let mut items = Vec::new();
        let mut source_bytes = 0;
        for number in 0..6 {
            let path = dir.path().join(format!("synthetic-{number}.jsonl"));
            append_codex_message(&path, "user", &filler);
            append_codex_message(&path, "assistant", "first unique answer; 第二个精确答案");
            source_bytes += path.metadata().unwrap().len();
            items.push(session_ref(&path, "codex"));
        }
        drop(filler);
        let cold = Instant::now();
        let first = search_contents(&items, "first unique answer", 0, SessionSearchMode::Exact);
        let cold = cold.elapsed();
        let warm = Instant::now();
        let second = search_contents(&items, "第二个精确答案", 0, SessionSearchMode::Exact);
        let warm = warm.elapsed();
        let negative = Instant::now();
        let missing = search_contents(&items, "从未出现的完整关键词", 0, SessionSearchMode::Exact);
        let negative = negative.elapsed();
        assert_eq!(first.len(), 6);
        assert_eq!(second.len(), 6);
        assert!(missing.is_empty());
        assert!(items.iter().all(|item| parse_count(item) == 1));
        // The previous implementation never cached a >8 MiB normalized file,
        // so every changed query repeated this full provider parse (6 workers).
        let reparsing = Instant::now();
        let reparsed_hits = std::thread::scope(|scope| {
            let handles: Vec<_> = items
                .iter()
                .map(|item| {
                    scope.spawn(move || {
                        let text = build_text(&item.provider_id, &item.source_path).unwrap();
                        usize::from(find_exact_matches(&text, "第二个精确答案").is_some())
                    })
                })
                .collect();
            handles
                .into_iter()
                .map(|handle| handle.join().unwrap())
                .sum::<usize>()
        });
        let reparsing = reparsing.elapsed();
        assert_eq!(reparsed_hits, 6);
        println!("synthetic_source_bytes={source_bytes} cold_ms={} warm_ms={} absent_ms={} hit_counts={}/{} source_parses=6", cold.as_millis(), warm.as_millis(), negative.as_millis(), first.len(), second.len());
        println!(
            "previous_uncached_reparse_ms={} hit_count={reparsed_hits}",
            reparsing.as_millis()
        );
        for item in items {
            evict(&item.source_path);
        }
    }

    #[test]
    fn exact_search_finds_ai_reply_beyond_the_former_character_cap() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("深层历史.jsonl");
        append_codex_message(&path, "user", &"前".repeat(2_000_010));
        append_codex_message(&path, "assistant", "这里是深层 AI 回复：完整精准答案。");
        let item = session_ref(&path, "codex");
        let hits = search_contents(&[item], "完整精准答案", 0, SessionSearchMode::Exact);
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].match_count, 1);
        assert!(hits[0].snippet.contains("完整精准答案"));
    }

    #[test]
    fn appended_ai_reply_is_searchable_with_the_same_session_identity() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("追加 对话.jsonl");
        append_codex_message(&path, "assistant", "较早的答案");
        let item = session_ref(&path, "codex");
        let items = [item];
        assert!(search_contents(&items, "新增答复", 0, SessionSearchMode::Exact).is_empty());

        append_codex_message(&path, "assistant", "新增答复。补充新增答复。");
        let hits = search_contents(&items, "新增答复", 0, SessionSearchMode::Exact);
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].match_count, 2);
        assert!(hits[0].snippet.contains("新增答复"));
        assert_eq!(
            search_contents(&items, "较早的答案", 0, SessionSearchMode::Exact).len(),
            1
        );
    }

    #[test]
    fn cache_does_not_share_content_between_providers_at_the_same_path() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("shared.jsonl");
        append_codex_message(&path, "assistant", "Codex 独有回复");
        let mut file = std::fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap();
        writeln!(
            file,
            "{}",
            json!({"message":{"role":"assistant","content":"Claude 独有回复"}})
        )
        .unwrap();

        let codex = [session_ref(&path, "codex")];
        let claude = [session_ref(&path, "claude")];
        assert_eq!(
            search_contents(&codex, "Codex 独有回复", 0, SessionSearchMode::Exact).len(),
            1
        );
        assert!(search_contents(&claude, "Codex 独有回复", 0, SessionSearchMode::Exact).is_empty());
        assert_eq!(
            search_contents(&claude, "Claude 独有回复", 0, SessionSearchMode::Exact).len(),
            1
        );
        assert!(search_contents(&codex, "Claude 独有回复", 0, SessionSearchMode::Exact).is_empty());
        evict(&path.to_string_lossy());
        let cache = cache().lock().unwrap();
        assert!(!cache
            .entries
            .contains_key(&("codex".into(), path.to_string_lossy().into())));
        assert!(!cache
            .entries
            .contains_key(&("claude".into(), path.to_string_lossy().into())));
    }

    #[test]
    fn oversized_text_is_fully_searchable_without_staying_in_cache() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("oversized.jsonl");
        let content = format!("{}完整尾部", "x".repeat(MAX_CACHE_ENTRY_BYTES + 1));
        append_codex_message(&path, "assistant", &content);
        let item = session_ref(&path, "codex");
        let key = (item.provider_id.clone(), item.source_path.clone());
        let hits = search_contents(&[item], "完整尾部", 0, SessionSearchMode::Exact);
        assert_eq!(hits.len(), 1);
        assert!(hits[0].snippet.ends_with("完整尾部"));
        assert!(!cache().lock().unwrap().entries.contains_key(&key));
    }

    #[test]
    fn cache_budget_uses_bytes_and_evicts_the_least_recently_used_entry() {
        let mut cache = TextCache {
            max_bytes: 12,
            max_entry_bytes: 8,
            max_entries: 2,
            ..TextCache::default()
        };
        let now = Instant::now();
        let entry = |text: &str, age: u64| CachedText {
            fingerprint: None,
            loaded_at: now,
            last_used: now - Duration::from_secs(age),
            text_lc: Arc::from(text),
        };
        let first = ("codex".into(), "first".into());
        let second = ("codex".into(), "second".into());
        let third = ("codex".into(), "third".into());
        cache.insert(first.clone(), entry("甲a", 3));
        cache.insert(second.clone(), entry("乙乙", 2));
        assert_eq!(cache.bytes, 10);
        assert!(cache.get_fresh(&first, None, true).is_some());
        cache.insert(third.clone(), entry("丙丙", 0));
        assert!(cache.entries.contains_key(&first));
        assert!(!cache.entries.contains_key(&second));
        assert!(cache.entries.contains_key(&third));
        assert_eq!(cache.bytes, 10);

        cache.insert(("codex".into(), "oversize".into()), entry("超出条目", 0));
        assert_eq!(cache.entries.len(), 2);
        assert_eq!(cache.bytes, 10);
        cache.remove(&first);
        assert_eq!(cache.bytes, 6);
    }

    #[test]
    fn exact_search_does_not_join_separate_messages_into_a_phrase() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("separate.jsonl");
        append_codex_message(&path, "user", "上半句");
        append_codex_message(&path, "assistant", "下半句");
        assert!(search_contents(
            &[session_ref(&path, "codex")],
            "上半句下半句",
            0,
            SessionSearchMode::Exact,
        )
        .is_empty());
    }

    #[test]
    fn exact_match_finds_chinese_substring_and_counts() {
        let text = "今天我们把星骸解压上线了。\n星骸解压的首页很好看。";
        let (count, snippet) = find_exact_matches(text, "星骸解压").expect("hit");
        assert_eq!(count, 2);
        assert!(snippet.contains("星骸解压"));
        assert!(!snippet.contains('\n'));
    }

    #[test]
    fn snippet_is_trimmed_with_ellipsis() {
        let long = format!("{}关键字{}", "a".repeat(200), "b".repeat(200));
        let (count, snippet) = find_exact_matches(&long, "关键字").expect("hit");
        assert_eq!(count, 1);
        assert!(snippet.starts_with('…'));
        assert!(snippet.ends_with('…'));
        assert!(snippet.chars().count() < 200);
    }

    #[test]
    fn exact_mode_rejects_a_non_contiguous_phrase() {
        assert!(find_exact_matches("修复 会话 搜索", "修复会话").is_none());
    }

    #[test]
    fn fuzzy_mode_ignores_spacing_and_punctuation() {
        let hit = find_fuzzy_matches("修复 会话-搜索失败", "修复会话搜索");
        assert!(hit.is_some());
        assert!(hit.unwrap().1 >= 900_000);
    }

    #[test]
    fn fuzzy_mode_matches_separated_keywords() {
        let hit = find_fuzzy_matches(
            "我们需要修复列表，随后重新实现会话的关键词搜索。",
            "修复 会话 搜索",
        );
        assert!(hit.is_some());
        assert!(hit.unwrap().1 >= 700_000);
    }

    #[test]
    fn fuzzy_mode_tolerates_one_transposed_chinese_pair() {
        let hit = find_fuzzy_matches("会话搜索失败，需要重新建立索引。", "会话搜素失败");
        assert!(hit.is_some());
        assert!(hit.unwrap().1 >= 400_000);
    }

    #[test]
    fn search_contents_reads_codex_file_in_both_modes() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("rollout-test.jsonl");
        std::fs::write(
            &path,
            "{\"timestamp\":\"2026-03-06T21:50:12Z\",\"type\":\"session_meta\",\"payload\":{\"id\":\"s1\",\"cwd\":\"/tmp\"}}\n\
             {\"timestamp\":\"2026-03-06T21:50:13Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"user\",\"content\":\"请帮我修复登录页的 Bug\"}}\n",
        )
        .unwrap();
        let items = vec![SessionRef {
            provider_id: "codex".into(),
            session_id: "s1".into(),
            source_path: path.to_string_lossy().to_string(),
        }];

        let exact = search_contents(&items, "登录页", 10, SessionSearchMode::Exact);
        assert_eq!(exact.len(), 1);
        assert_eq!(exact[0].match_count, 1);
        assert!(exact[0].snippet.contains("登录页"));
        assert_eq!(
            search_contents(&items, "BUG", 10, SessionSearchMode::Exact).len(),
            1
        );
        assert!(search_contents(&items, "不存在的词", 10, SessionSearchMode::Fuzzy).is_empty());
    }
}
