import {
  Archive,
  ArchiveRestore,
  ChevronRight,
  Clock,
  Pin,
  PinOff,
  Pencil,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { ProviderIcon } from "@/components/ProviderIcon";
import type { SessionMeta } from "@/types";
import {
  formatRelativeTime,
  formatSessionTitle,
  getProviderIconName,
  getProviderLabel,
  getSessionKey,
  highlightText,
} from "./utils";

interface SessionItemProps {
  session: SessionMeta;
  isSelected: boolean;
  selectionMode: boolean;
  isChecked: boolean;
  isCheckDisabled?: boolean;
  searchQuery?: string;
  /** 正文检索命中的上下文片段（仅搜索时存在） */
  matchSnippet?: string;
  isPinned: boolean;
  isArchived: boolean;
  onSelect: (key: string) => void;
  onToggleChecked: (checked: boolean) => void;
  onTogglePin: () => void;
  onToggleArchive: () => void;
  onRename: () => void;
}

export function SessionItem({
  session,
  isSelected,
  selectionMode,
  isChecked,
  isCheckDisabled = false,
  searchQuery,
  matchSnippet,
  isPinned,
  isArchived,
  onSelect,
  onToggleChecked,
  onTogglePin,
  onToggleArchive,
  onRename,
}: SessionItemProps) {
  const { t } = useTranslation();
  const title = formatSessionTitle(session);
  const lastActive = session.lastActiveAt || session.createdAt || undefined;
  const sessionKey = getSessionKey(session);

  return (
    <div
      className={cn(
        "relative flex items-start gap-2 rounded-lg px-3 py-2.5 transition-all group",
        isSelected
          ? "bg-primary/10 border border-primary/30"
          : "hover:bg-muted/60 border border-transparent",
      )}
    >
      {selectionMode && (
        <div className="shrink-0 pt-0.5">
          <Checkbox
            checked={isChecked}
            disabled={isCheckDisabled}
            aria-label={t("sessionManager.selectForBatch", {
              defaultValue: "选择会话",
            })}
            onCheckedChange={(checked) => onToggleChecked(Boolean(checked))}
          />
        </div>
      )}
      <button
        type="button"
        onClick={() => onSelect(sessionKey)}
        className="min-w-0 flex-1 text-left"
      >
        <div className="flex items-center gap-2 mb-1">
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="shrink-0">
                <ProviderIcon
                  icon={getProviderIconName(session.providerId)}
                  name={session.providerId}
                  size={18}
                />
              </span>
            </TooltipTrigger>
            <TooltipContent>
              {getProviderLabel(session.providerId, t)}
            </TooltipContent>
          </Tooltip>
          <span className="text-sm font-medium line-clamp-2 flex-1">
            {searchQuery ? highlightText(title, searchQuery) : title}
          </span>
          {isArchived && (
            <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
              {t("sessionManager.tabArchived", { defaultValue: "已归档" })}
            </span>
          )}
          {isPinned && (
            <Pin className="size-3.5 text-primary fill-current shrink-0" />
          )}
          <ChevronRight
            className={cn(
              "size-4 text-muted-foreground/50 shrink-0 transition-transform",
              isSelected && "text-primary rotate-90",
            )}
          />
        </div>

        <div className="flex items-center gap-1 text-[11px] text-muted-foreground">
          <Clock className="size-3" />
          <span>
            {lastActive
              ? formatRelativeTime(lastActive, t)
              : t("common.unknown")}
          </span>
        </div>
        {matchSnippet && searchQuery && (
          <div
            className="mt-1 text-[11px] leading-snug text-muted-foreground/90 line-clamp-2 break-all"
            title={t("sessionManager.contentMatch", {
              defaultValue: "聊天内容命中",
            })}
          >
            {highlightText(matchSnippet, searchQuery)}
          </div>
        )}
      </button>

      {!selectionMode && (
        <div className="absolute right-2 top-2 flex opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 items-center gap-0.5 rounded-md border bg-background/95 px-0.5 py-0.5 shadow-sm">
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                className="flex size-6 items-center justify-center rounded hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
                aria-label={
                  isPinned
                    ? t("sessionManager.unpin", { defaultValue: "取消置顶" })
                    : t("sessionManager.pin", { defaultValue: "置顶" })
                }
                onClick={(event) => {
                  event.stopPropagation();
                  onTogglePin();
                }}
              >
                {isPinned ? (
                  <PinOff className="size-3.5" />
                ) : (
                  <Pin className="size-3.5" />
                )}
              </button>
            </TooltipTrigger>
            <TooltipContent>
              {isPinned
                ? t("sessionManager.unpin", { defaultValue: "取消置顶" })
                : t("sessionManager.pin", { defaultValue: "置顶" })}
            </TooltipContent>
          </Tooltip>
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                className="flex size-6 items-center justify-center rounded hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
                aria-label={t("sessionManager.rename", {
                  defaultValue: "重命名会话",
                })}
                onClick={(event) => {
                  event.stopPropagation();
                  onRename();
                }}
              >
                <Pencil className="size-3.5" />
              </button>
            </TooltipTrigger>
            <TooltipContent>
              {t("sessionManager.rename", { defaultValue: "重命名会话" })}
            </TooltipContent>
          </Tooltip>
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                className="flex size-6 items-center justify-center rounded hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
                aria-label={
                  isArchived
                    ? t("sessionManager.unarchive", {
                        defaultValue: "取消归档",
                      })
                    : t("sessionManager.archive", { defaultValue: "归档" })
                }
                onClick={(event) => {
                  event.stopPropagation();
                  onToggleArchive();
                }}
              >
                {isArchived ? (
                  <ArchiveRestore className="size-3.5" />
                ) : (
                  <Archive className="size-3.5" />
                )}
              </button>
            </TooltipTrigger>
            <TooltipContent>
              {isArchived
                ? t("sessionManager.unarchive", { defaultValue: "取消归档" })
                : t("sessionManager.archive", { defaultValue: "归档" })}
            </TooltipContent>
          </Tooltip>
        </div>
      )}
    </div>
  );
}
