import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSessionContentSearch } from "@/hooks/useSessionContentSearch";
import { sessionsApi } from "@/lib/api/sessions";
import type { SessionMeta, SessionSearchHit, SessionSearchMode } from "@/types";

const session: SessionMeta = {
  providerId: "codex",
  sessionId: "session-1",
  sourcePath: "/mock/session-1.jsonl",
};
const hit: SessionSearchHit = {
  providerId: session.providerId,
  sessionId: session.sessionId,
  sourcePath: session.sourcePath!,
  matchCount: 1,
  score: 901,
  snippet: "修复 会话搜索",
};

const flushDebounce = () =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(300);
  });

describe("useSessionContentSearch", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(sessionsApi, "cancelSearch").mockResolvedValue(undefined);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("keeps the selected fulltext-only hit visible while the same search refreshes", async () => {
    let finishRefresh!: (hits: SessionSearchHit[]) => void;
    vi.spyOn(sessionsApi, "searchContents")
      .mockResolvedValueOnce([hit])
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finishRefresh = resolve;
          }),
      );
    const { result, rerender } = renderHook(
      ({ refreshVersion }) =>
        useSessionContentSearch({
          sessions: [session],
          query: "修复会话",
          mode: "exact",
          refreshVersion,
        }),
      { initialProps: { refreshVersion: 1 } },
    );
    await flushDebounce();
    rerender({ refreshVersion: 2 });
    expect(result.current.hits.size).toBe(1);
    expect(result.current.isSearching).toBe(true);
    await flushDebounce();
    expect(result.current.hits.size).toBe(1);
    await act(async () => finishRefresh([hit]));
    expect(result.current.hits.size).toBe(1);
    expect(result.current.isSearching).toBe(false);
  });

  it("clears fuzzy hits immediately when switching to exact mode", async () => {
    const searchContents = vi
      .spyOn(sessionsApi, "searchContents")
      .mockResolvedValueOnce([hit])
      .mockResolvedValueOnce([]);
    const { result, rerender } = renderHook(
      ({ mode }: { mode: SessionSearchMode }) =>
        useSessionContentSearch({
          sessions: [session],
          query: "修复会话",
          mode,
        }),
      { initialProps: { mode: "fuzzy" as SessionSearchMode } },
    );

    await flushDebounce();
    expect(result.current.hits.size).toBe(1);

    rerender({ mode: "exact" });
    expect(result.current.hits.size).toBe(0);
    expect(result.current.isSearching).toBe(true);

    await flushDebounce();
    expect(searchContents).toHaveBeenLastCalledWith(
      [session],
      "修复会话",
      "exact",
      1,
      expect.any(String),
    );
    expect(result.current.hits.size).toBe(0);
    expect(result.current.isSearching).toBe(false);
  });

  it("does not restore fuzzy results when an older request finishes last", async () => {
    let resolveFuzzy!: (hits: SessionSearchHit[]) => void;
    vi.spyOn(sessionsApi, "searchContents")
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveFuzzy = resolve;
          }),
      )
      .mockResolvedValueOnce([]);
    const { result, rerender } = renderHook(
      ({ mode }: { mode: SessionSearchMode }) =>
        useSessionContentSearch({
          sessions: [session],
          query: "修复会话",
          mode,
        }),
      { initialProps: { mode: "fuzzy" as SessionSearchMode } },
    );

    await flushDebounce();
    rerender({ mode: "exact" });
    await flushDebounce();
    await act(async () => resolveFuzzy([hit]));

    expect(result.current.hits.size).toBe(0);
    expect(result.current.isSearching).toBe(false);
  });

  it("clears previous hits when the query changes or is emptied", async () => {
    const searchContents = vi
      .spyOn(sessionsApi, "searchContents")
      .mockResolvedValue([hit]);
    const { result, rerender } = renderHook(
      ({ query }) =>
        useSessionContentSearch({ sessions: [session], query, mode: "fuzzy" }),
      { initialProps: { query: "修复会话" } },
    );

    await flushDebounce();
    expect(result.current.hits.size).toBe(1);
    rerender({ query: "其他内容" });
    expect(result.current.hits.size).toBe(0);

    rerender({ query: "   " });
    await flushDebounce();
    expect(searchContents).toHaveBeenCalledTimes(1);
    expect(result.current.isSearching).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("requests enough results for every candidate instead of stopping at 200", async () => {
    const searchContents = vi
      .spyOn(sessionsApi, "searchContents")
      .mockResolvedValue([]);
    const sessions = Array.from({ length: 250 }, (_, index) => ({
      ...session,
      sessionId: `session-${index}`,
      sourcePath: `/mock/session-${index}.jsonl`,
    }));
    renderHook(() =>
      useSessionContentSearch({ sessions, query: "会话", mode: "exact" }),
    );

    await flushDebounce();
    expect(searchContents).toHaveBeenCalledWith(
      sessions,
      "会话",
      "exact",
      250,
      expect.any(String),
    );
  });

  it("searches again after an explicit refresh even when query and metadata are unchanged", async () => {
    const refreshedHit = { ...hit, snippet: "修复会话后的新增内容" };
    const searchContents = vi
      .spyOn(sessionsApi, "searchContents")
      .mockResolvedValueOnce([hit])
      .mockResolvedValueOnce([refreshedHit]);
    const { result, rerender } = renderHook(
      ({ refreshVersion }) =>
        useSessionContentSearch({
          sessions: [session],
          query: "修复会话",
          mode: "exact",
          refreshVersion,
        }),
      { initialProps: { refreshVersion: 1 } },
    );

    await flushDebounce();
    expect([...result.current.hits.values()]).toEqual([hit]);
    rerender({ refreshVersion: 2 });
    expect([...result.current.hits.values()]).toEqual([hit]);
    await flushDebounce();

    expect(searchContents).toHaveBeenCalledTimes(2);
    expect([...result.current.hits.values()]).toEqual([refreshedHit]);
  });

  it("invalidates content when lastActiveAt changes without changing session identity", async () => {
    const searchContents = vi
      .spyOn(sessionsApi, "searchContents")
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([hit]);
    const { result, rerender } = renderHook(
      ({ lastActiveAt }) =>
        useSessionContentSearch({
          sessions: [{ ...session, lastActiveAt }],
          query: "修复会话",
          mode: "exact",
        }),
      { initialProps: { lastActiveAt: 100 } },
    );

    await flushDebounce();
    rerender({ lastActiveAt: 100 });
    await flushDebounce();
    expect(searchContents).toHaveBeenCalledTimes(1);

    rerender({ lastActiveAt: 200 });
    await flushDebounce();
    expect(searchContents).toHaveBeenCalledTimes(2);
    expect([...result.current.hits.values()]).toEqual([hit]);
  });

  it("does not leak a pending previous Agent result into the newly selected Agent", async () => {
    let resolveCodex!: (hits: SessionSearchHit[]) => void;
    const claudeSession = {
      ...session,
      providerId: "claude",
      sourcePath: "/mock/claude-session.jsonl",
    };
    const claudeHit = { ...hit, ...claudeSession, snippet: "Claude 修复会话" };
    const searchContents = vi
      .spyOn(sessionsApi, "searchContents")
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveCodex = resolve;
          }),
      )
      .mockResolvedValueOnce([claudeHit]);
    const { result, rerender } = renderHook(
      ({ candidate }) =>
        useSessionContentSearch({
          sessions: [candidate],
          query: "修复会话",
          mode: "exact",
        }),
      { initialProps: { candidate: session } },
    );

    await flushDebounce();
    rerender({ candidate: claudeSession });
    expect(result.current.hits.size).toBe(0);
    await flushDebounce();
    expect(searchContents).toHaveBeenLastCalledWith(
      [claudeSession],
      "修复会话",
      "exact",
      1,
      expect.any(String),
    );
    await act(async () => resolveCodex([hit]));
    expect([...result.current.hits.values()]).toEqual([claudeHit]);
    expect(result.current.isSearching).toBe(false);
  });

  it("discards errors from an in-flight search after unmount", async () => {
    let rejectSearch!: (error: Error) => void;
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.spyOn(sessionsApi, "searchContents").mockImplementation(
      () =>
        new Promise((_resolve, reject) => {
          rejectSearch = reject;
        }),
    );
    const { unmount } = renderHook(() =>
      useSessionContentSearch({
        sessions: [session],
        query: "修复会话",
        mode: "exact",
      }),
    );

    await flushDebounce();
    unmount();
    await act(async () => rejectSearch(new Error("obsolete request failed")));
    expect(warn).not.toHaveBeenCalled();
  });

  it("cancels a pending debounced search when its page unmounts", async () => {
    const searchContents = vi
      .spyOn(sessionsApi, "searchContents")
      .mockResolvedValue([]);
    const { unmount } = renderHook(() =>
      useSessionContentSearch({
        sessions: [session],
        query: "修复会话",
        mode: "exact",
      }),
    );

    unmount();
    await flushDebounce();
    expect(searchContents).not.toHaveBeenCalled();
  });
  it("searches only changed conversations after background activity and preserves other hits", async () => {
    const second = {
      ...session,
      sessionId: "second",
      sourcePath: "/mock/second.jsonl",
      lastActiveAt: 10,
    };
    const secondHit = { ...hit, ...second, snippet: "second match" };
    const api = vi
      .spyOn(sessionsApi, "searchContents")
      .mockResolvedValueOnce([hit, secondHit])
      .mockResolvedValueOnce([]);
    const { result, rerender } = renderHook(
      ({ timestamp }) =>
        useSessionContentSearch({
          sessions: [{ ...session, lastActiveAt: timestamp }, second],
          query: "匹配",
          mode: "exact",
        }),
      { initialProps: { timestamp: 10 } },
    );
    await flushDebounce();
    rerender({ timestamp: 20 });
    await flushDebounce();
    expect(api.mock.calls[1][0]).toEqual([session]);
    expect([...result.current.hits.values()]).toEqual([secondHit]);
  });

  it("cancels obsolete native work as soon as the query changes", async () => {
    const api = vi
      .spyOn(sessionsApi, "searchContents")
      .mockImplementation(() => new Promise(() => {}));
    const { rerender } = renderHook(
      ({ query }) =>
        useSessionContentSearch({ sessions: [session], query, mode: "exact" }),
      { initialProps: { query: "旧查询" } },
    );
    await flushDebounce();
    const oldRequest = api.mock.calls[0][4];
    rerender({ query: "新查询" });
    expect(sessionsApi.cancelSearch).toHaveBeenCalledWith(oldRequest);
    await flushDebounce();
    expect(api.mock.calls[1][4]).not.toBe(oldRequest);
  });

  it("does not rescan unchanged content when sessions are renamed or reordered", async () => {
    const api = vi
      .spyOn(sessionsApi, "searchContents")
      .mockResolvedValue([hit]);
    const { rerender } = renderHook(
      ({ title }) =>
        useSessionContentSearch({
          sessions: [{ ...session, title }],
          query: "测试",
          mode: "exact",
        }),
      { initialProps: { title: "原名" } },
    );
    await flushDebounce();
    rerender({ title: "自定义名称" });
    await flushDebounce();
    expect(api).toHaveBeenCalledTimes(1);
  });
  it("lets a cold search finish during CLI writes then searches only updated records", async () => {
    let finish!: (hits: SessionSearchHit[]) => void;
    const api = vi
      .spyOn(sessionsApi, "searchContents")
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finish = resolve;
          }),
      )
      .mockResolvedValueOnce([hit]);
    const { result, rerender } = renderHook(
      ({ time }) =>
        useSessionContentSearch({
          sessions: [{ ...session, lastActiveAt: time }],
          query: "持续写入",
          mode: "exact",
        }),
      { initialProps: { time: 1 } },
    );
    await flushDebounce();
    rerender({ time: 2 });
    await flushDebounce();
    expect(api).toHaveBeenCalledTimes(1);
    expect(sessionsApi.cancelSearch).not.toHaveBeenCalled();
    await act(async () => finish([]));
    await flushDebounce();
    expect(api).toHaveBeenCalledTimes(2);
    expect(result.current.hits.size).toBe(1);
  });

  it("does not continuously retry failed searches", async () => {
    const api = vi
      .spyOn(sessionsApi, "searchContents")
      .mockRejectedValue(new Error("disk unavailable"));
    const { result } = renderHook(() =>
      useSessionContentSearch({
        sessions: [session],
        query: "测试",
        mode: "exact",
      }),
    );
    await flushDebounce();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });
    expect(api).toHaveBeenCalledTimes(1);
    expect(result.current.error).toBe("disk unavailable");
  });
});
