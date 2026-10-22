//! 会话文件监听模块
//!
//! 终端里的 Claude Code / Codex 在会话 jsonl 文件末尾追加消息时，
//! 本模块通过 `notify` 监听会话根目录（递归），并向前端 emit
//! `session-manager://changed` 事件，让「会话管理」页面无需手动刷新
//! 就能看到新消息。
//!
//! 设计要点（与 `usage_events` 保持同一套风格）：
//! - 全局单例 AppHandle：回调线程里不持有 Tauri state，用 OnceLock 共享。
//! - 400ms 防抖合并：流式输出时一条消息可能触发多次写事件，合并成一次
//!   事件，前端再做一次节流。
//! - 只关心会话文件：仅 `*.jsonl`，跳过 `agent-*`（子代理转录）以及
//!   `subagents/`、`tool-results/`、`workflows/` 旁路目录。
//! - 每 30 秒重新核对有限的根目录，缺失目录稍后创建或被替换时重新挂载；
//!   不递归监听 home，也不设置一小时的重试期限。
//! - 监听失败只记录日志，绝不影响应用启动。

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

use notify::event::ModifyKind;
use notify::{Config, Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use serde::Serialize;
use tauri::{AppHandle, Emitter};

/// 前端监听的事件名
pub const EVENT_SESSION_CHANGED: &str = "session-manager://changed";

/// 防抖窗口：合并 400ms 内的多次文件事件。
const DEBOUNCE_WINDOW: Duration = Duration::from_millis(400);

/// 整个应用生命周期内低频核对目录，不轮询历史文件内容。
const RETRY_INTERVAL: Duration = Duration::from_secs(30);

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionChangedPayload {
    pub provider_id: String,
    pub paths: Vec<String>,
}

/// 一个被监听的根目录：provider 与（原始 / 规范化）路径。
#[derive(Debug, Clone)]
pub(crate) struct WatchRoot {
    pub provider_id: String,
    pub path: PathBuf,
    pub canonical: Option<PathBuf>,
}

#[derive(Default)]
pub(crate) struct WatchState {
    pub roots: Vec<WatchRoot>,
    identities: HashMap<PathBuf, DirectoryIdentity>,
}

#[derive(Clone, PartialEq, Eq)]
struct DirectoryIdentity {
    #[cfg(unix)]
    device: u64,
    #[cfg(unix)]
    inode: u64,
    #[cfg(not(unix))]
    created: Option<std::time::SystemTime>,
}

fn directory_identity(path: &Path) -> Option<DirectoryIdentity> {
    let metadata = std::fs::metadata(path).ok()?;
    if !metadata.is_dir() {
        return None;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        Some(DirectoryIdentity {
            device: metadata.dev(),
            inode: metadata.ino(),
        })
    }
    #[cfg(not(unix))]
    {
        Some(DirectoryIdentity {
            created: metadata.created().ok(),
        })
    }
}

static APP_HANDLE: OnceLock<AppHandle> = OnceLock::new();
static WATCHER: OnceLock<Mutex<Option<RecommendedWatcher>>> = OnceLock::new();
static ROOTS: OnceLock<Mutex<Vec<WatchRoot>>> = OnceLock::new();
static PENDING: OnceLock<Mutex<HashMap<String, HashSet<PathBuf>>>> = OnceLock::new();
static EMIT_SCHEDULED: AtomicBool = AtomicBool::new(false);

fn pending() -> &'static Mutex<HashMap<String, HashSet<PathBuf>>> {
    PENDING.get_or_init(|| Mutex::new(HashMap::new()))
}

fn roots() -> &'static Mutex<Vec<WatchRoot>> {
    ROOTS.get_or_init(|| Mutex::new(Vec::new()))
}

fn watcher_slot() -> &'static Mutex<Option<RecommendedWatcher>> {
    WATCHER.get_or_init(|| Mutex::new(None))
}

/// 需要监听的根目录：Claude Code 的 projects 与 Codex 的 sessions / archived_sessions。
fn desired_roots() -> Vec<(String, PathBuf)> {
    desired_roots_for_home(
        &crate::config::get_home_dir(),
        &crate::config::get_claude_config_dir(),
        &super::providers::codex::session_roots(),
    )
}

pub(crate) fn desired_roots_for_home(
    home: &Path,
    configured_claude_dir: &Path,
    codex_roots: &[PathBuf],
) -> Vec<(String, PathBuf)> {
    let mut list: Vec<_> = super::claude_roots::resolve_for_home(home, configured_claude_dir)
        .into_iter()
        .map(|root| ("claude".to_string(), root.path))
        .collect();
    for path in codex_roots {
        list.push(("codex".to_string(), path.clone()));
    }
    list
}

/// 判断一个文件事件路径属于哪个 provider 的会话文件。
///
/// 返回 `None` 表示与会话无关（非 jsonl、子代理转录、旁路目录、不在任何根目录下）。
pub(crate) fn classify_event_path(path: &Path, roots: &[WatchRoot]) -> Option<String> {
    if path.extension().and_then(|ext| ext.to_str()) != Some("jsonl") {
        return None;
    }
    let canonical = super::claude_roots::canonicalize_with_missing(path)?;
    roots
        .iter()
        .find(|root| {
            root.canonical
                .clone()
                .or_else(|| super::claude_roots::canonicalize_with_missing(&root.path))
                .filter(|_| {
                    roots.iter().any(|lexical_root| {
                        lexical_root.provider_id == root.provider_id
                            && lexical_root
                                .canonical
                                .clone()
                                .or_else(|| {
                                    super::claude_roots::canonicalize_with_missing(
                                        &lexical_root.path,
                                    )
                                })
                                .map(|canonical_root| {
                                    super::claude_roots::lexical_session_path_allowed(
                                        path,
                                        &lexical_root.path,
                                        &canonical_root,
                                    )
                                })
                                .unwrap_or(false)
                    })
                })
                .and_then(|root| {
                    canonical
                        .strip_prefix(root)
                        .ok()
                        .map(super::claude_roots::is_session_path)
                })
                .unwrap_or(false)
        })
        .map(|root| root.provider_id.clone())
}

/// 在应用 setup 阶段调用一次。失败只记录日志，不向上传播。
pub fn start(handle: AppHandle) {
    if APP_HANDLE.set(handle).is_err() {
        log::debug!("session_manager::watcher::start 重复调用，已忽略");
        return;
    }

    std::thread::Builder::new()
        .name("session-watcher".into())
        .spawn(|| {
            let mut state = WatchState::default();
            loop {
                match watch_missing_roots(&mut state) {
                    Ok(_) => {}
                    Err(err) => {
                        log::warn!("[session-watcher] 初始化失败，稍后重试: {err}");
                    }
                }
                std::thread::sleep(RETRY_INTERVAL);
            }
        })
        .ok();
}

/// 为尚未监听且已存在的根目录挂上 watcher，返回仍不存在的根目录数量。
fn watch_missing_roots(state: &mut WatchState) -> Result<usize, String> {
    let mut slot = watcher_slot()
        .lock()
        .map_err(|_| "watcher lock poisoned".to_string())?;

    if slot.is_none() {
        let watcher = RecommendedWatcher::new(
            move |result: notify::Result<Event>| {
                let Ok(event) = result else { return };
                handle_event(event);
            },
            Config::default().with_follow_symlinks(false),
        )
        .map_err(|e| format!("create watcher failed: {e}"))?;
        *slot = Some(watcher);
    }

    let watcher = slot.as_mut().expect("watcher just initialised");
    let (remaining, changed) = sync_watch_roots(watcher, state, &desired_roots());
    *roots()
        .lock()
        .map_err(|_| "roots lock poisoned".to_string())? = state.roots.clone();
    drop(slot);

    if !changed.is_empty() {
        if let Ok(mut map) = pending().lock() {
            for root in changed {
                map.entry(root.provider_id).or_default().insert(root.path);
            }
        }
        // Sessions may already have been written before a formerly missing
        // directory was attached. A root change must invalidate the list too.
        schedule_emit();
    }
    Ok(remaining)
}

/// Injectable native watcher reconciliation for isolated-home acceptance tests.
/// Returns changes as well as missing/failed roots, without creating directories.
pub(crate) fn sync_watch_roots<W: Watcher>(
    watcher: &mut W,
    state: &mut WatchState,
    desired: &[(String, PathBuf)],
) -> (usize, Vec<WatchRoot>) {
    let mut remaining = 0usize;
    let mut candidates = Vec::new();
    let mut seen = HashSet::new();
    for (provider_id, path) in desired {
        let Some((canonical, identity)) = path
            .canonicalize()
            .ok()
            .and_then(|canonical| directory_identity(&canonical).map(|id| (canonical, id)))
        else {
            remaining += 1;
            continue;
        };
        if seen.insert(canonical.clone()) {
            candidates.push((
                WatchRoot {
                    provider_id: provider_id.clone(),
                    path: path.clone(),
                    canonical: Some(canonical),
                },
                identity,
            ));
        }
    }
    let mut changed = Vec::new();
    state.roots.retain(|old| {
        let keep = candidates.iter().any(|(new, identity)| {
            old.path == new.path
                && old.provider_id == new.provider_id
                && old.canonical == new.canonical
                && state.identities.get(&old.path) == Some(identity)
        });
        if !keep {
            let _ = watcher.unwatch(old.canonical.as_deref().unwrap_or(&old.path));
            state.identities.remove(&old.path);
            changed.push(old.clone());
        }
        keep
    });

    for (root, identity) in candidates {
        if state.roots.iter().any(|old| old.path == root.path) {
            continue;
        }
        match watcher.watch(
            root.canonical.as_deref().unwrap_or(&root.path),
            RecursiveMode::Recursive,
        ) {
            Ok(()) => {
                state.identities.insert(root.path.clone(), identity);
                changed.push(root.clone());
                state.roots.push(root);
            }
            Err(err) => {
                log::warn!("[session-watcher] 监听 {} 失败: {err}", root.path.display());
                remaining += 1;
            }
        }
    }

    (remaining, changed)
}

fn handle_event(event: Event) {
    let relevant = match event.kind {
        EventKind::Create(_) | EventKind::Remove(_) | EventKind::Any => true,
        EventKind::Modify(ModifyKind::Metadata(_)) => false,
        EventKind::Modify(_) => true,
        _ => false,
    };
    if !relevant {
        return;
    }

    let Ok(roots) = roots().lock() else { return };
    let mut matched = false;
    for path in event.paths {
        if let Some(provider_id) = classify_event_path(&path, &roots) {
            if let Ok(mut map) = pending().lock() {
                let paths = map.entry(provider_id).or_default();
                if let Some(canonical) = super::claude_roots::canonicalize_with_missing(&path) {
                    paths.insert(canonical);
                }
                paths.insert(path);
                matched = true;
            }
        }
    }
    drop(roots);

    if matched {
        schedule_emit();
    }
}

/// 400ms 后把累计的变更按 provider 分组各发一次事件。
fn schedule_emit() {
    let Some(handle) = APP_HANDLE.get() else {
        return;
    };
    if EMIT_SCHEDULED.swap(true, Ordering::AcqRel) {
        return;
    }

    let handle = handle.clone();
    std::thread::spawn(move || {
        std::thread::sleep(DEBOUNCE_WINDOW);
        EMIT_SCHEDULED.store(false, Ordering::Release);

        let batches: Vec<(String, Vec<String>)> = match pending().lock() {
            Ok(mut map) => map
                .drain()
                .map(|(provider_id, paths)| {
                    let mut list: Vec<String> = paths
                        .into_iter()
                        .map(|p| p.to_string_lossy().to_string())
                        .collect();
                    list.sort();
                    (provider_id, list)
                })
                .collect(),
            Err(_) => return,
        };

        for (provider_id, paths) in batches {
            let payload = SessionChangedPayload { provider_id, paths };
            if let Err(err) = handle.emit(EVENT_SESSION_CHANGED, payload) {
                log::warn!("emit {EVENT_SESSION_CHANGED} 失败: {err}");
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    fn roots_for(claude: &Path, codex: &Path) -> Vec<WatchRoot> {
        vec![
            WatchRoot {
                provider_id: "claude".into(),
                path: claude.to_path_buf(),
                canonical: claude.canonicalize().ok(),
            },
            WatchRoot {
                provider_id: "codex".into(),
                path: codex.to_path_buf(),
                canonical: codex.canonicalize().ok(),
            },
        ]
    }

    #[test]
    fn classifies_claude_session_file() {
        let claude = tempdir().unwrap();
        let codex = tempdir().unwrap();
        let roots = roots_for(claude.path(), codex.path());
        let file = claude.path().join("-Users-me-proj").join("abc.jsonl");
        assert_eq!(
            classify_event_path(&file, &roots).as_deref(),
            Some("claude")
        );
    }

    #[test]
    fn classifies_codex_dated_session_file() {
        let claude = tempdir().unwrap();
        let codex = tempdir().unwrap();
        let roots = roots_for(claude.path(), codex.path());
        let file = codex
            .path()
            .join("2026/08/31/rollout-2026-08-31T10-00-00-uuid.jsonl");
        assert_eq!(classify_event_path(&file, &roots).as_deref(), Some("codex"));
    }

    #[test]
    fn ignores_non_session_files() {
        let claude = tempdir().unwrap();
        let codex = tempdir().unwrap();
        let roots = roots_for(claude.path(), codex.path());
        let base = claude.path().join("-Users-me-proj");
        assert!(classify_event_path(&base.join("agent-123.jsonl"), &roots).is_none());
        assert!(classify_event_path(&base.join("abc/subagents/x.jsonl"), &roots).is_none());
        assert!(classify_event_path(&base.join("abc/tool-results/x.json"), &roots).is_none());
        assert!(classify_event_path(&base.join("abc/workflows/w.jsonl"), &roots).is_none());
        assert!(classify_event_path(&base.join("notes.txt"), &roots).is_none());
    }

    #[test]
    fn ignores_paths_outside_roots() {
        let claude = tempdir().unwrap();
        let codex = tempdir().unwrap();
        let other = tempdir().unwrap();
        let roots = roots_for(claude.path(), codex.path());
        assert!(classify_event_path(&other.path().join("x.jsonl"), &roots).is_none());
    }
}
