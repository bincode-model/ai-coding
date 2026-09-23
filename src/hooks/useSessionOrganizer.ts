import { useCallback, useMemo, useState } from "react";

const STORAGE_KEY = "ai-coding-session-organizer";

interface OrganizerState {
  pinned: string[];
  archived: string[];
  names: Record<string, string>;
}

export const SESSION_NAME_MAX_LENGTH = 120;

const emptyState = (): OrganizerState => ({
  pinned: [],
  archived: [],
  names: {},
});

const sanitizeNames = (value: unknown): Record<string, string> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? Object.fromEntries(
        Object.entries(value)
          .filter(
            ([, name]) =>
              typeof name === "string" &&
              name.trim().length > 0 &&
              name.trim().length <= SESSION_NAME_MAX_LENGTH,
          )
          .map(([key, name]) => [key, (name as string).trim()]),
      )
    : {};

const sanitizeKeys = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];

const readState = (): OrganizerState => {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return emptyState();
    const parsed = JSON.parse(raw) as Partial<OrganizerState>;
    return {
      pinned: sanitizeKeys(parsed.pinned),
      archived: sanitizeKeys(parsed.archived),
      names: sanitizeNames(parsed.names),
    };
  } catch {
    return emptyState();
  }
};

const writeState = (state: OrganizerState) => {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // localStorage 不可用时静默降级为仅内存状态
  }
};

/**
 * 会话置顶、归档与本地名称互相独立，不改写原始会话文件。
 * 状态持久化在 localStorage，key 为会话的 getSessionKey 结果。
 */
export function useSessionOrganizer() {
  const [state, setState] = useState<OrganizerState>(readState);

  const pinnedKeys = useMemo(() => new Set(state.pinned), [state.pinned]);
  const archivedKeys = useMemo(() => new Set(state.archived), [state.archived]);

  const pinnedOrder = useCallback(
    (key: string) => state.pinned.indexOf(key),
    [state.pinned],
  );

  const update = useCallback(
    (updater: (current: OrganizerState) => OrganizerState) => {
      setState((current) => {
        const next = updater(current);
        if (next === current) return current;
        writeState(next);
        return next;
      });
    },
    [],
  );

  const togglePin = useCallback(
    (key: string) => {
      update((current) =>
        current.pinned.includes(key)
          ? { ...current, pinned: current.pinned.filter((k) => k !== key) }
          : { ...current, pinned: [...current.pinned, key] },
      );
    },
    [update],
  );

  const toggleArchive = useCallback(
    (key: string) => {
      update((current) =>
        current.archived.includes(key)
          ? { ...current, archived: current.archived.filter((k) => k !== key) }
          : {
              ...current,
              archived: [...current.archived, key],
            },
      );
    },
    [update],
  );

  const rename = useCallback(
    (key: string, value: string) => {
      const name = value.trim();
      if (name.length > SESSION_NAME_MAX_LENGTH) return false;
      update((current) => {
        const names = { ...current.names };
        if (name) names[key] = name;
        else delete names[key];
        return { ...current, names };
      });
      return true;
    },
    [update],
  );

  // 仅明确删除成功才清理；扫描暂缺、切换 Agent 不代表会话已删除。
  const removeSessions = useCallback(
    (deletedKeys: Set<string>) => {
      update((current) => {
        const pinned = current.pinned.filter((key) => !deletedKeys.has(key));
        const archived = current.archived.filter(
          (key) => !deletedKeys.has(key),
        );
        const names = Object.fromEntries(
          Object.entries(current.names).filter(
            ([key]) => !deletedKeys.has(key),
          ),
        );
        if (
          pinned.length === current.pinned.length &&
          archived.length === current.archived.length &&
          Object.keys(names).length === Object.keys(current.names).length
        ) {
          return current;
        }
        return { pinned, archived, names };
      });
    },
    [update],
  );

  return {
    pinnedKeys,
    archivedKeys,
    pinnedOrder,
    togglePin,
    toggleArchive,
    names: state.names,
    rename,
    removeSessions,
  };
}
