import {
  useCallback,
  useDeferredValue,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useSessionSearch } from "@/hooks/useSessionSearch";
import { useSessionContentSearch } from "@/hooks/useSessionContentSearch";
import { useSessionLiveSync } from "@/hooks/useSessionLiveSync";
import {
  SESSION_NAME_MAX_LENGTH,
  useSessionOrganizer,
} from "@/hooks/useSessionOrganizer";
import { useTranslation } from "react-i18next";
import { useVirtualizer } from "@tanstack/react-virtual";
import { toast } from "sonner";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Archive,
  Copy,
  Download,
  Pin,
  Pencil,
  RefreshCw,
  Search,
  Play,
  Trash2,
  MessageSquare,
  Clock,
  FolderOpen,
  FileText,
  X,
  CheckSquare,
  ListTree,
  List,
  ChevronDown,
  ChevronRight,
  ChevronsDownUp,
} from "lucide-react";
import {
  piKeys,
  useDeleteSessionMutation,
  useSessionMessagesQuery,
  useSessionsQuery,
} from "@/lib/query";
import { piApi, sessionsApi, settingsApi } from "@/lib/api";
import type { SessionMeta, SessionSearchMode } from "@/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from "@/components/ui/select";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { extractErrorMessage } from "@/utils/errorUtils";
import { cn } from "@/lib/utils";
import { isMac } from "@/lib/platform";
import { ProviderIcon } from "@/components/ProviderIcon";
import { SessionItem } from "./SessionItem";
import { SessionMessageItem } from "./SessionMessageItem";
import { SessionTocDialog, SessionTocSidebar } from "./SessionToc";
import {
  extractCodexPromptPreview,
  formatSessionMessagePreview,
  formatSessionTitle,
  formatTimestamp,
  getBaseName,
  getProviderIconName,
  getProviderLabel,
  getSessionDirectoryGroupKey,
  getSessionKey,
  groupSessionsByProviderAndDirectory,
  type SessionDirectoryGroup,
  type SessionProviderGroup,
  shouldHideCodexMessageFromToc,
} from "./utils";

const SESSION_LIST_VIEW_MODE_STORAGE_KEY =
  "ai-coding.sessionManager.listViewMode";
const SESSION_SEARCH_MODE_STORAGE_KEY = "ai-coding.sessionManager.searchMode";
const SESSION_GROUP_EXPANSION_STORAGE_KEY =
  "ai-coding.sessionManager.groupExpansionState";

type ProviderFilter =
  | "all"
  | "codex"
  | "grokbuild"
  | "claude"
  | "opencode"
  | "openclaw"
  | "gemini"
  | "hermes"
  | "pi";

type SessionListViewMode = "flat" | "grouped";

type GroupSelectionState = {
  checked: boolean | "indeterminate";
  isSelected: boolean;
  selectedCount: number;
  selectableCount: number;
};

type SessionGroupExpansionState = {
  expandedProviderIds: Set<string>;
  expandedDirectoryKeys: Set<string>;
};

const readInitialSessionListViewMode = (): SessionListViewMode => {
  if (typeof window === "undefined") return "flat";
  try {
    const stored = window.localStorage.getItem(
      SESSION_LIST_VIEW_MODE_STORAGE_KEY,
    );
    return stored === "grouped" || stored === "flat" ? stored : "flat";
  } catch {
    return "flat";
  }
};

const readInitialSessionSearchMode = (): SessionSearchMode => {
  if (typeof window === "undefined") return "fuzzy";
  try {
    return window.localStorage.getItem(SESSION_SEARCH_MODE_STORAGE_KEY) ===
      "exact"
      ? "exact"
      : "fuzzy";
  } catch {
    return "fuzzy";
  }
};

const readInitialSessionGroupExpansionState =
  (): SessionGroupExpansionState => {
    if (typeof window === "undefined") {
      return {
        expandedProviderIds: new Set(),
        expandedDirectoryKeys: new Set(),
      };
    }

    try {
      const stored = window.localStorage.getItem(
        SESSION_GROUP_EXPANSION_STORAGE_KEY,
      );
      const parsed = stored ? JSON.parse(stored) : null;

      if (!parsed || typeof parsed !== "object") {
        return {
          expandedProviderIds: new Set(),
          expandedDirectoryKeys: new Set(),
        };
      }

      const expandedProviderIds = Array.isArray(parsed.expandedProviderIds)
        ? parsed.expandedProviderIds.filter(
            (providerId: unknown): providerId is string =>
              typeof providerId === "string",
          )
        : [];
      const expandedDirectoryKeys = Array.isArray(parsed.expandedDirectoryKeys)
        ? parsed.expandedDirectoryKeys.filter(
            (directoryKey: unknown): directoryKey is string =>
              typeof directoryKey === "string",
          )
        : [];

      return {
        expandedProviderIds: new Set(expandedProviderIds),
        expandedDirectoryKeys: new Set(expandedDirectoryKeys),
      };
    } catch {
      return {
        expandedProviderIds: new Set(),
        expandedDirectoryKeys: new Set(),
      };
    }
  };

const serializeSessionGroupExpansionState = (
  expandedProviderGroups: Set<string>,
  expandedDirectoryGroups: Set<string>,
) =>
  JSON.stringify({
    expandedProviderIds: Array.from(expandedProviderGroups).sort(),
    expandedDirectoryKeys: Array.from(expandedDirectoryGroups).sort(),
  });

const filterSetToAllowedValues = (
  current: Set<string>,
  allowedValues: Set<string>,
) => {
  let changed = false;
  const next = new Set<string>();

  current.forEach((value) => {
    if (allowedValues.has(value)) {
      next.add(value);
    } else {
      changed = true;
    }
  });

  return changed ? next : current;
};

export function SessionManagerPage({ appId }: { appId: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data, isLoading, refetch, dataUpdatedAt } = useSessionsQuery();
  const {
    pinnedKeys,
    archivedKeys,
    pinnedOrder,
    togglePin,
    toggleArchive,
    names,
    rename,
    removeSessions,
  } = useSessionOrganizer();
  const sessions = useMemo(
    () =>
      (data ?? []).map((session) => {
        const name = names[getSessionKey(session)];
        return name ? { ...session, title: name } : session;
      }),
    [data, names],
  );
  const piSessionDiscovery = useQuery({
    queryKey: piKeys.sessionDiscovery,
    queryFn: () => piApi.getSessionDiscovery(),
    enabled: appId === "pi",
    staleTime: 30 * 1000,
  });
  const detailRef = useRef<HTMLDivElement | null>(null);
  const scrollContainerRef = useRef<HTMLDivElement | null>(null);
  const listScrollRef = useRef<HTMLDivElement | null>(null);
  const [renameTarget, setRenameTarget] = useState<SessionMeta | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [contentRefreshVersion, setContentRefreshVersion] = useState(0);
  const [activeMessageIndex, setActiveMessageIndex] = useState<number | null>(
    null,
  );
  const [tocDialogOpen, setTocDialogOpen] = useState(false);
  const [isSearchOpen, setIsSearchOpen] = useState(false);
  const [deleteTargets, setDeleteTargets] = useState<SessionMeta[] | null>(
    null,
  );
  const [selectedSessionKeys, setSelectedSessionKeys] = useState<Set<string>>(
    () => new Set(),
  );
  const [isBatchDeleting, setIsBatchDeleting] = useState(false);
  const [selectionMode, setSelectionMode] = useState(false);
  const searchInputRef = useRef<HTMLInputElement | null>(null);

  const [search, setSearch] = useState("");
  const deferredSearch = useDeferredValue(search);
  const [searchMode, setSearchMode] = useState<SessionSearchMode>(
    readInitialSessionSearchMode,
  );
  const [providerScope, setProviderScope] = useState({
    appId,
    value: appId as ProviderFilter,
  });
  // Scope changes synchronously with the app; never render a previous Agent's hits.
  const providerFilter =
    providerScope.appId === appId
      ? providerScope.value
      : (appId as ProviderFilter);
  const setProviderFilter = (value: ProviderFilter) =>
    setProviderScope({ appId, value });
  const [recentDays, setRecentDays] = useState("all");
  const [filterNow, setFilterNow] = useState(Date.now);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [listTab, setListTab] = useState<"sessions" | "archived">("sessions");
  const [listViewMode, setListViewMode] = useState<SessionListViewMode>(
    readInitialSessionListViewMode,
  );
  const [initialGroupExpansionState] = useState(
    readInitialSessionGroupExpansionState,
  );
  const [expandedProviderGroups, setExpandedProviderGroups] = useState<
    Set<string>
  >(() => initialGroupExpansionState.expandedProviderIds);
  const [expandedDirectoryGroups, setExpandedDirectoryGroups] = useState<
    Set<string>
  >(() => initialGroupExpansionState.expandedDirectoryKeys);

  useEffect(() => {
    setProviderScope({ appId, value: appId as ProviderFilter });
    setSelectedKey(null);
    setListTab("sessions");
    setSelectedSessionKeys(new Set());
  }, [appId]);
  useEffect(() => {
    setFilterNow(Date.now());
  }, [dataUpdatedAt, recentDays]);
  // 元数据搜索（标题 / 摘要 / 目录 / 会话来源等）
  const { search: searchSessions } = useSessionSearch({
    sessions,
    providerFilter,
    mode: searchMode,
  });

  // 正文全文检索：元数据搜不到的聊天内容由后端按需解析并缓存
  const providerScopedSessions = useMemo(
    () =>
      providerFilter === "all"
        ? sessions
        : sessions.filter((session) => session.providerId === providerFilter),
    [sessions, providerFilter],
  );
  const {
    hits: contentHits,
    isSearching: isSearchingContent,
    error: contentSearchError,
  } = useSessionContentSearch({
    sessions: providerScopedSessions,
    query: deferredSearch,
    mode: searchMode,
    refreshVersion: contentRefreshVersion,
  });

  const filteredSessions = useMemo(() => {
    const base = searchSessions(deferredSearch);
    if (!deferredSearch.trim() || contentHits.size === 0) return base;
    const seen = new Set(base.map((session) => getSessionKey(session)));
    const extra = providerScopedSessions
      .filter((session) => {
        const key = getSessionKey(session);
        return contentHits.has(key) && !seen.has(key);
      })
      .sort((a, b) => {
        const aHit = contentHits.get(getSessionKey(a));
        const bHit = contentHits.get(getSessionKey(b));
        const aScore = aHit?.score ?? aHit?.matchCount ?? 0;
        const bScore = bHit?.score ?? bHit?.matchCount ?? 0;
        if (bScore !== aScore) return bScore - aScore;
        const aTs = a.lastActiveAt ?? a.createdAt ?? 0;
        const bTs = b.lastActiveAt ?? b.createdAt ?? 0;
        return bTs - aTs;
      });
    return [...base, ...extra];
  }, [searchSessions, deferredSearch, contentHits, providerScopedSessions]);

  const recentSessions = useMemo(
    () =>
      filteredSessions.filter(
        (session) =>
          recentDays === "all" ||
          (session.lastActiveAt ?? session.createdAt ?? 0) >=
            filterNow - Number(recentDays) * 24 * 60 * 60 * 1000,
      ),
    [filteredSessions, recentDays, filterNow],
  );

  // 按归档状态拆分当前列表，并把置顶会话排到最前
  const visibleSessions = useMemo(() => {
    const inTab = recentSessions.filter((session) => {
      const isArchived = archivedKeys.has(getSessionKey(session));
      return (
        Boolean(deferredSearch.trim()) ||
        (listTab === "archived"
          ? isArchived
          : !isArchived || pinnedKeys.has(getSessionKey(session)))
      );
    });
    if (recentDays !== "all") {
      return inTab.sort(
        (a, b) =>
          (b.lastActiveAt ?? b.createdAt ?? 0) -
          (a.lastActiveAt ?? a.createdAt ?? 0),
      );
    }
    if (listTab === "archived") return inTab;
    const pinned = inTab.filter((session) =>
      pinnedKeys.has(getSessionKey(session)),
    );
    const rest = inTab.filter(
      (session) => !pinnedKeys.has(getSessionKey(session)),
    );
    pinned.sort(
      (a, b) => pinnedOrder(getSessionKey(a)) - pinnedOrder(getSessionKey(b)),
    );
    return [...pinned, ...rest];
  }, [
    recentSessions,
    listTab,
    pinnedKeys,
    archivedKeys,
    pinnedOrder,
    deferredSearch,
    recentDays,
  ]);

  const visiblePinnedCount = useMemo(
    () =>
      listTab === "sessions" && recentDays === "all"
        ? visibleSessions.filter((session) =>
            pinnedKeys.has(getSessionKey(session)),
          ).length
        : 0,
    [visibleSessions, listTab, pinnedKeys, recentDays],
  );

  const archivedCount = useMemo(
    () =>
      recentSessions.filter((session) =>
        archivedKeys.has(getSessionKey(session)),
      ).length,
    [recentSessions, archivedKeys],
  );
  const activeCount = deferredSearch.trim()
    ? recentSessions.length
    : recentSessions.filter(
        (session) =>
          !archivedKeys.has(getSessionKey(session)) ||
          pinnedKeys.has(getSessionKey(session)),
      ).length;

  // 分类视图：按供应商 / 项目目录分组（基于当前 tab 已过滤出的会话）
  const groupedSessions = useMemo(
    () =>
      groupSessionsByProviderAndDirectory(
        visibleSessions,
        t("sessionManager.unknownDirectory", {
          defaultValue: "未知目录",
        }),
      ),
    [visibleSessions, t],
  );

  const validGroupExpansionKeys = useMemo(
    () => ({
      providerIds: new Set(sessions.map((session) => session.providerId)),
      directoryKeys: new Set(
        sessions.map((session) =>
          getSessionDirectoryGroupKey(session.providerId, session.projectDir),
        ),
      ),
    }),
    [sessions],
  );

  useEffect(() => {
    try {
      window.localStorage.setItem(
        SESSION_LIST_VIEW_MODE_STORAGE_KEY,
        listViewMode,
      );
    } catch {
      // localStorage 不可用时静默降级为仅内存状态
    }
  }, [listViewMode]);

  useEffect(() => {
    try {
      window.localStorage.setItem(SESSION_SEARCH_MODE_STORAGE_KEY, searchMode);
    } catch {
      // localStorage 不可用时静默降级为仅内存状态
    }
  }, [searchMode]);

  useEffect(() => {
    try {
      window.localStorage.setItem(
        SESSION_GROUP_EXPANSION_STORAGE_KEY,
        serializeSessionGroupExpansionState(
          expandedProviderGroups,
          expandedDirectoryGroups,
        ),
      );
    } catch {
      // localStorage 不可用时静默降级为仅内存状态
    }
  }, [expandedDirectoryGroups, expandedProviderGroups]);

  useEffect(() => {
    if (isLoading) return;

    setExpandedProviderGroups((current) =>
      filterSetToAllowedValues(current, validGroupExpansionKeys.providerIds),
    );
    setExpandedDirectoryGroups((current) =>
      filterSetToAllowedValues(current, validGroupExpansionKeys.directoryKeys),
    );
  }, [isLoading, validGroupExpansionKeys]);

  useEffect(() => {
    if (visibleSessions.length === 0) {
      setSelectedKey(null);
      return;
    }
    const exists = selectedKey
      ? visibleSessions.some(
          (session) => getSessionKey(session) === selectedKey,
        )
      : false;
    if (!exists) {
      setSelectedKey(getSessionKey(visibleSessions[0]));
    }
  }, [visibleSessions, selectedKey]);

  const selectedSession = useMemo(() => {
    if (!selectedKey) return null;
    return (
      visibleSessions.find(
        (session) => getSessionKey(session) === selectedKey,
      ) || null
    );
  }, [visibleSessions, selectedKey]);

  const listViewModeLabel =
    listViewMode === "grouped"
      ? t("sessionManager.viewModeGrouped", {
          defaultValue: "分类",
        })
      : t("sessionManager.viewModeFlat", {
          defaultValue: "列表",
        });

  const {
    data: messages = [],
    isLoading: isLoadingMessages,
    error: messagesError,
  } = useSessionMessagesQuery(
    selectedSession?.providerId,
    selectedSession?.sourcePath,
  );

  // 终端里新产生的消息实时同步到当前页面（文件监听事件 + 3 秒 stat 兜底）
  useSessionLiveSync({
    providerId: selectedSession?.providerId,
    sourcePath: selectedSession?.sourcePath,
  });

  // 手动刷新：同时刷新列表与当前会话的消息（原来只刷新列表）
  const handleRefresh = useCallback(() => {
    void refetch().then((result) => {
      if (result.error) toast.error(extractErrorMessage(result.error));
      else setContentRefreshVersion((version) => version + 1);
    });
    if (selectedSession?.providerId && selectedSession.sourcePath) {
      void queryClient.invalidateQueries({
        queryKey: [
          "sessionMessages",
          selectedSession.providerId,
          selectedSession.sourcePath,
        ],
      });
    }
  }, [refetch, queryClient, selectedSession]);
  const deleteSessionMutation = useDeleteSessionMutation();
  const isDeleting = deleteSessionMutation.isPending || isBatchDeleting;

  const virtualizer = useVirtualizer({
    count: messages.length,
    getScrollElement: () => scrollContainerRef.current,
    estimateSize: () => 120,
    overscan: 5,
    gap: 12,
  });

  useEffect(() => {
    if (scrollContainerRef.current) {
      scrollContainerRef.current.scrollTop = 0;
    }
  }, [selectedKey]);

  // 小列表无需虚拟化；大列表仅挂载视口附近的行，名称/摘要换行后动态测高。
  const virtualizeList = listViewMode === "flat" && visibleSessions.length > 50;
  const listVirtualizer = useVirtualizer({
    count: virtualizeList ? visibleSessions.length : 0,
    getScrollElement: () => listScrollRef.current,
    getItemKey: (index) => getSessionKey(visibleSessions[index]),
    estimateSize: () => 86,
    overscan: 6,
    gap: 4,
    enabled: virtualizeList,
  });
  useEffect(() => {
    if (listScrollRef.current) listScrollRef.current.scrollTop = 0;
  }, [providerFilter, listTab, recentDays, deferredSearch, listViewMode]);

  useEffect(() => {
    const validKeys = new Set(
      sessions.map((session) => getSessionKey(session)),
    );
    setSelectedSessionKeys((current) => {
      let changed = false;
      const next = new Set<string>();
      current.forEach((key) => {
        if (validKeys.has(key)) {
          next.add(key);
        } else {
          changed = true;
        }
      });
      return changed ? next : current;
    });
  }, [sessions]);

  const isCodexSession = selectedSession?.providerId === "codex";

  // 提取用户消息用于目录
  const userMessagesToc = useMemo(() => {
    return messages
      .map((msg, index) => ({ msg, index }))
      .filter(({ msg }) => {
        if (msg.role.toLowerCase() !== "user") return false;
        return !(isCodexSession && shouldHideCodexMessageFromToc(msg.content));
      })
      .map(({ msg, index }) => {
        const previewContent = isCodexSession
          ? extractCodexPromptPreview(msg.content)
          : msg.content;

        return {
          index,
          preview: formatSessionMessagePreview(previewContent),
          ts: msg.ts,
        };
      });
  }, [isCodexSession, messages]);

  const scrollToMessage = (index: number) => {
    virtualizer.scrollToIndex(index, { align: "center", behavior: "smooth" });
    setActiveMessageIndex(index);
    setTocDialogOpen(false);
    setTimeout(() => setActiveMessageIndex(null), 2000);
  };

  const handleCopy = useCallback(
    async (text: string, successMessage: string) => {
      try {
        await navigator.clipboard.writeText(text);
        toast.success(successMessage);
      } catch (error) {
        toast.error(
          extractErrorMessage(error) ||
            t("common.error", { defaultValue: "Copy failed" }),
        );
      }
    },
    [t],
  );

  const handleMessageCopy = useCallback(
    (content: string) => {
      void handleCopy(
        content,
        t("sessionManager.messageCopied", { defaultValue: "已复制消息内容" }),
      );
    },
    [handleCopy, t],
  );

  const handleExportMarkdown = useCallback(async () => {
    if (!selectedSession) return;

    const sections = [
      `# ${formatSessionTitle(selectedSession)}`,
      "",
      `- Provider: ${selectedSession.providerId}`,
      `- Session ID: ${selectedSession.sessionId}`,
      selectedSession.projectDir
        ? `- Project: ${selectedSession.projectDir}`
        : null,
      selectedSession.sourcePath
        ? `- Source: ${selectedSession.sourcePath}`
        : null,
      selectedSession.createdAt
        ? `- Created: ${formatTimestamp(selectedSession.createdAt)}`
        : null,
      selectedSession.lastActiveAt
        ? `- Last Active: ${formatTimestamp(selectedSession.lastActiveAt)}`
        : null,
      "",
      "---",
      "",
      ...messages.flatMap((message) => [
        `## ${message.role || "message"}`,
        "",
        message.content || "",
        "",
      ]),
    ].filter((line): line is string => line !== null);

    const markdown = sections.join("\n");
    const safeName = formatSessionTitle(selectedSession)
      .replace(/[\\/:*?"<>|]/g, "-")
      .trim();
    try {
      const filePath = await settingsApi.saveFileDialog(
        `${safeName || selectedSession.sessionId}.md`,
        {
          filterName: "Markdown",
          extensions: ["md"],
        },
      );
      if (!filePath) {
        return;
      }

      await sessionsApi.exportMarkdown({
        filePath,
        content: markdown,
      });

      toast.success(
        t("sessionManager.exportedMarkdown", {
          defaultValue: "已导出 Markdown 文件",
        }),
      );
    } catch (error) {
      toast.error(
        extractErrorMessage(error) ||
          t("sessionManager.exportMarkdownFailed", {
            defaultValue: "导出 Markdown 失败",
          }),
      );
    }
  }, [messages, selectedSession, t]);

  const prepareSelectedResume = async () => {
    if (!selectedSession?.resumeCommand) throw new Error("此会话不支持恢复");
    if (["codex", "claude"].includes(selectedSession.providerId)) {
      if (!selectedSession.sourcePath)
        throw new Error("缺少原始会话文件，无法恢复");
      return await sessionsApi.prepareResume({
        providerId: selectedSession.providerId,
        sessionId: selectedSession.sessionId,
        sourcePath: selectedSession.sourcePath,
      });
    }
    return {
      command: selectedSession.resumeCommand,
      cwd: selectedSession.projectDir,
    };
  };

  const handleCopyResume = async () => {
    try {
      const prepared = await prepareSelectedResume();
      await handleCopy(
        prepared.command,
        t("sessionManager.resumeCommandCopied"),
      );
    } catch (error) {
      toast.error(extractErrorMessage(error) || t("sessionManager.openFailed"));
    }
  };

  const handleResume = async () => {
    let prepared;
    try {
      prepared = await prepareSelectedResume();
    } catch (error) {
      toast.error(extractErrorMessage(error) || t("sessionManager.openFailed"));
      return;
    }
    const { command, cwd } = prepared;

    if (!isMac()) {
      await handleCopy(command, t("sessionManager.resumeCommandCopied"));
      return;
    }

    try {
      await sessionsApi.launchTerminal({
        command,
        cwd: cwd ?? undefined,
      });
      toast.success(t("sessionManager.terminalLaunched"));
    } catch (error) {
      await handleCopy(command, t("sessionManager.resumeFallbackCopied"));
      toast.error(extractErrorMessage(error) || t("sessionManager.openFailed"));
    }
  };

  const handleDeleteConfirm = async () => {
    if (!deleteTargets || deleteTargets.length === 0 || isDeleting) {
      return;
    }

    const targets = deleteTargets.filter((session) => session.sourcePath);
    setDeleteTargets(null);

    if (targets.length === 0) {
      return;
    }

    if (targets.length === 1) {
      const [target] = targets;
      await deleteSessionMutation.mutateAsync({
        providerId: target.providerId,
        sessionId: target.sessionId,
        sourcePath: target.sourcePath!,
      });
      removeSessions(new Set([getSessionKey(target)]));
      setSelectedSessionKeys((current) => {
        const next = new Set(current);
        next.delete(getSessionKey(target));
        return next;
      });
      return;
    }

    setIsBatchDeleting(true);
    try {
      const results = await sessionsApi.deleteMany(
        targets.map((session) => ({
          providerId: session.providerId,
          sessionId: session.sessionId,
          sourcePath: session.sourcePath!,
        })),
      );

      const deletedKeys = results
        .filter((result) => result.success)
        .map(
          (result) =>
            `${result.providerId}:${result.sessionId}:${result.sourcePath ?? ""}`,
        );

      const failedErrors = results
        .filter((result) => !result.success)
        .map((result) => result.error || t("common.unknown"));

      if (deletedKeys.length > 0) {
        const deletedKeySet = new Set(deletedKeys);
        removeSessions(deletedKeySet);
        queryClient.setQueryData<SessionMeta[]>(["sessions"], (current) =>
          (current ?? []).filter(
            (session) => !deletedKeySet.has(getSessionKey(session)),
          ),
        );
      }

      results
        .filter((result) => result.success)
        .forEach((result) => {
          queryClient.removeQueries({
            queryKey: ["sessionMessages", result.providerId, result.sourcePath],
          });
        });

      setSelectedSessionKeys((current) => {
        const next = new Set(current);
        deletedKeys.forEach((key) => next.delete(key));
        return next;
      });

      await queryClient.invalidateQueries({ queryKey: ["sessions"] });

      if (deletedKeys.length > 0) {
        toast.success(
          t("sessionManager.batchDeleteSuccess", {
            defaultValue: "已删除 {{count}} 个会话",
            count: deletedKeys.length,
          }),
        );
      }

      if (failedErrors.length > 0) {
        toast.error(
          t("sessionManager.batchDeleteFailed", {
            defaultValue: "{{failed}} 个会话删除失败",
            failed: failedErrors.length,
          }),
          {
            description: failedErrors[0],
          },
        );
      }
    } catch (error) {
      toast.error(
        extractErrorMessage(error) ||
          t("sessionManager.batchDeleteRequestFailed", {
            defaultValue: "批量删除失败，请稍后重试",
          }),
      );
    } finally {
      setIsBatchDeleting(false);
    }
  };

  const handleTogglePin = useCallback(
    (session: SessionMeta) => {
      const key = getSessionKey(session);
      const wasPinned = pinnedKeys.has(key);
      togglePin(key);
      toast.success(
        wasPinned
          ? t("sessionManager.unpinnedToast", { defaultValue: "已取消置顶" })
          : t("sessionManager.pinnedToast", { defaultValue: "已置顶该会话" }),
      );
    },
    [pinnedKeys, togglePin, t],
  );

  const handleToggleArchive = useCallback(
    (session: SessionMeta) => {
      const key = getSessionKey(session);
      const wasArchived = archivedKeys.has(key);
      toggleArchive(key);
      toast.success(
        wasArchived
          ? t("sessionManager.unarchivedToast", {
              defaultValue: "已移回会话列表",
            })
          : t("sessionManager.archivedToast", { defaultValue: "已归档该会话" }),
      );
    },
    [archivedKeys, toggleArchive, t],
  );

  const openRename = (session: SessionMeta) => {
    setRenameTarget(session);
    setRenameValue(names[getSessionKey(session)] ?? "");
  };
  const renameTooLong = renameValue.trim().length > SESSION_NAME_MAX_LENGTH;
  const originalRenameSession = renameTarget
    ? (data?.find(
        (session) => getSessionKey(session) === getSessionKey(renameTarget),
      ) ?? renameTarget)
    : null;

  const deletableFilteredSessions = useMemo(
    () => visibleSessions.filter((session) => Boolean(session.sourcePath)),
    [visibleSessions],
  );

  const selectedSessions = useMemo(
    () =>
      sessions.filter((session) =>
        selectedSessionKeys.has(getSessionKey(session)),
      ),
    [sessions, selectedSessionKeys],
  );

  const selectedDeletableSessions = useMemo(
    () => selectedSessions.filter((session) => Boolean(session.sourcePath)),
    [selectedSessions],
  );

  useEffect(() => {
    if (!selectionMode) return;

    const visibleKeys = new Set(
      deletableFilteredSessions.map((session) => getSessionKey(session)),
    );

    setSelectedSessionKeys((current) => {
      let changed = false;
      const next = new Set<string>();

      current.forEach((key) => {
        if (visibleKeys.has(key)) {
          next.add(key);
        } else {
          changed = true;
        }
      });

      return changed ? next : current;
    });
  }, [deletableFilteredSessions, selectionMode]);

  const allFilteredSelected =
    deletableFilteredSessions.length > 0 &&
    deletableFilteredSessions.every((session) =>
      selectedSessionKeys.has(getSessionKey(session)),
    );

  const getGroupSelectionState = (
    groupSessions: SessionMeta[],
  ): GroupSelectionState => {
    const selectableSessions = groupSessions.filter((session) =>
      Boolean(session.sourcePath),
    );
    const selectedCount = selectableSessions.filter((session) =>
      selectedSessionKeys.has(getSessionKey(session)),
    ).length;
    const isSelected =
      selectableSessions.length > 0 &&
      selectedCount === selectableSessions.length;

    return {
      checked:
        selectedCount === 0 ? false : isSelected ? true : "indeterminate",
      isSelected,
      selectedCount,
      selectableCount: selectableSessions.length,
    };
  };

  const toggleSessionChecked = (session: SessionMeta, checked: boolean) => {
    if (!session.sourcePath) return;
    const key = getSessionKey(session);
    setSelectedSessionKeys((current) => {
      const next = new Set(current);
      if (checked) {
        next.add(key);
      } else {
        next.delete(key);
      }
      return next;
    });
  };

  const toggleSessionGroupChecked = (
    groupSessions: SessionMeta[],
    checked: boolean,
  ) => {
    const selectableSessions = groupSessions.filter((session) =>
      Boolean(session.sourcePath),
    );
    if (selectableSessions.length === 0) return;

    setSelectedSessionKeys((current) => {
      const next = new Set(current);
      selectableSessions.forEach((session) => {
        const sessionKey = getSessionKey(session);
        if (checked) {
          next.add(sessionKey);
        } else {
          next.delete(sessionKey);
        }
      });
      return next;
    });
  };

  const toggleProviderGroup = (providerId: string) => {
    setExpandedProviderGroups((current) => {
      const next = new Set(current);
      if (next.has(providerId)) {
        next.delete(providerId);
      } else {
        next.add(providerId);
      }
      return next;
    });
  };

  const toggleDirectoryGroup = (directoryKey: string) => {
    setExpandedDirectoryGroups((current) => {
      const next = new Set(current);
      if (next.has(directoryKey)) {
        next.delete(directoryKey);
      } else {
        next.add(directoryKey);
      }
      return next;
    });
  };

  const handleCollapseAllGroups = () => {
    setExpandedProviderGroups(new Set());
    setExpandedDirectoryGroups(new Set());
  };

  const renderSessionItem = (session: SessionMeta) => {
    const sessionKey = getSessionKey(session);
    const isSelected = selectedKey !== null && sessionKey === selectedKey;

    return (
      <SessionItem
        key={sessionKey}
        session={session}
        isSelected={isSelected}
        selectionMode={selectionMode}
        searchQuery={search}
        matchSnippet={contentHits.get(sessionKey)?.snippet}
        isChecked={selectedSessionKeys.has(sessionKey)}
        isCheckDisabled={!session.sourcePath}
        isPinned={pinnedKeys.has(sessionKey)}
        isArchived={archivedKeys.has(sessionKey)}
        onSelect={setSelectedKey}
        onToggleChecked={(checked) => toggleSessionChecked(session, checked)}
        onTogglePin={() => handleTogglePin(session)}
        onToggleArchive={() => handleToggleArchive(session)}
        onRename={() => openRename(session)}
      />
    );
  };

  const renderGroupSelectionBadge = (
    selectionState: GroupSelectionState,
    totalCount: number,
    variant: "secondary" | "outline",
  ) => (
    <Badge variant={variant} className="shrink-0 text-xs">
      {selectionMode
        ? `${selectionState.selectedCount}/${selectionState.selectableCount}`
        : totalCount}
    </Badge>
  );

  const renderProviderGroupCheckbox = (
    providerGroup: SessionProviderGroup,
    providerLabel: string,
    selectionState: GroupSelectionState,
  ) => {
    if (!selectionMode) return null;

    return (
      <Checkbox
        checked={selectionState.checked}
        disabled={selectionState.selectableCount === 0}
        aria-label={t("sessionManager.selectProviderGroupForBatch", {
          defaultValue: "选择 {{provider}} 供应商分组内会话",
          provider: providerLabel,
        })}
        onClick={(event) => event.stopPropagation()}
        onCheckedChange={() =>
          toggleSessionGroupChecked(
            providerGroup.sessions,
            !selectionState.isSelected,
          )
        }
      />
    );
  };

  const renderDirectoryGroupCheckbox = (
    directoryGroup: SessionDirectoryGroup,
    selectionState: GroupSelectionState,
  ) => {
    if (!selectionMode) return null;

    return (
      <Checkbox
        checked={selectionState.checked}
        disabled={selectionState.selectableCount === 0}
        aria-label={t("sessionManager.selectDirectoryGroupForBatch", {
          defaultValue: "选择 {{directory}} 目录分组内会话",
          directory: directoryGroup.label,
        })}
        onClick={(event) => event.stopPropagation()}
        onCheckedChange={() =>
          toggleSessionGroupChecked(
            directoryGroup.sessions,
            !selectionState.isSelected,
          )
        }
      />
    );
  };

  const handleToggleSelectAll = () => {
    setSelectedSessionKeys((current) => {
      const next = new Set(current);
      if (allFilteredSelected) {
        deletableFilteredSessions.forEach((session) =>
          next.delete(getSessionKey(session)),
        );
      } else {
        deletableFilteredSessions.forEach((session) =>
          next.add(getSessionKey(session)),
        );
      }
      return next;
    });
  };

  const openBatchDeleteDialog = () => {
    if (selectedDeletableSessions.length === 0) return;
    setDeleteTargets(selectedDeletableSessions);
  };

  const exitSelectionMode = () => {
    setSelectionMode(false);
    setSelectedSessionKeys(new Set());
  };

  const sessionFilters = (
    <div className="flex items-center justify-between gap-2">
      <Select
        value={providerFilter}
        onValueChange={(value) => setProviderFilter(value as ProviderFilter)}
      >
        <SelectTrigger
          className="h-7 w-auto gap-1 px-2 text-xs"
          aria-label={t("sessionManager.providerFilterTooltip", {
            defaultValue: "供应商筛选",
          })}
        >
          <ProviderIcon
            icon={
              providerFilter === "all"
                ? "apps"
                : getProviderIconName(providerFilter)
            }
            name={providerFilter}
            size={14}
          />
          <span>
            {providerFilter === "all"
              ? t("sessionManager.mergedSearch", {
                  defaultValue: "合并检索所有 Agent",
                })
              : getProviderLabel(appId, t)}
          </span>
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={appId}>{getProviderLabel(appId, t)}</SelectItem>
          <SelectItem value="all">
            {t("sessionManager.mergedSearch", {
              defaultValue: "合并检索所有 Agent",
            })}
          </SelectItem>
        </SelectContent>
      </Select>
      <Select value={recentDays} onValueChange={setRecentDays}>
        <SelectTrigger
          aria-label={t("sessionManager.recentActivity", {
            defaultValue: "最近活动",
          })}
          className="h-7 w-auto gap-2 px-2 text-xs"
        >
          <span>
            {recentDays === "all"
              ? t("sessionManager.allTime", {
                  defaultValue: "全部时间",
                })
              : recentDays === "1"
                ? t("sessionManager.last24Hours", {
                    defaultValue: "最近 24 小时",
                  })
                : recentDays === "7"
                  ? t("sessionManager.last7Days", {
                      defaultValue: "最近 7 天",
                    })
                  : t("sessionManager.last30Days", {
                      defaultValue: "最近 30 天",
                    })}
          </span>
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">
            {t("sessionManager.allTime", {
              defaultValue: "全部时间",
            })}
          </SelectItem>
          <SelectItem value="1">
            {t("sessionManager.last24Hours", {
              defaultValue: "最近 24 小时",
            })}
          </SelectItem>
          <SelectItem value="7">
            {t("sessionManager.last7Days", {
              defaultValue: "最近 7 天",
            })}
          </SelectItem>
          <SelectItem value="30">
            {t("sessionManager.last30Days", {
              defaultValue: "最近 30 天",
            })}
          </SelectItem>
        </SelectContent>
      </Select>
    </div>
  );

  return (
    <TooltipProvider>
      <div
        className="mx-auto px-4 sm:px-6 flex flex-col h-full min-h-0"
        onWheel={(e) => e.stopPropagation()}
      >
        <div className="flex-1 overflow-hidden flex flex-col gap-4">
          {appId === "pi" &&
            piSessionDiscovery.data?.status === "requires_project_context" && (
              <div
                role="status"
                className="flex shrink-0 items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-800 dark:text-amber-200"
              >
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>
                  {t("sessionManager.piRelativeSessionDir")}{" "}
                  <code>{piSessionDiscovery.data.configuredPath}</code>
                </span>
              </div>
            )}
          {appId === "pi" &&
            (piSessionDiscovery.data?.status === "unavailable" ||
              piSessionDiscovery.isError) && (
              <div
                role="alert"
                className="flex shrink-0 items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-800 dark:text-red-200"
              >
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>
                  {t("sessionManager.piDiscoveryUnavailable", {
                    error:
                      piSessionDiscovery.data?.status === "unavailable"
                        ? piSessionDiscovery.data.reason
                        : extractErrorMessage(piSessionDiscovery.error),
                  })}
                </span>
              </div>
            )}
          {/* 主内容区域 - 左右分栏 */}
          <div className="flex-1 overflow-hidden grid gap-4 md:grid-cols-[320px_1fr]">
            {/* 左侧会话列表 */}
            <Card className="flex flex-col flex-1 min-h-0 overflow-hidden">
              <CardHeader className="py-2 px-3 border-b">
                {isSearchOpen ? (
                  <div className="flex flex-col gap-1.5">
                    <div className="flex items-center gap-2">
                      <div className="relative flex-1">
                        <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 size-3.5 text-muted-foreground" />
                        <Input
                          ref={searchInputRef}
                          value={search}
                          onChange={(event) => setSearch(event.target.value)}
                          placeholder={t("sessionManager.searchPlaceholder")}
                          className="h-8 pl-8 pr-8 text-sm"
                          autoFocus
                          onKeyDown={(e) => {
                            if (e.key === "Escape") {
                              setIsSearchOpen(false);
                              setSearch("");
                            }
                          }}
                        />
                        <Button
                          variant="ghost"
                          size="icon"
                          className="absolute right-1 top-1/2 -translate-y-1/2 size-6"
                          aria-label={t("sessionManager.closeSearch", {
                            defaultValue: "关闭搜索",
                          })}
                          onClick={() => {
                            setIsSearchOpen(false);
                            setSearch("");
                          }}
                        >
                          <X className="size-3" />
                        </Button>
                      </div>
                      <Select
                        value={searchMode}
                        onValueChange={(value) =>
                          setSearchMode(value as SessionSearchMode)
                        }
                      >
                        <SelectTrigger
                          className="h-8 w-[76px] shrink-0 px-2 text-xs"
                          aria-label={t("sessionManager.searchMode", {
                            defaultValue: "匹配方式",
                          })}
                        >
                          <span className="truncate">
                            {searchMode === "exact"
                              ? t("sessionManager.searchModeExact", {
                                  defaultValue: "精准",
                                })
                              : t("sessionManager.searchModeFuzzy", {
                                  defaultValue: "模糊",
                                })}
                          </span>
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="fuzzy">
                            {t("sessionManager.searchModeFuzzy", {
                              defaultValue: "模糊匹配",
                            })}
                          </SelectItem>
                          <SelectItem value="exact">
                            {t("sessionManager.searchModeExact", {
                              defaultValue: "精准匹配",
                            })}
                          </SelectItem>
                        </SelectContent>
                      </Select>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="size-7 shrink-0"
                        onClick={handleRefresh}
                        aria-label={t("sessionManager.refreshTooltip", {
                          defaultValue: "刷新（终端新消息会自动同步）",
                        })}
                      >
                        <RefreshCw className="size-3.5" />
                      </Button>
                      {selectionMode && (
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              variant="secondary"
                              size="icon"
                              className="size-7 bg-blue-50 text-blue-600 hover:bg-blue-100 dark:bg-blue-950/40 dark:text-blue-300 dark:hover:bg-blue-950/60"
                              aria-label={t(
                                "sessionManager.exitBatchModeTooltip",
                                {
                                  defaultValue: "退出批量管理",
                                },
                              )}
                              onClick={exitSelectionMode}
                            >
                              <CheckSquare className="size-3.5" />
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent>
                            {t("sessionManager.exitBatchModeTooltip", {
                              defaultValue: "退出批量管理",
                            })}
                          </TooltipContent>
                        </Tooltip>
                      )}
                    </div>
                    {sessionFilters}
                    <span className="text-[11px] text-muted-foreground">
                      {t("sessionManager.searchIncludingArchived", {
                        defaultValue: "搜索结果（含归档）",
                      })}{" "}
                      · {visibleSessions.length}
                    </span>
                    {search.trim() &&
                      (isSearchingContent || contentSearchError) && (
                        <div
                          role={contentSearchError ? "alert" : "status"}
                          className={cn(
                            "flex items-center gap-1.5 px-1 text-[11px]",
                            contentSearchError
                              ? "text-destructive"
                              : "text-muted-foreground",
                          )}
                        >
                          {contentSearchError ? (
                            <AlertTriangle className="size-3 shrink-0" />
                          ) : (
                            <RefreshCw className="size-3 shrink-0 animate-spin" />
                          )}
                          <span className="truncate">
                            {contentSearchError
                              ? t("sessionManager.contentSearchFailed", {
                                  defaultValue: "聊天正文搜索失败：{{error}}",
                                  error: contentSearchError,
                                })
                              : t("sessionManager.searchingContent", {
                                  defaultValue: "正在搜索完整聊天记录…",
                                })}
                          </span>
                        </div>
                      )}
                  </div>
                ) : (
                  <div className="flex flex-col gap-2">
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex items-center gap-2 min-w-0">
                        <CardTitle className="text-sm font-medium whitespace-nowrap">
                          {t("sessionManager.sessionList")}
                        </CardTitle>
                        <Badge variant="secondary" className="text-xs">
                          {visibleSessions.length}
                        </Badge>
                      </div>
                      <div className="flex items-center gap-1 shrink-0">
                        {(selectionMode ||
                          deletableFilteredSessions.length > 0) && (
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button
                                variant={selectionMode ? "secondary" : "ghost"}
                                size="icon"
                                className={
                                  selectionMode
                                    ? "size-7 bg-blue-50 text-blue-600 hover:bg-blue-100 dark:bg-blue-950/40 dark:text-blue-300 dark:hover:bg-blue-950/60"
                                    : "size-7"
                                }
                                aria-label={
                                  selectionMode
                                    ? t("sessionManager.exitBatchModeTooltip", {
                                        defaultValue: "退出批量管理",
                                      })
                                    : t("sessionManager.manageBatchTooltip", {
                                        defaultValue: "批量管理",
                                      })
                                }
                                onClick={() => {
                                  if (selectionMode) {
                                    exitSelectionMode();
                                  } else {
                                    setSelectionMode(true);
                                  }
                                }}
                              >
                                <CheckSquare className="size-3.5" />
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent>
                              {selectionMode
                                ? t("sessionManager.exitBatchModeTooltip", {
                                    defaultValue: "退出批量管理",
                                  })
                                : t("sessionManager.manageBatchTooltip", {
                                    defaultValue: "批量管理",
                                  })}
                            </TooltipContent>
                          </Tooltip>
                        )}
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="size-7"
                              aria-label={t("sessionManager.searchSessions", {
                                defaultValue: "搜索会话",
                              })}
                              onClick={() => {
                                setIsSearchOpen(true);
                                setTimeout(
                                  () => searchInputRef.current?.focus(),
                                  0,
                                );
                              }}
                            >
                              <Search className="size-3.5" />
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent>
                            {t("sessionManager.searchSessions")}
                          </TooltipContent>
                        </Tooltip>
                        <Select
                          value={listViewMode}
                          onValueChange={(value) =>
                            setListViewMode(value as SessionListViewMode)
                          }
                        >
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <SelectTrigger
                                className="size-7 p-0 justify-center border-0 bg-transparent hover:bg-muted"
                                aria-label={t(
                                  "sessionManager.viewModeTooltip",
                                  {
                                    defaultValue: "查看方式",
                                  },
                                )}
                              >
                                <span className="sr-only">
                                  {t("sessionManager.viewModeTooltip", {
                                    defaultValue: "查看方式",
                                  })}
                                </span>
                                {listViewMode === "grouped" ? (
                                  <ListTree className="size-3.5" />
                                ) : (
                                  <List className="size-3.5" />
                                )}
                              </SelectTrigger>
                            </TooltipTrigger>
                            <TooltipContent>{listViewModeLabel}</TooltipContent>
                          </Tooltip>
                          <SelectContent className="w-40">
                            <SelectItem value="flat">
                              <div className="flex items-center gap-2">
                                <List className="size-3.5" />
                                <span>
                                  {t("sessionManager.viewModeFlat", {
                                    defaultValue: "列表",
                                  })}
                                </span>
                              </div>
                            </SelectItem>
                            <SelectItem value="grouped">
                              <div className="flex items-center gap-2">
                                <ListTree className="size-3.5" />
                                <span>
                                  {t("sessionManager.viewModeGrouped", {
                                    defaultValue: "分类",
                                  })}
                                </span>
                              </div>
                            </SelectItem>
                          </SelectContent>
                        </Select>
                        {listViewMode === "grouped" && (
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button
                                variant="ghost"
                                size="icon"
                                className="size-7"
                                aria-label={t(
                                  "sessionManager.collapseAllGroups",
                                  {
                                    defaultValue: "全部收起",
                                  },
                                )}
                                onClick={handleCollapseAllGroups}
                              >
                                <ChevronsDownUp className="size-3.5" />
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent>
                              {t("sessionManager.collapseAllGroups", {
                                defaultValue: "全部收起",
                              })}
                            </TooltipContent>
                          </Tooltip>
                        )}

                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="size-7"
                              onClick={handleRefresh}
                              aria-label={t("sessionManager.refreshTooltip", {
                                defaultValue: "刷新（终端新消息会自动同步）",
                              })}
                            >
                              <RefreshCw className="size-3.5" />
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent>
                            {t("sessionManager.refreshTooltip", {
                              defaultValue: "刷新（终端新消息会自动同步）",
                            })}
                          </TooltipContent>
                        </Tooltip>
                      </div>
                    </div>
                    {sessionFilters}
                    {/* 会话 / 归档 切换 */}
                    <div className="flex items-center rounded-lg bg-muted/60 p-0.5">
                      <button
                        type="button"
                        onClick={() => setListTab("sessions")}
                        className={cn(
                          "flex-1 flex items-center justify-center gap-1.5 rounded-md px-2 py-1 text-xs transition-colors",
                          listTab === "sessions"
                            ? "bg-background text-foreground font-medium shadow-sm"
                            : "text-muted-foreground hover:text-foreground",
                        )}
                      >
                        <MessageSquare className="size-3" />
                        {search.trim()
                          ? t("sessionManager.searchIncludingArchived", {
                              defaultValue: "搜索结果（含归档）",
                            })
                          : t("sessionManager.tabSessions", {
                              defaultValue: "会话",
                            })}
                        <span className="text-[10px] text-muted-foreground">
                          {activeCount}
                        </span>
                      </button>
                      <button
                        type="button"
                        onClick={() => setListTab("archived")}
                        className={cn(
                          "flex-1 flex items-center justify-center gap-1.5 rounded-md px-2 py-1 text-xs transition-colors",
                          listTab === "archived"
                            ? "bg-background text-foreground font-medium shadow-sm"
                            : "text-muted-foreground hover:text-foreground",
                        )}
                      >
                        <Archive className="size-3" />
                        {t("sessionManager.tabArchived", {
                          defaultValue: "已归档",
                        })}
                        <span className="text-[10px] text-muted-foreground">
                          {archivedCount}
                        </span>
                      </button>
                    </div>
                    {selectionMode && (
                      <div className="grid gap-3 rounded-md border bg-muted/40 px-3 py-2.5">
                        <div className="flex items-center gap-2 text-xs text-muted-foreground">
                          <Badge variant="outline" className="text-xs">
                            {t("sessionManager.selectedCount", {
                              defaultValue: "已选 {{count}} 项",
                              count: selectedDeletableSessions.length,
                            })}
                          </Badge>
                          <span className="truncate">
                            {t("sessionManager.batchModeHint", {
                              defaultValue: "勾选要删除的会话",
                            })}
                          </span>
                        </div>
                        <div className="grid gap-3 min-[520px]:grid-cols-[minmax(0,1fr)_auto] min-[520px]:items-center">
                          <div className="flex flex-wrap items-center gap-2">
                            {deletableFilteredSessions.length > 0 && (
                              <Button
                                variant="ghost"
                                size="sm"
                                className="h-7 px-2.5 text-xs whitespace-nowrap"
                                onClick={handleToggleSelectAll}
                              >
                                {allFilteredSelected
                                  ? t("sessionManager.clearFilteredSelection", {
                                      defaultValue: "取消全选",
                                    })
                                  : t("sessionManager.selectAllFiltered", {
                                      defaultValue: "全选当前",
                                    })}
                              </Button>
                            )}
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-7 px-2.5 text-xs whitespace-nowrap"
                              onClick={() => setSelectedSessionKeys(new Set())}
                            >
                              {t("sessionManager.clearSelection", {
                                defaultValue: "清空已选",
                              })}
                            </Button>
                          </div>
                          <Button
                            variant="destructive"
                            size="sm"
                            className="h-7 gap-1.5 px-2.5 whitespace-nowrap justify-self-start min-[520px]:justify-self-end"
                            onClick={openBatchDeleteDialog}
                            disabled={
                              isDeleting ||
                              selectedDeletableSessions.length === 0
                            }
                          >
                            <Trash2 className="size-3.5" />
                            <span className="text-xs">
                              {isBatchDeleting
                                ? t("sessionManager.batchDeleting", {
                                    defaultValue: "删除中...",
                                  })
                                : t("sessionManager.deleteSelected", {
                                    defaultValue: "批量删除",
                                  })}
                            </span>
                          </Button>
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </CardHeader>
              <CardContent className="flex-1 min-h-0 p-0">
                <div
                  ref={listScrollRef}
                  className="h-full overflow-y-auto"
                  role="region"
                  tabIndex={0}
                  aria-label="会话列表"
                >
                  <div className="p-2">
                    {isLoading ? (
                      <div className="flex items-center justify-center py-12">
                        <RefreshCw className="size-5 animate-spin text-muted-foreground" />
                      </div>
                    ) : visibleSessions.length === 0 ? (
                      <div className="flex flex-col items-center justify-center py-12 text-center">
                        {listTab === "archived" ? (
                          <Archive className="size-8 text-muted-foreground/50 mb-2" />
                        ) : (
                          <MessageSquare className="size-8 text-muted-foreground/50 mb-2" />
                        )}
                        <p className="text-sm text-muted-foreground">
                          {listTab === "archived"
                            ? t("sessionManager.noArchivedSessions", {
                                defaultValue: "暂无归档会话",
                              })
                            : t("sessionManager.noSessions")}
                        </p>
                        {listTab === "archived" && (
                          <p className="text-xs text-muted-foreground/70 mt-1 px-4">
                            {t("sessionManager.archiveHint", {
                              defaultValue:
                                "鼠标悬停会话，点击归档按钮即可收纳到这里",
                            })}
                          </p>
                        )}
                      </div>
                    ) : listViewMode === "grouped" ? (
                      <div className="space-y-2">
                        {groupedSessions.map((providerGroup) => {
                          const providerOpen = expandedProviderGroups.has(
                            providerGroup.providerId,
                          );
                          const providerLabel = getProviderLabel(
                            providerGroup.providerId,
                            t,
                          );
                          const providerSelectionState = getGroupSelectionState(
                            providerGroup.sessions,
                          );

                          return (
                            <Collapsible
                              key={providerGroup.providerId}
                              open={providerOpen}
                              onOpenChange={() =>
                                toggleProviderGroup(providerGroup.providerId)
                              }
                            >
                              <div className="flex w-full items-center gap-2 rounded-md border bg-muted/40 px-2.5 py-2 transition-colors hover:bg-muted">
                                {renderProviderGroupCheckbox(
                                  providerGroup,
                                  providerLabel,
                                  providerSelectionState,
                                )}
                                <CollapsibleTrigger asChild>
                                  <button
                                    type="button"
                                    className="flex min-w-0 flex-1 items-center gap-2 text-left"
                                    aria-label={t(
                                      "sessionManager.toggleProviderGroup",
                                      {
                                        defaultValue:
                                          "展开或折叠 {{provider}} 供应商分组",
                                        provider: providerLabel,
                                      },
                                    )}
                                  >
                                    {providerOpen ? (
                                      <ChevronDown className="size-3.5 shrink-0 text-muted-foreground" />
                                    ) : (
                                      <ChevronRight className="size-3.5 shrink-0 text-muted-foreground" />
                                    )}
                                    <ProviderIcon
                                      icon={getProviderIconName(
                                        providerGroup.providerId,
                                      )}
                                      name={providerGroup.providerId}
                                      size={16}
                                    />
                                    <span className="min-w-0 flex-1 truncate text-sm font-medium">
                                      {providerLabel}
                                    </span>
                                    {renderGroupSelectionBadge(
                                      providerSelectionState,
                                      providerGroup.sessions.length,
                                      "secondary",
                                    )}
                                  </button>
                                </CollapsibleTrigger>
                              </div>
                              <CollapsibleContent className="mt-1 space-y-1 pl-2">
                                {providerGroup.directories.map(
                                  (directoryGroup) => {
                                    const directoryOpen =
                                      expandedDirectoryGroups.has(
                                        directoryGroup.key,
                                      );
                                    const directorySelectionState =
                                      getGroupSelectionState(
                                        directoryGroup.sessions,
                                      );

                                    return (
                                      <Collapsible
                                        key={directoryGroup.key}
                                        open={directoryOpen}
                                        onOpenChange={() =>
                                          toggleDirectoryGroup(
                                            directoryGroup.key,
                                          )
                                        }
                                      >
                                        <div className="flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground">
                                          {renderDirectoryGroupCheckbox(
                                            directoryGroup,
                                            directorySelectionState,
                                          )}
                                          <CollapsibleTrigger asChild>
                                            <button
                                              type="button"
                                              className="flex min-w-0 flex-1 items-center gap-2 text-left"
                                              aria-label={t(
                                                "sessionManager.toggleDirectoryGroup",
                                                {
                                                  defaultValue:
                                                    "展开或折叠 {{directory}} 目录分组",
                                                  directory:
                                                    directoryGroup.label,
                                                },
                                              )}
                                            >
                                              {directoryOpen ? (
                                                <ChevronDown className="size-3.5 shrink-0" />
                                              ) : (
                                                <ChevronRight className="size-3.5 shrink-0" />
                                              )}
                                              <FolderOpen className="size-3.5 shrink-0" />
                                              <Tooltip>
                                                <TooltipTrigger asChild>
                                                  <span className="min-w-0 flex-1 truncate text-xs font-medium">
                                                    {directoryGroup.label}
                                                  </span>
                                                </TooltipTrigger>
                                                <TooltipContent
                                                  side="bottom"
                                                  className="max-w-xs"
                                                >
                                                  <p className="font-mono text-xs break-all">
                                                    {directoryGroup.projectDir ??
                                                      t(
                                                        "sessionManager.unknownDirectory",
                                                        {
                                                          defaultValue:
                                                            "未知目录",
                                                        },
                                                      )}
                                                  </p>
                                                </TooltipContent>
                                              </Tooltip>
                                              {renderGroupSelectionBadge(
                                                directorySelectionState,
                                                directoryGroup.sessions.length,
                                                "outline",
                                              )}
                                            </button>
                                          </CollapsibleTrigger>
                                        </div>
                                        <CollapsibleContent className="mt-1 space-y-1 pl-3">
                                          {directoryGroup.sessions.map(
                                            (session) =>
                                              renderSessionItem(session),
                                          )}
                                        </CollapsibleContent>
                                      </Collapsible>
                                    );
                                  },
                                )}
                              </CollapsibleContent>
                            </Collapsible>
                          );
                        })}
                      </div>
                    ) : (
                      <div
                        className={virtualizeList ? "relative" : "space-y-1"}
                        style={
                          virtualizeList
                            ? { height: listVirtualizer.getTotalSize() }
                            : undefined
                        }
                      >
                        {(virtualizeList
                          ? listVirtualizer.getVirtualItems()
                          : visibleSessions.map((_, index) => ({
                              index,
                              key: getSessionKey(visibleSessions[index]),
                              start: 0,
                            }))
                        ).map((row) => {
                          const { index } = row;
                          const session = visibleSessions[index];
                          const sessionKey = getSessionKey(session);

                          return (
                            <div
                              key={sessionKey}
                              data-index={index}
                              ref={
                                virtualizeList
                                  ? listVirtualizer.measureElement
                                  : undefined
                              }
                              style={
                                virtualizeList
                                  ? {
                                      position: "absolute",
                                      top: 0,
                                      left: 0,
                                      width: "100%",
                                      transform: `translateY(${row.start}px)`,
                                    }
                                  : undefined
                              }
                            >
                              {visiblePinnedCount > 0 && index === 0 && (
                                <div className="flex items-center gap-1 px-2 pb-1 text-[11px] text-muted-foreground">
                                  <Pin className="size-3" />
                                  {t("sessionManager.pinnedSection", {
                                    defaultValue: "已置顶",
                                  })}
                                </div>
                              )}
                              {visiblePinnedCount > 0 &&
                                visiblePinnedCount < visibleSessions.length &&
                                index === visiblePinnedCount && (
                                  <div className="mt-2 mb-1 border-t border-border/60" />
                                )}
                              {renderSessionItem(session)}
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                </div>
              </CardContent>
            </Card>

            {/* 右侧会话详情 */}
            <Card
              className="flex flex-col overflow-hidden min-h-0"
              ref={detailRef}
            >
              {!selectedSession ? (
                <div className="flex-1 flex flex-col items-center justify-center text-muted-foreground p-8">
                  <MessageSquare className="size-12 mb-3 opacity-30" />
                  <p className="text-sm">{t("sessionManager.selectSession")}</p>
                </div>
              ) : (
                <>
                  {/* 详情头部 */}
                  <CardHeader className="py-3 px-4 border-b shrink-0">
                    <div className="flex items-start justify-between gap-4">
                      {/* 左侧：会话信息 */}
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2 mb-1">
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <span className="shrink-0">
                                <ProviderIcon
                                  icon={getProviderIconName(
                                    selectedSession.providerId,
                                  )}
                                  name={selectedSession.providerId}
                                  size={20}
                                />
                              </span>
                            </TooltipTrigger>
                            <TooltipContent>
                              {getProviderLabel(selectedSession.providerId, t)}
                            </TooltipContent>
                          </Tooltip>
                          <h2 className="text-base font-semibold truncate">
                            {formatSessionTitle(selectedSession)}
                          </h2>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="size-7 shrink-0"
                            aria-label={t("sessionManager.renameSelected", {
                              defaultValue: "重命名当前会话",
                            })}
                            onClick={() => openRename(selectedSession)}
                          >
                            <Pencil className="size-3.5" />
                          </Button>
                        </div>

                        {/* 元信息 */}
                        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
                          <div className="flex items-center gap-1">
                            <Clock className="size-3" />
                            <span>
                              {formatTimestamp(
                                selectedSession.lastActiveAt ??
                                  selectedSession.createdAt,
                              )}
                            </span>
                          </div>
                          {selectedSession.projectDir && (
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <button
                                  type="button"
                                  onClick={() =>
                                    void handleCopy(
                                      selectedSession.projectDir!,
                                      t("sessionManager.projectDirCopied"),
                                    )
                                  }
                                  className="flex items-center gap-1 hover:text-foreground transition-colors"
                                >
                                  <FolderOpen className="size-3" />
                                  <span className="truncate max-w-[200px]">
                                    {getBaseName(selectedSession.projectDir)}
                                  </span>
                                </button>
                              </TooltipTrigger>
                              <TooltipContent
                                side="bottom"
                                className="max-w-xs"
                              >
                                <p className="font-mono text-xs break-all">
                                  {selectedSession.projectDir}
                                </p>
                                <p className="text-muted-foreground mt-1">
                                  {t("sessionManager.clickToCopyPath")}
                                </p>
                              </TooltipContent>
                            </Tooltip>
                          )}
                          {selectedSession.sourcePath && (
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <button
                                  type="button"
                                  onClick={() =>
                                    void handleCopy(
                                      selectedSession.sourcePath!,
                                      t("sessionManager.sourcePathCopied"),
                                    )
                                  }
                                  className="flex items-center gap-1 hover:text-foreground transition-colors"
                                >
                                  <FileText className="size-3 shrink-0" />
                                  <span className="font-mono truncate max-w-[200px]">
                                    {getBaseName(selectedSession.sourcePath)}
                                  </span>
                                </button>
                              </TooltipTrigger>
                              <TooltipContent
                                side="bottom"
                                className="max-w-xs"
                              >
                                <p className="font-mono text-xs break-all">
                                  {selectedSession.sourcePath}
                                </p>
                                <p className="text-muted-foreground mt-1">
                                  {t("sessionManager.clickToCopyPath")}
                                </p>
                              </TooltipContent>
                            </Tooltip>
                          )}
                        </div>
                      </div>

                      {/* 右侧：操作按钮组 */}
                      <div className="flex items-center gap-2 shrink-0">
                        {isMac() && (
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button
                                size="sm"
                                className="gap-1.5"
                                onClick={() => void handleResume()}
                                disabled={!selectedSession.resumeCommand}
                              >
                                <Play className="size-3.5" />
                                <span className="hidden sm:inline">
                                  {t("sessionManager.resume", {
                                    defaultValue: "恢复会话",
                                  })}
                                </span>
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent>
                              {selectedSession.resumeCommand
                                ? t("sessionManager.resumeTooltip", {
                                    defaultValue: "在终端中恢复此会话",
                                  })
                                : t("sessionManager.noResumeCommand", {
                                    defaultValue: "此会话无法恢复",
                                  })}
                            </TooltipContent>
                          </Tooltip>
                        )}
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              size="sm"
                              variant="outline"
                              className="gap-1.5"
                              onClick={handleExportMarkdown}
                              disabled={messages.length === 0}
                            >
                              <Download className="size-3.5" />
                              <span className="hidden sm:inline">
                                {t("sessionManager.exportMarkdown", {
                                  defaultValue: "导出 .md",
                                })}
                              </span>
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent>
                            {t("sessionManager.exportMarkdownTooltip", {
                              defaultValue: "导出当前会话为 Markdown 文件",
                            })}
                          </TooltipContent>
                        </Tooltip>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              size="sm"
                              variant="destructive"
                              className="gap-1.5"
                              onClick={() =>
                                setDeleteTargets([selectedSession])
                              }
                              disabled={
                                !selectedSession.sourcePath || isDeleting
                              }
                            >
                              <Trash2 className="size-3.5" />
                              <span className="hidden sm:inline">
                                {isDeleting
                                  ? t("sessionManager.deleting", {
                                      defaultValue: "删除中...",
                                    })
                                  : t("sessionManager.delete", {
                                      defaultValue: "删除会话",
                                    })}
                              </span>
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent>
                            {t("sessionManager.deleteTooltip", {
                              defaultValue: "永久删除此本地会话记录",
                            })}
                          </TooltipContent>
                        </Tooltip>
                      </div>
                    </div>

                    {isCodexSession && selectedSession.resumeCommand && (
                      <p className="mt-2 text-xs text-muted-foreground">
                        {t("sessionManager.codexFullHistoryHint", {
                          defaultValue:
                            "恢复时加载全部已保存历史；长会话首次打开可能稍慢。",
                        })}
                      </p>
                    )}

                    {/* 恢复命令预览 */}
                    {selectedSession.resumeCommand && (
                      <div className="mt-3 flex items-center gap-2">
                        <div className="flex-1 rounded-md bg-muted/60 px-3 py-1.5 font-mono text-xs text-muted-foreground truncate">
                          {selectedSession.resumeCommand}
                        </div>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="size-7 shrink-0"
                              onClick={() => void handleCopyResume()}
                              aria-label={t("sessionManager.copyCommand", {
                                defaultValue: "复制命令",
                              })}
                            >
                              <Copy className="size-3.5" />
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent>
                            {t("sessionManager.copyCommand", {
                              defaultValue: "复制命令",
                            })}
                          </TooltipContent>
                        </Tooltip>
                      </div>
                    )}
                  </CardHeader>

                  {/* 消息列表区域 */}
                  <CardContent className="flex-1 min-h-0 p-0">
                    <div className="flex h-full min-w-0">
                      {/* 消息列表 */}
                      <div className="flex-1 min-w-0 flex flex-col">
                        <div className="px-4 pt-4 pb-2 min-w-0">
                          {messagesError && (
                            <div
                              role="alert"
                              className="mb-2 text-sm text-destructive"
                            >
                              {t("sessionManager.messagesReadFailed", {
                                defaultValue:
                                  "读取会话记录失败：{{error}}。请刷新后重试。",
                                error: extractErrorMessage(messagesError),
                              })}
                            </div>
                          )}
                          <div className="flex items-center gap-2">
                            <MessageSquare className="size-4 text-muted-foreground" />
                            <span className="text-sm font-medium">
                              {t("sessionManager.conversationHistory", {
                                defaultValue: "对话记录",
                              })}
                            </span>
                            <Badge variant="secondary" className="text-xs">
                              {messages.length}
                            </Badge>
                          </div>
                        </div>
                        <div
                          ref={scrollContainerRef}
                          className="flex-1 overflow-y-auto px-4 pb-4 min-w-0"
                        >
                          {isLoadingMessages ? (
                            <div className="flex items-center justify-center py-12">
                              <RefreshCw className="size-5 animate-spin text-muted-foreground" />
                            </div>
                          ) : messages.length === 0 ? (
                            <div className="flex flex-col items-center justify-center py-12 text-center">
                              <MessageSquare className="size-8 text-muted-foreground/50 mb-2" />
                              <p className="text-sm text-muted-foreground">
                                {t("sessionManager.emptySession")}
                              </p>
                            </div>
                          ) : (
                            <div
                              style={{
                                height: virtualizer.getTotalSize(),
                                position: "relative",
                              }}
                            >
                              {virtualizer
                                .getVirtualItems()
                                .map((virtualRow) => (
                                  <div
                                    key={`${selectedKey}:${virtualRow.key}`}
                                    data-index={virtualRow.index}
                                    ref={virtualizer.measureElement}
                                    style={{
                                      position: "absolute",
                                      top: 0,
                                      left: 0,
                                      width: "100%",
                                      transform: `translateY(${virtualRow.start}px)`,
                                    }}
                                  >
                                    <SessionMessageItem
                                      message={messages[virtualRow.index]}
                                      isActive={
                                        activeMessageIndex === virtualRow.index
                                      }
                                      searchQuery={search}
                                      onCopy={handleMessageCopy}
                                    />
                                  </div>
                                ))}
                            </div>
                          )}
                        </div>
                      </div>

                      {/* 右侧目录 - 类似少数派 (大屏幕) */}
                      <SessionTocSidebar
                        items={userMessagesToc}
                        onItemClick={scrollToMessage}
                      />
                    </div>

                    {/* 浮动目录按钮 (小屏幕) */}
                    <SessionTocDialog
                      items={userMessagesToc}
                      onItemClick={scrollToMessage}
                      open={tocDialogOpen}
                      onOpenChange={setTocDialogOpen}
                    />
                  </CardContent>
                </>
              )}
            </Card>
          </div>
        </div>
      </div>
      <Dialog
        open={Boolean(renameTarget)}
        onOpenChange={(open) => {
          if (!open) setRenameTarget(null);
        }}
      >
        <DialogContent>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              if (
                renameTarget &&
                rename(getSessionKey(renameTarget), renameValue)
              )
                setRenameTarget(null);
            }}
          >
            <DialogHeader>
              <DialogTitle>
                {t("sessionManager.rename", { defaultValue: "重命名会话" })}
              </DialogTitle>
              <DialogDescription>
                {t("sessionManager.renameHint", {
                  defaultValue: "仅修改本地显示名称，留空恢复原名。",
                })}
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-2 px-6 py-5">
              <label
                htmlFor="session-custom-name"
                className="text-sm font-medium"
              >
                {t("sessionManager.customName", { defaultValue: "会话名称" })}
              </label>
              <Input
                id="session-custom-name"
                value={renameValue}
                placeholder={
                  originalRenameSession
                    ? formatSessionTitle(originalRenameSession)
                    : ""
                }
                onChange={(event) => setRenameValue(event.target.value)}
                aria-invalid={renameTooLong}
                aria-describedby={
                  renameTooLong ? "session-name-error" : "session-name-limit"
                }
              />
              <p
                id="session-name-limit"
                className="text-xs text-muted-foreground"
              >
                {t("sessionManager.nameLimit", {
                  defaultValue: "最多 {{count}} 个字符",
                  count: SESSION_NAME_MAX_LENGTH,
                })}
              </p>
              {renameTooLong && (
                <p
                  id="session-name-error"
                  role="alert"
                  className="text-sm text-destructive"
                >
                  {t("sessionManager.nameTooLong", {
                    defaultValue: "名称不能超过 {{count}} 个字符",
                    count: SESSION_NAME_MAX_LENGTH,
                  })}
                </p>
              )}
            </div>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => setRenameTarget(null)}
              >
                {t("common.cancel", { defaultValue: "取消" })}
              </Button>
              <Button type="submit" disabled={renameTooLong}>
                {t("common.save", { defaultValue: "保存" })}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      <ConfirmDialog
        isOpen={Boolean(deleteTargets)}
        title={
          deleteTargets && deleteTargets.length > 1
            ? t("sessionManager.batchDeleteConfirmTitle", {
                defaultValue: "批量删除会话",
              })
            : t("sessionManager.deleteConfirmTitle", {
                defaultValue: "删除会话",
              })
        }
        message={
          deleteTargets && deleteTargets.length > 1
            ? t("sessionManager.batchDeleteConfirmMessage", {
                defaultValue:
                  "将永久删除已选中的 {{count}} 个本地会话记录。\n\n此操作不可恢复。",
                count: deleteTargets.length,
              })
            : deleteTargets?.[0]
              ? t("sessionManager.deleteConfirmMessage", {
                  defaultValue:
                    "将永久删除本地会话“{{title}}”\nSession ID: {{sessionId}}\n\n此操作不可恢复。",
                  title: formatSessionTitle(deleteTargets[0]),
                  sessionId: deleteTargets[0].sessionId,
                })
              : ""
        }
        confirmText={
          deleteTargets && deleteTargets.length > 1
            ? t("sessionManager.batchDeleteConfirmAction", {
                defaultValue: "删除所选会话",
              })
            : t("sessionManager.deleteConfirmAction", {
                defaultValue: "删除会话",
              })
        }
        cancelText={t("common.cancel", { defaultValue: "取消" })}
        variant="destructive"
        onConfirm={() => void handleDeleteConfirm()}
        onCancel={() => {
          if (!isDeleting) {
            setDeleteTargets(null);
          }
        }}
      />
    </TooltipProvider>
  );
}
