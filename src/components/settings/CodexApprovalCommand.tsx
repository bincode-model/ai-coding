import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Copy, Terminal } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { copyText } from "@/lib/clipboard";
import { isWindows } from "@/lib/platform";
import {
  CODEX_NO_APPROVAL_UNIX_COMMAND,
  CODEX_NO_APPROVAL_WINDOWS_COMMAND,
} from "@/lib/codexApprovalCommands";

export function CodexApprovalCommand() {
  const { t } = useTranslation();
  const [copying, setCopying] = useState(false);
  const windows = isWindows();
  const command = windows
    ? CODEX_NO_APPROVAL_WINDOWS_COMMAND
    : CODEX_NO_APPROVAL_UNIX_COMMAND;

  const handleCopy = async () => {
    if (copying) return;
    setCopying(true);
    try {
      await copyText(command);
      toast.success(t("settings.codexApproval.copySuccess"));
    } catch (error) {
      console.error("Failed to copy Codex approval command:", error);
      toast.error(t("settings.codexApproval.copyFailed"));
    } finally {
      setCopying(false);
    }
  };

  return (
    <div className="rounded-xl border border-border/50 bg-muted/20 p-4 space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <Terminal className="h-4 w-4 text-primary" aria-hidden="true" />
            <h4 className="text-sm font-medium">
              {t("settings.codexApproval.title")}
            </h4>
          </div>
          <p className="text-xs text-muted-foreground">
            {t("settings.codexApproval.description")}
          </p>
          <p className="text-xs text-muted-foreground">
            {windows ? "Windows PowerShell" : "macOS / Linux"}
          </p>
        </div>
        <Button
          type="button"
          size="sm"
          onClick={() => void handleCopy()}
          disabled={copying}
        >
          <Copy className="mr-1.5 h-3.5 w-3.5" aria-hidden="true" />
          {t("settings.codexApproval.copy")}
        </Button>
      </div>

      <pre
        data-testid="codex-approval-command"
        className="max-h-40 overflow-auto rounded-lg border border-border/40 bg-background/60 p-3 text-[11px] leading-relaxed select-text"
      >
        <code>{command}</code>
      </pre>
      <p className="text-xs text-muted-foreground">
        {t("settings.codexApproval.note")}
      </p>
    </div>
  );
}
