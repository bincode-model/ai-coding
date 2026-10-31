//! Read-only Claude history discovery. This must not change the configuration
//! directory used by provider/auth writes in `crate::config`.
use std::collections::HashSet;
use std::fs::File;
use std::io::Read;
use std::path::{Component, Path, PathBuf};

use serde::Deserialize;

const MAX_GG_CONFIG_BYTES: u64 = 1024 * 1024;
const ARCHIVE_PREFIX: &str = "Claude-Code-archive-";
const IGNORED_DIRS: [&str; 3] = ["subagents", "tool-results", "workflows"];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ClaudeSource {
    Configured,
    Archive,
    CurrentGg,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ClaudeRoot {
    pub path: PathBuf,
    pub source: ClaudeSource,
}

/// Installation identity is kept separate from the deduplicated viewing roots.
/// Two installations can point at the same history while using different auth.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum GgInstallation {
    Owner,
    Client,
}

impl GgInstallation {
    fn config_dir(self, home: &Path) -> PathBuf {
        home.join(match self {
            Self::Owner => ".claude-gg-dmit-metered",
            Self::Client => ".claude-gg-client",
        })
    }

    fn config(self, home: &Path) -> PathBuf {
        self.config_dir(home).join("config.private.json")
    }

    pub(crate) fn profile(self, home: &Path) -> PathBuf {
        read_gg_profile(&self.config(home)).unwrap_or_else(|| self.config_dir(home).join("profile"))
    }

    /// A fallback is useful for discovering history, but cannot establish which
    /// profile a customer's fixed-config launcher will actually use. Retain the
    /// legacy owner's default only when its config is genuinely absent.
    pub(crate) fn resume_profile(self, home: &Path) -> Result<PathBuf, String> {
        let config = self.config(home);
        if let Some(profile) = read_gg_profile(&config) {
            return Ok(profile);
        }
        if self == Self::Owner
            && std::fs::symlink_metadata(&config)
                .err()
                .map(|error| error.kind() == std::io::ErrorKind::NotFound)
                .unwrap_or(false)
        {
            return Ok(self.config_dir(home).join("profile"));
        }
        Err(format!(
            "无法确认{} Claude GG 配置中的当前记录目录，已停止恢复；请检查原安装，完整记录仍可查看。",
            self.label()
        ))
    }

    pub(crate) fn wrapper(self, home: &Path, windows: bool) -> PathBuf {
        let wrapper = match self {
            Self::Owner => gg_wrapper(home),
            Self::Client => home.join(".claude-gg-client/bin/claude-gg"),
        };
        if windows {
            wrapper.with_extension("cmd")
        } else {
            wrapper
        }
    }

    pub(crate) fn label(self) -> &'static str {
        match self {
            Self::Owner => "站长版",
            Self::Client => "客户版",
        }
    }

    fn installed(self, home: &Path) -> bool {
        marker_present(&self.config_dir(home))
            || marker_present(&self.wrapper(home, false))
            || marker_present(&self.wrapper(home, true))
            || (self == Self::Owner && marker_present(&home.join(".claude-gg-dmit")))
    }
}

// Broken links and unreadable markers still indicate an incomplete GG install;
// they must not enable a fallback through ordinary Claude and a different login.
fn marker_present(path: &Path) -> bool {
    match std::fs::symlink_metadata(path) {
        Ok(_) => true,
        Err(error) => error.kind() != std::io::ErrorKind::NotFound,
    }
}

pub(crate) fn gg_installations_for_home(home: &Path) -> Vec<GgInstallation> {
    let client_installed = GgInstallation::Client.installed(home);
    let mut installations = Vec::new();
    // Preserve the original two-root placeholder/watch behavior without adding
    // a nonexistent owner installation on customer-only machines.
    if !client_installed || GgInstallation::Owner.installed(home) {
        installations.push(GgInstallation::Owner);
    }
    if client_installed {
        installations.push(GgInstallation::Client);
    }
    installations
}

pub(crate) fn resolve() -> Vec<ClaudeRoot> {
    resolve_for_home(
        &crate::config::get_home_dir(),
        &crate::config::get_claude_config_dir(),
    )
}

pub(crate) fn paths(roots: &[ClaudeRoot]) -> Vec<PathBuf> {
    roots.iter().map(|root| root.path.clone()).collect()
}

/// One legacy source plus the applicable GG installations, including missing
/// history directories so the watcher can attach when they appear. Physical
/// aliases collapse to a GG viewing source; resume separately checks ownership.
pub(crate) fn resolve_for_home(home: &Path, configured_dir: &Path) -> Vec<ClaudeRoot> {
    let configured = configured_dir.join("projects");
    let legacy = if configured.is_dir() {
        ClaudeRoot {
            path: configured,
            source: ClaudeSource::Configured,
        }
    } else if let Some(path) = latest_archive_projects(home) {
        ClaudeRoot {
            path,
            source: ClaudeSource::Archive,
        }
    } else {
        let mirror = home.join(".codex/vendor_imports/claude_chats/raw/projects");
        if mirror.is_dir() {
            ClaudeRoot {
                path: mirror,
                source: ClaudeSource::Archive,
            }
        } else {
            ClaudeRoot {
                path: configured,
                source: ClaudeSource::Configured,
            }
        }
    };
    let mut roots = vec![legacy];
    for installation in gg_installations_for_home(home) {
        let profile = match installation {
            GgInstallation::Owner => current_gg_profile(home),
            GgInstallation::Client => installation.profile(home),
        };
        let gg = ClaudeRoot {
            path: profile.join("projects"),
            source: ClaudeSource::CurrentGg,
        };
        let comparable = comparable_path(&gg.path);
        if let Some(existing) = roots
            .iter_mut()
            .find(|root| comparable_path(&root.path) == comparable)
        {
            if existing.source != ClaudeSource::CurrentGg {
                *existing = gg;
            }
        } else {
            roots.push(gg);
        }
    }
    roots
}

fn latest_archive_projects(home: &Path) -> Option<PathBuf> {
    std::fs::read_dir(home)
        .ok()?
        .flatten()
        .filter_map(|entry| {
            let name = entry.file_name().into_string().ok()?;
            let stamp = name.strip_prefix(ARCHIVE_PREFIX)?;
            if stamp.len() != 15
                || stamp.as_bytes()[8] != b'-'
                || !stamp
                    .bytes()
                    .enumerate()
                    .all(|(i, byte)| i == 8 || byte.is_ascii_digit())
                || chrono::NaiveDateTime::parse_from_str(stamp, "%Y%m%d-%H%M%S").is_err()
            {
                return None;
            }
            let projects = entry.path().join("profile/projects");
            projects.is_dir().then_some((name, projects))
        })
        .max_by(|a, b| a.0.cmp(&b.0))
        .map(|(_, path)| path)
}

pub(crate) fn current_gg_profile(home: &Path) -> PathBuf {
    GgInstallation::Owner.profile(home)
}

/// Deserialize only the profile field. Never retain, report or log credentials
/// from this file, including serde error text or the original JSON.
pub(crate) fn read_gg_profile(config: &Path) -> Option<PathBuf> {
    #[derive(Deserialize)]
    struct ProfileOnly {
        profile: String,
    }
    let metadata = std::fs::symlink_metadata(config).ok()?;
    if !metadata.is_file() || metadata.len() > MAX_GG_CONFIG_BYTES {
        return None;
    }
    let file = File::open(config).ok()?;
    let metadata = file.metadata().ok()?;
    if !metadata.is_file() || metadata.len() > MAX_GG_CONFIG_BYTES {
        return None;
    }
    let mut bytes = Vec::new();
    file.take(MAX_GG_CONFIG_BYTES + 1)
        .read_to_end(&mut bytes)
        .ok()?;
    if bytes.len() as u64 > MAX_GG_CONFIG_BYTES {
        return None;
    }
    let ProfileOnly { profile } = serde_json::from_slice(&bytes).ok()?;
    let path = PathBuf::from(&profile);
    if profile.is_empty()
        || profile.len() > 4096
        || profile.chars().any(char::is_control)
        || !path.is_absolute()
        || path.file_name().is_none()
        || path
            .components()
            .any(|part| matches!(part, Component::ParentDir | Component::CurDir))
    {
        return None;
    }
    Some(path)
}

pub(crate) fn gg_wrapper(home: &Path) -> PathBuf {
    home.join(".claude-gg-dmit/bin/claude-gg")
}

pub(crate) fn gg_installed(home: &Path) -> bool {
    GgInstallation::Owner.installed(home) || GgInstallation::Client.installed(home)
}

pub(crate) fn is_ignored_directory(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .map(|name| IGNORED_DIRS.contains(&name))
        .unwrap_or(false)
}

pub(crate) fn is_session_path(relative: &Path) -> bool {
    relative.extension().and_then(|ext| ext.to_str()) == Some("jsonl")
        && relative
            .file_name()
            .and_then(|name| name.to_str())
            .map(|name| !name.starts_with("agent-") && name != "journal.jsonl")
            .unwrap_or(false)
        && !relative.components().any(|part| match part {
            Component::ParentDir => true,
            Component::Normal(name) => name
                .to_str()
                .map(|name| IGNORED_DIRS.contains(&name))
                .unwrap_or(false),
            _ => false,
        })
}

/// Keep exclusions on the requested path as well as the resolved target. A
/// symlink named `agent-*` or inside `subagents` must not erase that exclusion.
pub(crate) fn lexical_session_path_allowed(
    path: &Path,
    root: &Path,
    canonical_root: &Path,
) -> bool {
    let absolute = |path: &Path| {
        if path.is_absolute() {
            Some(path.to_path_buf())
        } else {
            std::env::current_dir().ok().map(|cwd| cwd.join(path))
        }
    };
    let Some(path) = absolute(path) else {
        return false;
    };
    [absolute(root), Some(canonical_root.to_path_buf())]
        .into_iter()
        .flatten()
        .any(|root| {
            path.strip_prefix(root)
                .map(is_session_path)
                .unwrap_or(false)
        })
}

/// Shared read/stat/resume boundary, also used by search through load_messages.
pub(crate) fn validate_source(path: &Path, roots: &[PathBuf]) -> Result<PathBuf, String> {
    let canonical = path
        .canonicalize()
        .map_err(|_| "Claude session source not found or unreadable".to_string())?;
    if !canonical.is_file() {
        return Err("Claude session source must be a JSONL file".into());
    }
    let lexical_inside = roots.iter().any(|root| {
        root.canonicalize()
            .ok()
            .map(|canonical_root| lexical_session_path_allowed(path, root, &canonical_root))
            .unwrap_or(false)
    });
    let inside = roots.iter().any(|root| {
        root.canonicalize()
            .ok()
            .filter(|canonical_root| canonical_root.is_dir())
            .and_then(|root| canonical.strip_prefix(root).ok().map(is_session_path))
            .unwrap_or(false)
    });
    if !lexical_inside || !inside {
        return Err(
            "Claude session source is outside provider roots or is not a user session".into(),
        );
    }
    Ok(canonical)
}

pub(crate) fn collect_session_files(roots: &[PathBuf]) -> Vec<PathBuf> {
    let mut files = HashSet::new();
    for root in roots {
        let Ok(root) = root.canonicalize() else {
            continue;
        };
        let mut visited = HashSet::new();
        let mut pending = vec![root.clone()];
        while let Some(directory) = pending.pop() {
            let Ok(directory) = directory.canonicalize() else {
                continue;
            };
            if !directory.starts_with(&root) || !visited.insert(directory.clone()) {
                continue;
            }
            let Ok(entries) = std::fs::read_dir(&directory) else {
                continue;
            };
            for entry in entries.flatten() {
                let path = entry.path();
                if is_ignored_directory(&path) {
                    continue;
                }
                let Ok(canonical) = path.canonicalize() else {
                    continue;
                };
                let Ok(relative) = canonical.strip_prefix(&root) else {
                    continue;
                };
                if canonical.is_dir() {
                    if !relative.components().any(|part| match part {
                        Component::Normal(name) => IGNORED_DIRS.iter().any(|dir| name == *dir),
                        _ => false,
                    }) {
                        pending.push(canonical);
                    }
                } else if is_session_path(relative)
                    && lexical_session_path_allowed(&path, &root, &root)
                    && canonical.is_file()
                {
                    files.insert(canonical);
                }
            }
        }
    }
    let mut files: Vec<_> = files.into_iter().collect();
    files.sort();
    files
}

/// Resolve the nearest existing ancestor for missing roots and removal events.
/// Broken links and parent traversal are rejected, not treated as safe absence.
pub(crate) fn canonicalize_with_missing(path: &Path) -> Option<PathBuf> {
    if path.components().any(|part| part == Component::ParentDir) {
        return None;
    }
    let mut ancestor = path.to_path_buf();
    let mut missing = Vec::new();
    loop {
        match std::fs::symlink_metadata(&ancestor) {
            Ok(_) => {
                let mut result = ancestor.canonicalize().ok()?;
                for part in missing.into_iter().rev() {
                    result.push(part);
                }
                return Some(result);
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                missing.push(ancestor.file_name()?.to_os_string());
                if !ancestor.pop() {
                    return None;
                }
            }
            Err(_) => return None,
        }
    }
}

fn comparable_path(path: &Path) -> PathBuf {
    canonicalize_with_missing(path).unwrap_or_else(|| path.to_path_buf())
}
