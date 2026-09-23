#![allow(non_snake_case)]

use std::path::PathBuf;
use std::sync::{Arc, OnceLock};

use crate::session_manager;

/// Resolve an original session immediately before resuming it, including its CLI home.
#[tauri::command]
pub async fn prepare_session_resume(
    providerId: String,
    sessionId: String,
    sourcePath: String,
) -> Result<session_manager::resume::PreparedResume, String> {
    tauri::async_runtime::spawn_blocking(move || {
        session_manager::resume::prepare(&providerId, &sessionId, &sourcePath)
    })
    .await
    .map_err(|e| format!("Failed to prepare session resume: {e}"))?
}

#[tauri::command]
pub async fn list_sessions() -> Result<Vec<session_manager::SessionMeta>, String> {
    let sessions = tauri::async_runtime::spawn_blocking(session_manager::scan_sessions)
        .await
        .map_err(|e| format!("Failed to scan sessions: {e}"))?;
    Ok(sessions)
}

#[tauri::command]
pub async fn get_session_messages(
    providerId: String,
    sourcePath: String,
) -> Result<Vec<session_manager::SessionMessage>, String> {
    let provider_id = providerId.clone();
    let source_path = sourcePath.clone();
    tauri::async_runtime::spawn_blocking(move || {
        session_manager::load_messages(&provider_id, &source_path)
    })
    .await
    .map_err(|e| format!("Failed to load session messages: {e}"))?
}

/// 会话源文件的 mtime / size，供前端「变了才重载」的兜底轮询使用。
#[tauri::command]
pub async fn get_session_file_stat(
    providerId: String,
    sourcePath: String,
) -> Result<Option<session_manager::SessionFileStat>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        session_manager::session_file_stat(&providerId, &sourcePath)
    })
    .await
    .map_err(|e| format!("Failed to stat session file: {e}"))?
}

/// 在给定会话集合中检索正文，支持精准与模糊匹配（带内存缓存）。
#[tauri::command]
pub async fn search_session_contents(
    items: Vec<session_manager::search::SessionRef>,
    query: String,
    mode: Option<session_manager::search::SessionSearchMode>,
    limit: Option<usize>,
    requestId: Option<String>,
) -> Result<Vec<session_manager::search::SessionSearchHit>, String> {
    // Register before awaiting the gate: a new keystroke cancels the running
    // request immediately, and superseded queued requests never start workers.
    let request = session_manager::search::begin_request(requestId);
    static SEARCH_GATE: OnceLock<Arc<tokio::sync::Mutex<()>>> = OnceLock::new();
    let permit = Arc::clone(SEARCH_GATE.get_or_init(|| Arc::new(tokio::sync::Mutex::new(()))))
        .lock_owned()
        .await;
    if request.is_cancelled() {
        return Err("Session search cancelled".into());
    }
    let limit = limit.unwrap_or(items.len());
    let mode = mode.unwrap_or_default();
    tauri::async_runtime::spawn_blocking(move || {
        // Hold the gate inside the blocking task too, even if its IPC waiter
        // disappears while the provider finishes a synchronous file parse.
        let _permit = permit;
        session_manager::search::search_contents_with_request(&items, &query, limit, mode, &request)
    })
    .await
    .map_err(|e| format!("Failed to search session contents: {e}"))?
}

#[tauri::command]
pub fn cancel_session_search(requestId: String) {
    session_manager::search::cancel_request(&requestId);
}

/// 在用户选定的终端里恢复一个会话。
///
/// # 安全边界：`command` 是刻意不加校验的
///
/// 本命令接受 renderer 传来的任意字符串并最终交给 shell。多份外部审计把这一点
/// 报成"IPC 任意命令执行"，这里明确记录为**已知并接受的风险**，而不是待修缺陷。
///
/// 依据是本应用把 renderer 当作可信边界。支撑这一判断的是以下事实，全部逐条
/// 核实过（2026-07）：
///
/// 1. 全库仅一处 `dangerouslySetInnerHTML`（`ProviderIcon.tsx`），其入参是图标
///    **名字**，经 `hasIcon()` 把关后从手工维护的构建期注册表取 SVG——用户与
///    深链接都只能给名字，给不了标记内容
/// 2. 前端无 `eval` / `new Function`
/// 3. `tauri.conf.json` 的 `frontendDist` 指向打包产物，webview 不加载任何远程
///    源；界面里也没有 `<iframe>` / `<webview>`
/// 4. CSP 为 `script-src 'self'`——既不允许内联脚本，也不允许外部脚本
///
/// 因此"攻击者能调用本 IPC"这一前提，成立时已意味着他能以当前用户身份执行代码；
/// 那种情况下绕道本命令并不会让他多拿到任何东西。
///
/// # 什么会推翻这个结论
///
/// 上面四条任意一条不再成立，本命令就必须改成**只接收 session / provider 标识、
/// 由后端从会话记录重建命令**。具体触发条件：
///
/// - 渲染任何来自网络或配置文件的富文本 / HTML / SVG 内容
/// - 引入 `<iframe>`、`<webview>`，或让 webview 导航到远程 origin
/// - 放宽 CSP 的 `script-src`（例如为了加载第三方脚本或统计 SDK）
/// - 引入任何在 renderer 内执行外部代码的机制
///
/// 相比之下 `cwd` 的处理**不属于**这条豁免：它是磁盘上扫来的项目路径，正常使用
/// 就可能含 `$(...)`，与 renderer 是否可信无关，因此在
/// `session_manager::terminal::shell_escape` 里做了完整的单引号转义。
#[tauri::command]
pub async fn launch_session_terminal(
    command: String,
    cwd: Option<String>,
    custom_config: Option<String>,
) -> Result<bool, String> {
    let command = command.clone();
    let cwd = cwd.clone();
    let custom_config = custom_config.clone();

    // Read preferred terminal from global settings
    let preferred = crate::settings::get_preferred_terminal();
    // Map global setting terminal names to session terminal names
    // Global uses "iterm2", session terminal uses "iterm"
    let target = match preferred.as_deref() {
        Some("iterm2") => "iterm".to_string(),
        Some(t) => t.to_string(),
        None => "terminal".to_string(), // Default to Terminal.app on macOS
    };

    tauri::async_runtime::spawn_blocking(move || {
        session_manager::terminal::launch_terminal(
            &target,
            &command,
            cwd.as_deref(),
            custom_config.as_deref(),
        )
    })
    .await
    .map_err(|e| format!("Failed to launch terminal: {e}"))??;

    Ok(true)
}

#[tauri::command]
pub async fn delete_session(
    providerId: String,
    sessionId: String,
    sourcePath: String,
) -> Result<bool, String> {
    let provider_id = providerId.clone();
    let session_id = sessionId.clone();
    let source_path = sourcePath.clone();

    tauri::async_runtime::spawn_blocking(move || {
        session_manager::delete_session(&provider_id, &session_id, &source_path)
    })
    .await
    .map_err(|e| format!("Failed to delete session: {e}"))?
}

#[tauri::command]
pub async fn delete_sessions(
    items: Vec<session_manager::DeleteSessionRequest>,
) -> Result<Vec<session_manager::DeleteSessionOutcome>, String> {
    tauri::async_runtime::spawn_blocking(move || session_manager::delete_sessions(&items))
        .await
        .map_err(|e| format!("Failed to delete sessions: {e}"))
}

#[tauri::command]
pub async fn export_session_markdown(filePath: String, content: String) -> Result<(), String> {
    let path = PathBuf::from(filePath);
    tauri::async_runtime::spawn_blocking(move || {
        crate::config::write_text_file(&path, &content)
            .map_err(|e| format!("Failed to export markdown: {e}"))
    })
    .await
    .map_err(|e| format!("Failed to export markdown: {e}"))?
}
