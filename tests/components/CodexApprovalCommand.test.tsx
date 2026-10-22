import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { CodexApprovalCommand } from "@/components/settings/CodexApprovalCommand";
import { copyText } from "@/lib/clipboard";
import { isWindows } from "@/lib/platform";
import {
  CODEX_NO_APPROVAL_UNIX_COMMAND,
  CODEX_NO_APPROVAL_WINDOWS_COMMAND,
} from "@/lib/codexApprovalCommands";

vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn() }));
vi.mock("@/lib/platform", () => ({ isWindows: vi.fn(() => false) }));

beforeEach(() => vi.mocked(isWindows).mockReturnValue(false));
afterEach(() => vi.restoreAllMocks());

describe("CodexApprovalCommand", () => {
  it("copies the one visible macOS terminal command", async () => {
    const user = userEvent.setup();
    vi.mocked(copyText).mockResolvedValue();
    const success = vi.spyOn(toast, "success").mockImplementation(() => "");
    render(<CodexApprovalCommand />);

    expect(screen.getByTestId("codex-approval-command").textContent).toBe(
      CODEX_NO_APPROVAL_UNIX_COMMAND,
    );
    await user.click(
      screen.getByRole("button", { name: "settings.codexApproval.copy" }),
    );
    expect(copyText).toHaveBeenCalledWith(CODEX_NO_APPROVAL_UNIX_COMMAND);
    await waitFor(() => expect(success).toHaveBeenCalled());

    expect(screen.getAllByRole("button")).toHaveLength(1);
  });

  it("shows the PowerShell command on Windows with the same copy button", async () => {
    vi.mocked(isWindows).mockReturnValue(true);
    vi.mocked(copyText).mockResolvedValue();
    const user = userEvent.setup();
    render(<CodexApprovalCommand />);
    expect(screen.getByTestId("codex-approval-command").textContent).toBe(
      CODEX_NO_APPROVAL_WINDOWS_COMMAND,
    );
    await user.click(
      screen.getByRole("button", { name: "settings.codexApproval.copy" }),
    );
    expect(copyText).toHaveBeenCalledWith(CODEX_NO_APPROVAL_WINDOWS_COMMAND);
  });

  it("reports a clipboard failure without changing Codex configuration", async () => {
    const user = userEvent.setup();
    vi.mocked(copyText).mockRejectedValue(new Error("clipboard unavailable"));
    vi.spyOn(console, "error").mockImplementation(() => {});
    const failure = vi.spyOn(toast, "error").mockImplementation(() => "");
    render(<CodexApprovalCommand />);

    await user.click(
      screen.getByRole("button", { name: "settings.codexApproval.copy" }),
    );
    await waitFor(() =>
      expect(failure).toHaveBeenCalledWith("settings.codexApproval.copyFailed"),
    );
  });
});
