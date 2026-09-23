//! Resume the selected source, never an implicit latest session or a summary prompt.
use std::path::Path;

use serde::Serialize;

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
    let roots = provider_roots(provider)?;
    let prepared = prepare_with_roots(provider, session_id, Path::new(source), &roots)?;
    #[cfg(not(target_os = "windows"))]
    if provider == "claude" {
        if let Some(executable) = crate::commands::resolve_session_cli_path("claude")? {
            // Respect launchers that deliberately prohibit direct Claude execution.
            // Do not replace a selected UUID with `--continue` (a different session).
            if is_guarded_claude_launcher(&executable) {
                return Err(format!("当前 Claude GG 启动器不支持按 ID 一键恢复。请在正常启动的 Claude GG 中使用 /resume {session_id}；目标会话必须位于同一记录目录。这里的完整记录仍可查看。"));
            }
        }
    }
    Ok(prepared)
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

fn prepare_with_roots(
    provider: &str,
    session_id: &str,
    source: &Path,
    roots: &[std::path::PathBuf],
) -> Result<PreparedResume, String> {
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
    let meta = match provider {
        "codex" => providers::codex::parse_session(&source),
        "claude" => providers::claude::parse_session(&source),
        _ => None,
    }
    .ok_or_else(|| "无法读取原会话，请刷新列表后重试".to_string())?;
    if meta.session_id != session_id {
        return Err("会话 ID 与原始文件不一致，请刷新列表后重新选择".into());
    }
    let cli_home = root
        .parent()
        .ok_or_else(|| "无法确定原会话的 CLI 目录".to_string())?;
    if let Some(cwd) = meta.project_dir.as_deref() {
        if !Path::new(cwd).is_dir() {
            return Err(format!(
                "原会话的工作目录已不存在：{cwd}。完整记录仍可在此查看。"
            ));
        }
    }
    Ok(PreparedResume {
        command: build_command(provider, cli_home, session_id, cfg!(windows)),
        cwd: meta.project_dir,
    })
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
            &[root.clone()],
        )
        .unwrap();
        assert!(prepared.command.contains("CODEX_HOME="));
        assert!(prepared.command.contains(
            "custom home' codex resume -c tui.terminal_resize_reflow_max_rows=0 --no-alt-screen -- '01a08128-e54f-7352-98f2-3ac0816362ae'"
        ));
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
