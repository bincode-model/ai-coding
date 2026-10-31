//! Resume the selected source, never an implicit latest session or a summary prompt.
use std::path::Path;

use serde::Serialize;

use super::claude_roots::{self, ClaudeRoot, ClaudeSource};
use super::{canonicalize_existing_path, provider_roots, providers};

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PreparedResume {
    pub command: String,
    pub cwd: Option<String>,
}

pub fn prepare(provider: &str, session_id: &str, source: &str) -> Result<PreparedResume, String> {
    if !matches!(provider, "codex" | "claude") {
        return Err(format!("Unsupported resume provider: {provider}"));
    }
    if provider == "codex" {
        return prepare_with_roots(
            provider,
            session_id,
            Path::new(source),
            &provider_roots(provider)?,
        );
    }
    let home = crate::config::get_home_dir();
    let prepared = prepare_claude_for_home(
        &home,
        &crate::config::get_claude_config_dir(),
        session_id,
        Path::new(source),
    )?;
    #[cfg(not(target_os = "windows"))]
    if !claude_roots::gg_installed(&home) {
        if let Some(executable) = crate::commands::resolve_session_cli_path("claude")? {
            // Respect launchers that deliberately prohibit direct Claude execution.
            // Do not replace a selected UUID with `--continue` (a different session).
            if is_guarded_claude_launcher(&executable) {
                return Err("Claude 的直接启动入口已被禁用，且无法确认当前 Claude GG 包装入口；已停止恢复。完整记录仍可查看。".into());
            }
        }
    }
    Ok(prepared)
}

pub(crate) fn prepare_claude_for_home(
    home: &Path,
    configured_dir: &Path,
    session_id: &str,
    source: &Path,
) -> Result<PreparedResume, String> {
    let roots = claude_roots::resolve_for_home(home, configured_dir);
    prepare_claude_with_roots(home, session_id, source, &roots)
}

pub(crate) fn prepare_claude_with_roots(
    home: &Path,
    session_id: &str,
    source: &Path,
    roots: &[ClaudeRoot],
) -> Result<PreparedResume, String> {
    let command = claude_command_for_source(home, session_id, source, roots, cfg!(windows))?;
    let source = claude_roots::validate_source(source, &claude_roots::paths(roots))?;
    let meta = providers::claude::parse_session(&source)
        .ok_or_else(|| "无法读取原会话，请刷新列表后重试".to_string())?;
    if meta.session_id != session_id {
        return Err("会话 ID 与原始文件不一致，请刷新列表后重新选择".into());
    }
    validate_cwd(meta.project_dir.as_deref())?;
    Ok(PreparedResume {
        command,
        cwd: meta.project_dir,
    })
}

/// Used by both the frontend's metadata preview and its prepare/copy/launch
/// actions. Only the existing GG wrapper may choose its profile and network.
pub(crate) fn claude_command_for_source(
    home: &Path,
    session_id: &str,
    source: &Path,
    roots: &[ClaudeRoot],
    windows: bool,
) -> Result<String, String> {
    uuid::Uuid::parse_str(session_id)
        .map_err(|_| "会话 ID 不是有效 UUID，已停止恢复；请刷新后重新选择".to_string())?;
    let source = claude_roots::validate_source(source, &claude_roots::paths(roots))?;
    let root = roots
        .iter()
        .filter(|root| {
            root.path
                .canonicalize()
                .ok()
                .and_then(|root| {
                    source
                        .strip_prefix(root)
                        .ok()
                        .map(claude_roots::is_session_path)
                })
                .unwrap_or(false)
        })
        // A nested/aliased GG root must never be routed through ordinary Claude.
        .max_by_key(|root| {
            (
                root.source == ClaudeSource::CurrentGg,
                root.path.components().count(),
            )
        })
        .ok_or_else(|| "会话文件不属于当前 Claude 记录目录".to_string())?;
    match root.source {
        ClaudeSource::CurrentGg => {
            // Re-read installation bindings even when callers retain an older
            // discovery snapshot. Never select a wrapper by availability/order.
            let mut matches = Vec::new();
            for installation in claude_roots::gg_installations_for_home(home) {
                let Ok(current_root) = installation.profile(home).join("projects").canonicalize()
                else {
                    continue;
                };
                if source
                    .strip_prefix(&current_root)
                    .map(claude_roots::is_session_path)
                    .unwrap_or(false)
                {
                    matches.push((installation, current_root));
                }
            }
            if matches.len() > 1 {
                return Err("此会话同时属于站长版和客户版 Claude GG 的记录目录，入口归属有歧义，已停止恢复；请核对原安装，完整记录仍可查看。".into());
            }
            let stale_source = || "当前 Claude GG 记录目录已变化或来源不匹配，已停止恢复；请刷新列表并核对原安装，完整记录仍可查看。".to_string();
            let (installation, current_root) = matches.pop().ok_or_else(stale_source)?;
            if root.path.canonicalize().map_err(|_| stale_source())? != current_root {
                return Err(stale_source());
            }
            let wrapper = installation.wrapper(home, windows);
            if !wrapper_available(home, &wrapper, windows) {
                return Err(format!(
                    "当前{} Claude GG 包装入口不可用（缺失、非可执行或符号链接），已停止恢复；完整记录仍可查看。",
                    installation.label()
                ));
            }
            let verified_root = installation
                .resume_profile(home)?
                .join("projects")
                .canonicalize()
                .map_err(|_| stale_source())?;
            if verified_root != current_root {
                return Err(stale_source());
            }
            Ok(build_gg_command(&wrapper, session_id, windows))
        }
        ClaudeSource::Archive => Err(
            "这是旧 Claude 历史归档，无法一键恢复到当前 Claude GG；完整记录仍可查看。不会复制历史或切换专线配置。".into(),
        ),
        ClaudeSource::Configured if claude_roots::gg_installed(home) => Err(
            "此记录不在当前 Claude GG 的记录目录，无法通过专线一键恢复；完整记录仍可查看。".into(),
        ),
        ClaudeSource::Configured => {
            let cli_home = root.path.parent().ok_or_else(|| "无法确定 Claude 目录".to_string())?;
            Ok(build_command("claude", cli_home, session_id, windows))
        }
    }
}

fn wrapper_available(home: &Path, path: &Path, windows: bool) -> bool {
    if !path.is_absolute() {
        return false;
    }
    // Do not follow an installation/bin symlink into a different installation.
    // Ancestors of HOME may legitimately be aliases (e.g. /var on macOS).
    let Some(relative_parent) = path
        .parent()
        .and_then(|parent| parent.strip_prefix(home).ok())
    else {
        return false;
    };
    let mut parent = home.to_path_buf();
    for component in relative_parent.components() {
        let std::path::Component::Normal(name) = component else {
            return false;
        };
        parent.push(name);
        if !std::fs::symlink_metadata(&parent)
            .map(|metadata| metadata.is_dir() && !metadata.file_type().is_symlink())
            .unwrap_or(false)
        {
            return false;
        }
    }
    let Ok(metadata) = std::fs::symlink_metadata(path) else {
        return false;
    };
    if !metadata.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        windows || metadata.permissions().mode() & 0o111 != 0
    }
    #[cfg(not(unix))]
    {
        let _ = windows;
        true
    }
}

pub(crate) fn build_gg_command(wrapper: &Path, session_id: &str, windows: bool) -> String {
    if windows {
        format!(
            "& '{}' --resume '{}'",
            wrapper.to_string_lossy().replace('\'', "''"),
            session_id.replace('\'', "''")
        )
    } else {
        format!(
            "{} --resume {}",
            super::terminal::shell_escape(&wrapper.to_string_lossy()),
            super::terminal::shell_escape(session_id)
        )
    }
}

#[cfg(not(target_os = "windows"))]
fn is_guarded_claude_launcher(path: &Path) -> bool {
    std::fs::metadata(path)
        .ok()
        .filter(|m| m.len() < 4096)
        .and_then(|_| std::fs::read_to_string(path).ok())
        .map(|text| text.contains("Direct Claude launch blocked.") && text.contains("claude-gg"))
        .unwrap_or(false)
}

pub(crate) fn prepare_with_roots(
    provider: &str,
    session_id: &str,
    source: &Path,
    roots: &[std::path::PathBuf],
) -> Result<PreparedResume, String> {
    if provider != "codex" {
        return Err("Claude 恢复必须先确认旧历史或当前 GG 来源".into());
    }
    uuid::Uuid::parse_str(session_id)
        .map_err(|_| "会话 ID 不是有效 UUID，已停止恢复；请刷新后重新选择".to_string())?;
    let source = canonicalize_existing_path(source, "会话原始文件")?;
    if !source.is_file() || source.extension().and_then(|e| e.to_str()) != Some("jsonl") {
        return Err("恢复会话需要原始 JSONL 文件".into());
    }
    let root = roots
        .iter()
        .find(|root| {
            root.canonicalize()
                .map(|root| source.starts_with(root))
                .unwrap_or(false)
        })
        .ok_or_else(|| "会话文件不属于此 Agent 的记录目录".to_string())?;
    let meta = providers::codex::parse_session(&source)
        .ok_or_else(|| "无法读取原会话，请刷新列表后重试".to_string())?;
    if meta.session_id != session_id {
        return Err("会话 ID 与原始文件不一致，请刷新列表后重新选择".into());
    }
    let cli_home = root
        .parent()
        .ok_or_else(|| "无法确定原会话的 CLI 目录".to_string())?;
    validate_cwd(meta.project_dir.as_deref())?;
    Ok(PreparedResume {
        command: build_command(provider, cli_home, session_id, cfg!(windows)),
        cwd: meta.project_dir,
    })
}

fn validate_cwd(cwd: Option<&str>) -> Result<(), String> {
    if let Some(cwd) = cwd {
        if !Path::new(cwd).is_dir() {
            return Err(format!(
                "原会话的工作目录已不存在：{cwd}。完整记录仍可在此查看。"
            ));
        }
    }
    Ok(())
}

fn build_command(provider: &str, home: &Path, session_id: &str, windows: bool) -> String {
    let env_name = if provider == "codex" {
        "CODEX_HOME"
    } else {
        "CLAUDE_CONFIG_DIR"
    };
    let quote = |value: &str| {
        if windows {
            format!("'{}'", value.replace('\'', "''"))
        } else {
            super::terminal::shell_escape(value)
        }
    };
    let cli = if provider == "codex" {
        "codex resume -c tui.terminal_resize_reflow_max_rows=0 --no-alt-screen --"
    } else {
        "claude --resume"
    };
    let home = quote(&home.to_string_lossy());
    let id = quote(session_id);
    if windows {
        format!("$env:{env_name}={home}; {cli} {id}")
    } else {
        format!("env {env_name}={home} {cli} {id}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use tempfile::tempdir;

    fn fixture(root: &Path, id: &str) -> std::path::PathBuf {
        std::fs::create_dir_all(root).unwrap();
        let path = root.join(format!("{id}.jsonl"));
        std::fs::write(
            &path,
            json!({"type":"session_meta","payload":{
                "id":id,"source":"cli","cwd":root.to_string_lossy()
            }})
            .to_string(),
        )
        .unwrap();
        path
    }

    #[test]
    fn resumes_exact_source_with_original_home_and_scrollback() {
        let dir = tempdir().unwrap();
        let root = dir.path().join("custom home/sessions");
        let source = fixture(&root, "01a08128-e54f-7352-98f2-3ac0816362ae");
        let prepared = prepare_with_roots(
            "codex",
            "01a08128-e54f-7352-98f2-3ac0816362ae",
            &source,
            std::slice::from_ref(&root),
        )
        .unwrap();
        assert!(prepared.command.contains("CODEX_HOME="));
        assert!(prepared.command.contains(
            "codex resume -c tui.terminal_resize_reflow_max_rows=0 --no-alt-screen -- '01a08128-e54f-7352-98f2-3ac0816362ae'"
        ));
        if cfg!(windows) {
            assert!(prepared.command.starts_with("$env:CODEX_HOME='"));
            assert!(prepared.command.contains("custom home'; codex resume"));
        } else {
            assert!(prepared.command.starts_with("env CODEX_HOME='"));
            assert!(prepared.command.contains("custom home' codex resume"));
        }
        assert_eq!(prepared.cwd.as_deref(), root.to_str());
        assert!(!prepared.command.contains("--last"));
        assert!(source.exists());
    }

    #[test]
    fn rejects_another_session_or_agent_root() {
        let dir = tempdir().unwrap();
        let root = dir.path().join("sessions");
        let source = fixture(&root, "01a08128-e54f-7352-98f2-3ac0816362ae");
        assert!(prepare_with_roots("codex", "different", &source, &[root]).is_err());
        assert!(prepare_with_roots(
            "codex",
            "01a08128-e54f-7352-98f2-3ac0816362ae",
            &source,
            &[dir.path().join("projects")]
        )
        .is_err());
    }

    #[test]
    fn rejects_cli_flags_disguised_as_session_ids() {
        let dir = tempdir().unwrap();
        let root = dir.path().join("sessions");
        let source = fixture(&root, "--dangerously-bypass-approvals-and-sandbox");
        let error = prepare_with_roots(
            "codex",
            "--dangerously-bypass-approvals-and-sandbox",
            &source,
            &[root],
        )
        .unwrap_err();
        assert!(error.contains("UUID"));
    }

    #[test]
    fn quotes_home_and_id_for_posix_and_powershell() {
        let home = Path::new("/tmp/it's $(id)");
        let command = build_command("codex", home, "id'; echo bad", false);
        assert!(command.contains("'/tmp/it'\\''s $(id)'"));
        assert!(command.ends_with("'id'\\''; echo bad'"));
        let command = build_command("claude", home, "chosen", true);
        assert_eq!(
            command,
            "$env:CLAUDE_CONFIG_DIR='/tmp/it''s $(id)'; claude --resume 'chosen'"
        );
    }
}
