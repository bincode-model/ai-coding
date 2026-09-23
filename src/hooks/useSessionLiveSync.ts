import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTauriEvent } from "@/hooks/useTauriEvent";
import { sessionsApi } from "@/lib/api/sessions";
import type { SessionChangedPayload, SessionFileStat } from "@/types";

/** 后端 notify 监听到会话文件变化时推送的事件名。 */
export const SESSION_CHANGED_EVENT = "session-manager://changed";

/** 前端合并事件的节流窗口（毫秒）。 */
const EVENT_THROTTLE_MS = 3000;
/** 兜底轮询：对当前打开的会话文件做一次轻量 stat 的间隔（毫秒）。 */
const STAT_POLL_MS = 3000;

interface UseSessionLiveSyncOptions {
  providerId?: string;
  sourcePath?: string;
}

/**
 * 让「会话管理」页跟着终端里的对话实时更新。
 *
 * 两条通道互不依赖、可单独失效：
 * 1. 事件通道：后端 `session-manager://changed`（400ms 防抖）→ 前端 3 秒
 *    节流后 invalidate 会话列表；若变化的文件正是当前打开的会话，则同时
 *    invalidate 该会话的消息查询。
 * 2. 兜底通道：每 3 秒对当前会话文件做一次 mtime / size 的 stat（几十字节
 *    IPC），只有 mtime 晚于上次拉取消息的时间才重载——事件通道刚刷过就
 *    不会重复刷，写到一半的半行会在下一轮 stat 时自然收敛。
 *
 * 卸载页面时监听与轮询一并停止，没有常驻开销。旧后端没有 stat 命令时
 * 静默忽略，不弹 toast。
 */
export function useSessionLiveSync({
  providerId,
  sourcePath,
}: UseSessionLiveSyncOptions) {
  const queryClient = useQueryClient();
  const throttleRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingCurrentRef = useRef(false);
  const previousStatRef = useRef<{ key: string; stat: SessionFileStat } | null>(
    null,
  );
  const currentRef = useRef<{ providerId?: string; sourcePath?: string }>({});
  currentRef.current = { providerId, sourcePath };

  useTauriEvent<SessionChangedPayload>(SESSION_CHANGED_EVENT, (payload) => {
    const current = currentRef.current;
    if (
      current.sourcePath &&
      payload?.providerId === current.providerId &&
      Array.isArray(payload.paths) &&
      payload.paths.includes(current.sourcePath)
    ) {
      pendingCurrentRef.current = true;
    }
    if (throttleRef.current) return;
    throttleRef.current = setTimeout(() => {
      throttleRef.current = null;
      void queryClient.invalidateQueries({ queryKey: ["sessions"] });
      if (pendingCurrentRef.current) {
        pendingCurrentRef.current = false;
        const latest = currentRef.current;
        if (latest.providerId && latest.sourcePath) {
          void queryClient.invalidateQueries({
            queryKey: ["sessionMessages", latest.providerId, latest.sourcePath],
          });
        }
      }
    }, EVENT_THROTTLE_MS);
  });

  useEffect(() => {
    return () => {
      if (throttleRef.current) {
        clearTimeout(throttleRef.current);
        throttleRef.current = null;
      }
    };
  }, []);

  const statEnabled = Boolean(
    providerId && sourcePath && !sourcePath.startsWith("sqlite:"),
  );

  const { data: stat } = useQuery<SessionFileStat | null>({
    queryKey: ["sessionFileStat", providerId, sourcePath],
    queryFn: async () => {
      try {
        return await sessionsApi.getFileStat(providerId!, sourcePath!);
      } catch {
        // 旧后端没有该命令，或文件暂时不可读：静默降级
        return null;
      }
    },
    enabled: statEnabled,
    refetchInterval: STAT_POLL_MS,
    refetchIntervalInBackground: false,
    staleTime: 0,
    gcTime: 10_000,
    retry: false,
  });

  useEffect(() => {
    if (!stat || !providerId || !sourcePath) return;
    const key = `${providerId}:${sourcePath}`;
    const previous = previousStatRef.current;
    previousStatRef.current = { key, stat };
    const state = queryClient.getQueryState([
      "sessionMessages",
      providerId,
      sourcePath,
    ]);
    const lastFetched = state?.dataUpdatedAt ?? 0;
    const changed =
      previous?.key === key &&
      (previous.stat.mtimeMs !== stat.mtimeMs ||
        previous.stat.size !== stat.size);
    if (changed || stat.mtimeMs > lastFetched) {
      void queryClient.invalidateQueries({
        queryKey: ["sessionMessages", providerId, sourcePath],
      });
      void queryClient.invalidateQueries({ queryKey: ["sessions"] });
    }
  }, [stat, providerId, sourcePath, queryClient]);
}
