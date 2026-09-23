import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { SessionMessageItem } from "@/components/sessions/SessionMessageItem";

describe("complete conversation display", () => {
  it("shows the end of long AI replies by default and only folds on request", async () => {
    const content = "完整上下文".repeat(1000) + "真正的末尾答复";
    render(
      <TooltipProvider>
        <SessionMessageItem
          message={{ role: "assistant", content }}
          isActive={false}
          onCopy={vi.fn()}
        />
      </TooltipProvider>,
    );
    expect(screen.getByText(content)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "收起" }));
    expect(screen.queryByText(content)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /展开完整内容/ }));
    expect(screen.getByText(content)).toBeInTheDocument();
  });
});
