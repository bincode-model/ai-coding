import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { useSessionOrganizer } from "@/hooks/useSessionOrganizer";

const STORAGE_KEY = "ai-coding-session-organizer";
const key = "codex:session-1:/original/session.jsonl";

describe("useSessionOrganizer", () => {
  beforeEach(() => localStorage.removeItem(STORAGE_KEY));

  it("keeps archive and pin independent across remounts", () => {
    const first = renderHook(useSessionOrganizer);
    act(() => first.result.current.togglePin(key));
    act(() => first.result.current.toggleArchive(key));
    expect(first.result.current.pinnedKeys.has(key)).toBe(true);
    expect(first.result.current.archivedKeys.has(key)).toBe(true);
    first.unmount();
    const second = renderHook(useSessionOrganizer);
    expect(second.result.current.pinnedKeys.has(key)).toBe(true);
    expect(second.result.current.archivedKeys.has(key)).toBe(true);
    act(() => second.result.current.togglePin(key));
    expect(second.result.current.pinnedKeys.has(key)).toBe(false);
    expect(second.result.current.archivedKeys.has(key)).toBe(true);
    act(() => second.result.current.togglePin(key));
    act(() => second.result.current.toggleArchive(key));
    expect(second.result.current.pinnedKeys.has(key)).toBe(true);
    expect(second.result.current.archivedKeys.has(key)).toBe(false);
  });

  it("loads legacy state and persists trimmed custom names without changing pin or archive", () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ pinned: [key], archived: [key] }),
    );
    const first = renderHook(useSessionOrganizer);
    expect(first.result.current.names).toEqual({});
    act(() => {
      expect(first.result.current.rename(key, "  客户项目复盘  ")).toBe(true);
    });
    first.unmount();
    const second = renderHook(useSessionOrganizer);
    expect(second.result.current.names[key]).toBe("客户项目复盘");
    expect(second.result.current.pinnedKeys.has(key)).toBe(true);
    expect(second.result.current.archivedKeys.has(key)).toBe(true);
    act(() => {
      expect(second.result.current.rename(key, "  ")).toBe(true);
    });
    expect(second.result.current.names[key]).toBeUndefined();
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!).names).toEqual({});
  });

  it("validates trimmed names at 120 characters and keeps the previous name when rejected", () => {
    const { result } = renderHook(useSessionOrganizer);
    const maxName = "名".repeat(120);
    act(() => {
      expect(result.current.rename(key, ` ${maxName} `)).toBe(true);
    });
    act(() => {
      expect(result.current.rename(key, `${maxName}多`)).toBe(false);
    });
    expect(result.current.names[key]).toBe(maxName);
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!).names[key]).toBe(
      maxName,
    );
  });

  it("removes only explicitly deleted keys and preserves unseen sessions", () => {
    const unseen = "claude:other:/temporarily-unavailable/session.jsonl";
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        pinned: [key, unseen],
        archived: [key, unseen],
        names: { [key]: "删除目标", [unseen]: "保留目标" },
      }),
    );
    const { result } = renderHook(useSessionOrganizer);
    act(() => result.current.removeSessions(new Set([key])));
    expect([...result.current.pinnedKeys]).toEqual([unseen]);
    expect([...result.current.archivedKeys]).toEqual([unseen]);
    expect(result.current.names).toEqual({ [unseen]: "保留目标" });
  });

  it("ignores malformed stored entries without losing valid local names", () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        pinned: [key, 2],
        archived: null,
        names: {
          [key]: "  正常名称  ",
          invalid: 42,
          blank: "  ",
          tooLong: "名".repeat(121),
        },
      }),
    );
    const { result } = renderHook(useSessionOrganizer);
    expect([...result.current.pinnedKeys]).toEqual([key]);
    expect([...result.current.archivedKeys]).toEqual([]);
    expect(result.current.names).toEqual({ [key]: "正常名称" });
  });
});
