import { useCallback, useEffect, useRef, useState } from "react";
import { ExternalLink, Loader2, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { settingsApi, type ToolVersion } from "@/lib/api/settings";
import { compareVersions } from "@/lib/version";
import { extractErrorMessage } from "@/utils/errorUtils";

const TOOLS = [
  { name: "claude", label: "Claude Code" },
  { name: "codex", label: "Codex" },
  { name: "gemini", label: "Gemini CLI" },
  { name: "grok", label: "Grok Build" },
  { name: "opencode", label: "OpenCode" },
  { name: "openclaw", label: "OpenClaw" },
  { name: "hermes", label: "Hermes" },
  { name: "pi", label: "Pi" },
  { name: "claude-desktop", label: "Claude Desktop" },
] as const;
type ToolName = (typeof TOOLS)[number]["name"];
type Owner = { active: boolean };
type Job = { owner: Owner; name: ToolName; run: () => Promise<void> };

// Keep the limit across unmount/remount too: old IPC cannot be cancelled, but
// its queued siblings can. A later mount performs fresh checks, never adopts
// the old component's delayed results.
const pendingJobs: Job[] = [];
const activeTools = new Set<ToolName>();
let pumpScheduled = false;
function schedulePump() {
  if (pumpScheduled) return;
  pumpScheduled = true;
  void Promise.resolve().then(() => {
    pumpScheduled = false;
    while (activeTools.size < 2) {
      const index = pendingJobs.findIndex(
        (job) => job.owner.active && !activeTools.has(job.name),
      );
      if (index < 0) break;
      const [job] = pendingJobs.splice(index, 1);
      activeTools.add(job.name);
      void job.run().finally(() => {
        activeTools.delete(job.name);
        schedulePump();
      });
    }
  });
}

type RowState = {
  phase: "queued" | "checking" | "done";
  result?: ToolVersion;
  failure?: string;
  checkedAt?: number;
};
type Status =
  | "unchecked"
  | "checking"
  | "latest"
  | "update"
  | "ahead"
  | "missing"
  | "local_error"
  | "remote_error"
  | "blocked"
  | "blocked_latest"
  | "blocked_update"
  | "blocked_ahead"
  | "unsupported";

// compareVersions deliberately returns zero for unknown input. Validate the
// whole version first so malformed output can never acquire an "up to date" badge.
function comparableVersion(value: string | null | undefined): string | null {
  if (!value) return null;
  const version = value.trim().replace(/^v/, "");
  const match = version.match(
    /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/,
  );
  if (
    !match ||
    match.slice(1, 4).some((part) => !Number.isSafeInteger(Number(part)))
  )
    return null;
  if (match[4]?.split(".").some((part) => /^0\d+$/.test(part))) return null;
  return version;
}

function rowStatus(row: RowState | undefined): Status {
  if (!row) return "unchecked";
  if (row.phase !== "done") return "checking";
  if (row.failure || !row.result) return "local_error";
  const tool = row.result;
  if (tool.status === "blocked") {
    const current = comparableVersion(tool.version);
    const latest = comparableVersion(tool.latest_version);
    if (!current || !latest || tool.latest_error) return "blocked";
    // Protected launchers may still expose trustworthy installation metadata.
    // Comparing that metadata does not imply the PATH entry can be executed.
    const comparison = compareVersions(current, latest);
    return comparison === 0
      ? "blocked_latest"
      : comparison < 0
        ? "blocked_update"
        : "blocked_ahead";
  }
  if (tool.status === "unsupported") return "unsupported";
  if (tool.status === "not_installed") return "missing";
  if (tool.installed_but_broken || tool.local_error) return "local_error";
  if (!tool.version) return tool.status === "error" ? "local_error" : "missing";
  if (!comparableVersion(tool.version)) return "local_error";
  if (tool.latest_error || !comparableVersion(tool.latest_version))
    return "remote_error";
  if (tool.error || tool.status === "error") return "local_error";
  const comparison = compareVersions(
    comparableVersion(tool.version)!,
    comparableVersion(tool.latest_version)!,
  );
  return comparison === 0 ? "latest" : comparison < 0 ? "update" : "ahead";
}

function safeSourceUrl(value: string | null | undefined): string | undefined {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password
      ? url.href
      : undefined;
  } catch {
    return undefined;
  }
}

export function ToolVersionsSection() {
  const { t } = useTranslation();
  const [rows, setRows] = useState<Partial<Record<ToolName, RowState>>>({});
  const ownerRef = useRef<Owner | null>(null);
  const requested = useRef(new Set<ToolName>());

  const check = useCallback((names: readonly ToolName[]) => {
    const owner = ownerRef.current;
    if (!owner?.active) return;
    const targets = names.filter((name) => !requested.current.has(name));
    if (targets.length === 0) return;
    targets.forEach((name) => requested.current.add(name));
    setRows((current) => ({
      ...current,
      ...Object.fromEntries(targets.map((name) => [name, { phase: "queued" }])),
    }));
    for (const name of targets) {
      pendingJobs.push({
        owner,
        name,
        run: async () => {
          if (!owner.active) return;
          setRows((current) => ({ ...current, [name]: { phase: "checking" } }));
          let next: RowState;
          try {
            const results = await settingsApi.getToolVersions([name]);
            const result = results.find((tool) => tool.name === name);
            if (!result) throw new Error("检查未返回该工具的版本信息");
            next = {
              phase: "done",
              result,
              checkedAt:
                typeof result.checked_at === "number" &&
                result.checked_at >= 0 &&
                Number.isFinite(new Date(result.checked_at).getTime())
                  ? result.checked_at
                  : Date.now(),
            };
          } catch (error) {
            next = {
              phase: "done",
              failure: extractErrorMessage(error) || "版本检查请求失败",
              checkedAt: Date.now(),
            };
          }
          if (!owner.active || ownerRef.current !== owner) return;
          requested.current.delete(name);
          setRows((current) => ({ ...current, [name]: next }));
        },
      });
    }
    schedulePump();
  }, []);

  useEffect(() => {
    const owner = { active: true };
    ownerRef.current = owner;
    check(TOOLS.map((tool) => tool.name));
    return () => {
      owner.active = false;
      for (let index = pendingJobs.length - 1; index >= 0; index--) {
        if (pendingJobs[index].owner === owner) pendingJobs.splice(index, 1);
      }
      requested.current.clear();
    };
  }, [check]);

  const completed = TOOLS.filter(
    (tool) => rows[tool.name]?.phase === "done",
  ).length;
  const busy = TOOLS.some(
    (tool) => rows[tool.name] && rows[tool.name]!.phase !== "done",
  );
  const labels: Record<Status, string> = {
    unchecked: t("settings.toolVersions.unchecked", { defaultValue: "未检查" }),
    checking: t("settings.toolVersions.checking", { defaultValue: "检查中" }),
    latest: t("settings.toolVersions.latest", { defaultValue: "已最新" }),
    update: t("settings.toolVersions.updateAvailable", {
      defaultValue: "可更新",
    }),
    ahead: t("settings.toolVersions.ahead", { defaultValue: "本地版本领先" }),
    missing: t("settings.toolVersions.missing", { defaultValue: "未安装" }),
    local_error: t("settings.toolVersions.localError", {
      defaultValue: "本地检查失败",
    }),
    remote_error: t("settings.toolVersions.remoteError", {
      defaultValue: "远端检查失败",
    }),
    blocked: t("settings.toolVersions.blocked", { defaultValue: "检查受阻" }),
    blocked_latest: t("settings.toolVersions.blockedLatest", {
      defaultValue: "安装版本已最新 · 运行入口受限",
    }),
    blocked_update: t("settings.toolVersions.blockedUpdate", {
      defaultValue: "安装版本可更新 · 运行入口受限",
    }),
    blocked_ahead: t("settings.toolVersions.blockedAhead", {
      defaultValue: "安装版本领先 · 运行入口受限",
    }),
    unsupported: t("settings.toolVersions.unsupported", {
      defaultValue: "当前平台不支持",
    }),
  };

  const renderRow = (name: ToolName, label: string, alias = false) => {
    const row = rows[name];
    const status = rowStatus(row);
    const tool = row?.result;
    const sourceUrl = safeSourceUrl(tool?.source_url);
    const errors = [
      row?.failure,
      tool?.local_error,
      tool?.error,
      tool?.latest_error,
    ];
    if (
      status === "local_error" &&
      !row?.failure &&
      !tool?.error &&
      !tool?.local_error
    )
      errors.push("无法识别本地版本号");
    if (status === "remote_error" && !tool?.latest_error)
      errors.push(
        tool?.latest_version ? "无法识别远端版本号" : "未获取到最新版本",
      );
    const errorDetails = [
      ...new Set(errors.filter((error): error is string => Boolean(error))),
    ].join("\n");
    return (
      <div
        key={alias ? "bincode" : name}
        role="row"
        aria-label={label}
        className="grid gap-x-4 gap-y-2 border-t border-border px-4 py-3 sm:grid-cols-[minmax(9rem,1.1fr)_minmax(6rem,1fr)_minmax(6rem,1fr)_minmax(9rem,1.2fr)]"
      >
        <div role="rowheader" className="min-w-0">
          <div className="text-sm font-medium">{label}</div>
          {alias && (
            <p className="mt-1 text-xs text-muted-foreground">
              {t("settings.toolVersions.bincodeChannel", {
                defaultValue: "复用 OpenCode 通道；非独立 BinCode 版本",
              })}
            </p>
          )}
          {tool?.env_type === "wsl" && (
            <p className="mt-1 break-words text-xs text-muted-foreground">
              WSL{tool.wsl_distro ? ` · ${tool.wsl_distro}` : ""}
            </p>
          )}
        </div>
        <div role="cell" className="min-w-0 text-sm">
          <span className="mr-2 text-xs text-muted-foreground sm:hidden">
            {t("settings.toolVersions.currentVersion", {
              defaultValue: "当前版本",
            })}
          </span>
          <span className="break-all font-mono">{tool?.version || "—"}</span>
        </div>
        <div role="cell" className="min-w-0 text-sm">
          <span className="mr-2 text-xs text-muted-foreground sm:hidden">
            {t("settings.toolVersions.latestVersion", {
              defaultValue: "最新版本",
            })}
          </span>
          <span className="break-all font-mono">
            {tool?.latest_version || "—"}
          </span>
          {sourceUrl && (
            <a
              href={sourceUrl}
              onClick={(event) => {
                event.preventDefault();
                void settingsApi
                  .openExternal(sourceUrl)
                  .catch((error) =>
                    toast.error(
                      extractErrorMessage(error) || "无法打开版本来源",
                    ),
                  );
              }}
              className="mt-1 flex w-fit items-center gap-1 text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground"
            >
              {t("settings.toolVersions.source", { defaultValue: "版本来源" })}
              <ExternalLink className="size-3" />
            </a>
          )}
        </div>
        <div role="cell" className="space-y-1">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span
              className={
                status.endsWith("error")
                  ? "text-sm text-destructive"
                  : status === "update"
                    ? "text-sm text-primary"
                    : "text-sm text-muted-foreground"
              }
            >
              {labels[status]}
            </span>
            {!alias && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-7 px-2 text-xs"
                disabled={status === "checking"}
                aria-label={`${label} ${t("settings.toolVersions.retry", { defaultValue: "重新检查" })}`}
                onClick={() => check([name])}
              >
                {status === "checking" ? (
                  <Loader2 className="size-3 animate-spin" />
                ) : (
                  <RefreshCw className="size-3" />
                )}
                {t("settings.toolVersions.retry", { defaultValue: "重新检查" })}
              </Button>
            )}
          </div>
          {row?.checkedAt !== undefined && (
            <time
              dateTime={new Date(row.checkedAt).toISOString()}
              className="block text-xs text-muted-foreground"
            >
              {new Date(row.checkedAt).toLocaleString()}
            </time>
          )}
        </div>
        {errorDetails && (
          <details className="min-w-0 text-xs sm:col-span-4">
            <summary className="w-fit cursor-pointer text-muted-foreground">
              {t("settings.toolVersions.errorDetails", {
                defaultValue: "查看错误详情",
              })}
            </summary>
            <p className="mt-2 whitespace-pre-wrap break-words text-destructive [overflow-wrap:anywhere]">
              {errorDetails}
            </p>
          </details>
        )}
      </div>
    );
  };

  return (
    <section
      className="space-y-4"
      aria-label={t("settings.toolVersions.title", {
        defaultValue: "工具版本",
      })}
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">
            {t("settings.toolVersions.title", { defaultValue: "工具版本" })}
          </h2>
          <p
            role="status"
            aria-live="polite"
            className="mt-1 text-sm text-muted-foreground"
          >
            {busy
              ? t("settings.toolVersions.progress", {
                  defaultValue: "检查中 {{completed}} / {{total}}",
                  completed,
                  total: TOOLS.length,
                })
              : t("settings.toolVersions.completed", {
                  defaultValue: "已检查 {{completed}} / {{total}}",
                  completed,
                  total: TOOLS.length,
                })}
          </p>
        </div>
        <Button
          type="button"
          onClick={() => {
            if (requested.current.size === 0)
              check(TOOLS.map((tool) => tool.name));
          }}
          disabled={busy}
        >
          {busy ? (
            <Loader2 className="size-4 animate-spin" />
          ) : (
            <RefreshCw className="size-4" />
          )}
          {t("settings.toolVersions.checkAll", { defaultValue: "检查全部" })}
        </Button>
      </div>
      <div
        role="table"
        aria-label={t("settings.toolVersions.title", {
          defaultValue: "工具版本",
        })}
        className="overflow-hidden rounded-lg border border-border bg-card"
      >
        <div
          role="row"
          className="grid grid-cols-[minmax(9rem,1.1fr)_minmax(6rem,1fr)_minmax(6rem,1fr)_minmax(9rem,1.2fr)] gap-4 px-4 py-2 text-xs text-muted-foreground max-sm:sr-only"
        >
          {["工具", "当前版本", "最新版本", "状态"].map((heading) => (
            <span key={heading} role="columnheader">
              {heading}
            </span>
          ))}
        </div>
        {TOOLS.map((tool) => renderRow(tool.name, tool.label))}
        {renderRow("opencode", "BinCode · 复用 OpenCode 通道", true)}
      </div>
    </section>
  );
}
