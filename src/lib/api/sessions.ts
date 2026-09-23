import { invoke } from "@tauri-apps/api/core";
import type {
  SessionFileStat,
  SessionMessage,
  SessionMeta,
  SessionRef,
  SessionSearchHit,
  SessionSearchMode,
} from "@/types";

export interface DeleteSessionOptions {
  providerId: string;
  sessionId: string;
  sourcePath: string;
}

export interface DeleteSessionResult extends DeleteSessionOptions {
  success: boolean;
  error?: string;
}

export interface ExportSessionMarkdownOptions {
  filePath: string;
  content: string;
}

export const sessionsApi = {
  async prepareResume(
    options: DeleteSessionOptions,
  ): Promise<{ command: string; cwd?: string | null }> {
    return await invoke("prepare_session_resume", { ...options });
  },
  async list(): Promise<SessionMeta[]> {
    return await invoke("list_sessions");
  },

  async getMessages(
    providerId: string,
    sourcePath: string,
  ): Promise<SessionMessage[]> {
    return await invoke("get_session_messages", { providerId, sourcePath });
  },

  /** 会话源文件的 mtime / size；SQLite 来源或文件不存在时为 null */
  async getFileStat(
    providerId: string,
    sourcePath: string,
  ): Promise<SessionFileStat | null> {
    const result = await invoke<SessionFileStat | null>(
      "get_session_file_stat",
      { providerId, sourcePath },
    );
    return result ?? null;
  },

  /** 在给定会话集合中检索聊天正文，支持精准与模糊匹配 */
  async searchContents(
    items: SessionRef[],
    query: string,
    mode: SessionSearchMode,
    limit = 200,
    requestId?: string,
  ): Promise<SessionSearchHit[]> {
    const result = await invoke<SessionSearchHit[]>("search_session_contents", {
      items,
      query,
      mode,
      limit,
      requestId,
    });
    return result ?? [];
  },

  async cancelSearch(requestId: string): Promise<void> {
    await invoke("cancel_session_search", { requestId });
  },

  async delete(options: DeleteSessionOptions): Promise<boolean> {
    const { providerId, sessionId, sourcePath } = options;
    return await invoke("delete_session", {
      providerId,
      sessionId,
      sourcePath,
    });
  },

  async deleteMany(
    items: DeleteSessionOptions[],
  ): Promise<DeleteSessionResult[]> {
    return await invoke("delete_sessions", { items });
  },

  async launchTerminal(options: {
    command: string;
    cwd?: string | null;
    customConfig?: string | null;
  }): Promise<boolean> {
    const { command, cwd, customConfig } = options;
    return await invoke("launch_session_terminal", {
      command,
      cwd,
      customConfig,
    });
  },

  async exportMarkdown(options: ExportSessionMarkdownOptions): Promise<void> {
    const { filePath, content } = options;
    await invoke("export_session_markdown", { filePath, content });
  },
};
