import { useEffect, useMemo, useRef, useState } from "react";
import { sessionsApi } from "@/lib/api/sessions";
import type { SessionMeta, SessionSearchHit, SessionSearchMode } from "@/types";
import { getSessionKey } from "@/components/sessions/utils";

const DEBOUNCE_MS = 300;
let nextSearchId = 0;

interface UseSessionContentSearchOptions {
  sessions: SessionMeta[];
  query: string;
  mode: SessionSearchMode;
  /** Explicit manual refresh, not every background list fetch. */
  refreshVersion?: number;
}

interface UseSessionContentSearchResult {
  hits: Map<string, SessionSearchHit>;
  isSearching: boolean;
  error: string | null;
}

export function useSessionContentSearch({
  sessions,
  query,
  mode,
  refreshVersion = 0,
}: UseSessionContentSearchOptions): UseSessionContentSearchResult {
  const [hits, setHits] = useState<Map<string, SessionSearchHit>>(
    () => new Map(),
  );
  const [isSearching, setIsSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestSeq = useRef(0);
  const [completedRun, setCompletedRun] = useState(0);
  const active = useRef<{
    id: string;
    context: string;
    refreshVersion: number;
  } | null>(null);
  useEffect(
    () => () => {
      requestSeq.current += 1;
      if (active.current)
        void sessionsApi.cancelSearch(active.current.id).catch(() => undefined);
      active.current = null;
    },
    [],
  );
  const completed = useRef({
    context: "",
    refreshVersion,
    revisions: new Map<string, string>(),
    hits: new Map<string, SessionSearchHit>(),
  });
  const needle = query.trim();
  const identity = useMemo(
    () =>
      JSON.stringify(
        sessions
          .map((session) => [
            getSessionKey(session),
            session.lastActiveAt ?? 0,
            session.summary ?? "",
          ])
          .sort((a, b) => String(a[0]).localeCompare(String(b[0]))),
      ),
    [sessions],
  );

  useEffect(() => {
    const context = JSON.stringify([needle, mode]);
    const entries = sessions
      .filter((session) => Boolean(session.sourcePath))
      .map((session) => ({
        key: getSessionKey(session),
        revision: JSON.stringify([
          session.lastActiveAt ?? 0,
          session.summary ?? "",
        ]),
        item: {
          providerId: session.providerId,
          sessionId: session.sessionId,
          sourcePath: session.sourcePath!,
        },
      }));
    const activeContext = JSON.stringify([
      needle,
      mode,
      [...new Set(entries.map((entry) => entry.item.providerId))].sort(),
    ]);
    if (active.current) {
      if (
        active.current.context === activeContext &&
        active.current.refreshVersion === refreshVersion
      ) {
        // Let a cold full search finish while CLI activity accumulates. Its completion
        // schedules only the changed records, instead of starving every search.
        return;
      }
      void sessionsApi.cancelSearch(active.current.id).catch(() => undefined);
      active.current = null;
      requestSeq.current += 1;
    }
    const previous = completed.current;
    const sameQuery = previous.context === context;
    const validKeys = new Set(entries.map((entry) => entry.key));
    const retained = sameQuery
      ? new Map([...previous.hits].filter(([key]) => validKeys.has(key)))
      : new Map<string, SessionSearchHit>();
    const force = !sameQuery || previous.refreshVersion !== refreshVersion;
    const changed = entries.filter(
      (entry) => force || previous.revisions.get(entry.key) !== entry.revision,
    );
    setError(null);

    if (!needle || entries.length === 0) {
      completed.current = {
        context: "",
        refreshVersion,
        revisions: new Map(),
        hits: new Map(),
      };
      setHits(new Map());
      setIsSearching(false);
      return;
    }
    setHits((current) =>
      sameQuery &&
      current.size === retained.size &&
      [...current].every(([key, hit]) => retained.get(key) === hit)
        ? current
        : retained,
    );
    if (changed.length === 0) {
      completed.current = {
        ...previous,
        hits: retained,
        revisions: new Map(entries.map((entry) => [entry.key, entry.revision])),
      };
      setIsSearching(false);
      return;
    }
    setIsSearching(true);
    const requestId = `session-search-${Date.now()}-${++nextSearchId}`;
    const seq = ++requestSeq.current;
    const timer = setTimeout(() => {
      active.current = {
        id: requestId,
        context: activeContext,
        refreshVersion,
      };
      void (async () => {
        try {
          const items = changed.map((entry) => entry.item);
          const results = await sessionsApi.searchContents(
            items,
            needle,
            mode,
            items.length,
            requestId,
          );
          if (seq !== requestSeq.current) return;
          const next = new Map(retained);
          changed.forEach((entry) => next.delete(entry.key));
          for (const hit of results) {
            const key = getSessionKey(hit);
            if (validKeys.has(key)) next.set(key, hit);
          }
          completed.current = {
            context,
            refreshVersion,
            revisions: new Map(
              entries.map((entry) => [entry.key, entry.revision]),
            ),
            hits: next,
          };
          setHits(next);
          setError(null);
          setCompletedRun((value) => value + 1);
        } catch (searchError) {
          if (seq !== requestSeq.current) return;
          setError(
            searchError instanceof Error
              ? searchError.message
              : String(searchError),
          );
        } finally {
          if (seq === requestSeq.current) {
            active.current = null;
            setIsSearching(false);
          }
        }
      })();
    }, DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
    };
    // Identity captures only fields affecting original fulltext; renaming is metadata-only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [needle, identity, mode, refreshVersion, completedRun]);

  return { hits, isSearching, error };
}
