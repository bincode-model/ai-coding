import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SessionManagerPage } from "@/components/sessions/SessionManagerPage";
import { piApi } from "@/lib/api/pi";
import { sessionsApi } from "@/lib/api/sessions";
import * as platform from "@/lib/platform";
import type { SessionMessage, SessionMeta } from "@/types";
import { setSessionFixtures } from "../msw/state";

const toastSuccessMock = vi.fn();
const toastErrorMock = vi.fn();
const GROUP_EXPANSION_STORAGE_KEY =
  "ai-coding.sessionManager.groupExpansionState";
const SEARCH_MODE_STORAGE_KEY = "ai-coding.sessionManager.searchMode";
const ORGANIZER_STORAGE_KEY = "ai-coding-session-organizer";

vi.mock("sonner", () => ({
  toast: {
    success: (...args: unknown[]) => toastSuccessMock(...args),
    error: (...args: unknown[]) => toastErrorMock(...args),
  },
}));

vi.mock("@/components/sessions/SessionToc", () => ({
  SessionTocSidebar: () => null,
  SessionTocDialog: () => null,
}));

vi.mock("@/components/ConfirmDialog", () => ({
  ConfirmDialog: ({
    isOpen,
    title,
    message,
    confirmText,
    cancelText,
    onConfirm,
    onCancel,
  }: {
    isOpen: boolean;
    title: string;
    message: string;
    confirmText: string;
    cancelText: string;
    onConfirm: () => void;
    onCancel: () => void;
  }) =>
    isOpen ? (
      <div data-testid="confirm-dialog">
        <div>{title}</div>
        <div>{message}</div>
        <button onClick={onConfirm}>{confirmText}</button>
        <button onClick={onCancel}>{cancelText}</button>
      </div>
    ) : null,
}));

const renderPage = (appId = "codex") => {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <SessionManagerPage appId={appId} />
      </QueryClientProvider>,
    ),
  };
};

const openSearch = async () => {
  await userEvent.click(screen.getByRole("button", { name: /搜索会话/i }));
};

const closeSearch = async () => {
  await userEvent.click(screen.getByRole("button", { name: /关闭搜索/i }));
};

const openViewModeMenu = async () => {
  await userEvent.click(screen.getByRole("combobox", { name: /查看方式/i }));
};

const switchToGroupedView = async () => {
  await openViewModeMenu();
  const groupedOption = await screen.findByRole("option", { name: /分类/i });
  await userEvent.click(groupedOption);
  await waitFor(() =>
    expect(
      screen.queryByRole("option", { name: /分类/i }),
    ).not.toBeInTheDocument(),
  );
};

const switchProviderFilter = async (providerLabel: RegExp) => {
  const providerFilterTrigger = screen.getByRole("combobox", {
    name: /供应商筛选/i,
  });

  await userEvent.click(providerFilterTrigger);
  await userEvent.click(
    await screen.findByRole("option", { name: providerLabel }),
  );
};

const enterGroupedBatchMode = async () => {
  await switchToGroupedView();
  fireEvent.click(screen.getByRole("button", { name: /批量管理/i }));
};

const collapseAllGroups = () => {
  fireEvent.click(screen.getByRole("button", { name: /全部收起/i }));
};

const expandDirectoryGroup = (provider: string, directory: string) => {
  fireEvent.click(
    screen.getByRole("button", {
      name: new RegExp(`展开或折叠 ${provider} 供应商分组`),
    }),
  );
  fireEvent.click(
    screen.getByRole("button", {
      name: new RegExp(`展开或折叠 ${directory} 目录分组`),
    }),
  );
};

describe("SessionManagerPage", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  beforeEach(() => {
    toastSuccessMock.mockReset();
    toastErrorMock.mockReset();
    Element.prototype.scrollIntoView = vi.fn();
    window.localStorage.removeItem("ai-coding.sessionManager.listViewMode");
    window.localStorage.removeItem(GROUP_EXPANSION_STORAGE_KEY);
    window.localStorage.removeItem(SEARCH_MODE_STORAGE_KEY);
    window.localStorage.removeItem(ORGANIZER_STORAGE_KEY);

    const sessions: SessionMeta[] = [
      {
        providerId: "codex",
        sessionId: "codex-session-1",
        title: "Alpha Session",
        summary: "Alpha summary",
        projectDir: "/mock/codex",
        createdAt: 2,
        lastActiveAt: 20,
        sourcePath: "/mock/codex/session-1.jsonl",
        resumeCommand: "codex resume codex-session-1",
      },
      {
        providerId: "codex",
        sessionId: "codex-session-2",
        title: "Beta Session",
        summary: "Beta summary",
        projectDir: "/mock/codex",
        createdAt: 1,
        lastActiveAt: 10,
        sourcePath: "/mock/codex/session-2.jsonl",
        resumeCommand: "codex resume codex-session-2",
      },
      {
        providerId: "claude",
        sessionId: "claude-session-1",
        title: "Claude Session",
        summary: "Claude summary",
        projectDir: "/mock/claude",
        createdAt: 3,
        lastActiveAt: 30,
        sourcePath: "/mock/claude/session-1.jsonl",
        resumeCommand: "claude --resume claude-session-1",
      },
      {
        providerId: "codex",
        sessionId: "codex-session-3",
        title: "Gamma Session",
        summary: "Gamma summary",
        projectDir: null,
        createdAt: 0,
        lastActiveAt: 5,
        sourcePath: "/mock/codex/session-3.jsonl",
        resumeCommand: "codex resume codex-session-3",
      },
    ];
    const messages: Record<string, SessionMessage[]> = {
      "codex:/mock/codex/session-1.jsonl": [
        { role: "user", content: "alpha", ts: 20 },
      ],
      "codex:/mock/codex/session-2.jsonl": [
        { role: "user", content: "beta", ts: 10 },
      ],
      "codex:/mock/codex/session-3.jsonl": [
        { role: "user", content: "gamma", ts: 5 },
      ],
      "claude:/mock/claude/session-1.jsonl": [
        { role: "user", content: "claude", ts: 30 },
      ],
    };

    setSessionFixtures(sessions, messages);
  });

  it.each([
    {
      providerId: "codex",
      title: "Alpha Session",
      sessionId: "codex-session-1",
      sourcePath: "/mock/codex/session-1.jsonl",
      command: "codex resume --verified codex-session-1",
      cwd: "/prepared/codex project",
    },
    {
      providerId: "claude",
      title: "Claude Session",
      sessionId: "claude-session-1",
      sourcePath: "/mock/claude/session-1.jsonl",
      command: "claude --resume verified-claude-session-1",
      cwd: "/prepared/claude project",
    },
  ])(
    "prepares the exact $providerId source before launching the returned command and directory on Mac",
    async ({ providerId, title, sessionId, sourcePath, command, cwd }) => {
      vi.spyOn(platform, "isMac").mockReturnValue(true);
      const user = userEvent.setup();
      const copySpy = vi
        .spyOn(navigator.clipboard, "writeText")
        .mockResolvedValue();
      let resolvePreparation!: (value: {
        command: string;
        cwd: string;
      }) => void;
      const prepareSpy = vi.spyOn(sessionsApi, "prepareResume").mockReturnValue(
        new Promise((resolve) => {
          resolvePreparation = resolve;
        }),
      );
      const launchSpy = vi
        .spyOn(sessionsApi, "launchTerminal")
        .mockResolvedValue(true);
      renderPage(providerId);

      await user.click(
        await screen.findByRole("button", { name: new RegExp(title) }),
      );
      await user.click(screen.getByRole("button", { name: "恢复会话" }));

      expect(prepareSpy.mock.calls).toEqual([
        [
          {
            providerId,
            sessionId,
            sourcePath,
          },
        ],
      ]);
      expect(launchSpy).not.toHaveBeenCalled();
      expect(copySpy).not.toHaveBeenCalled();
      expect(toastSuccessMock).not.toHaveBeenCalled();

      await act(async () => resolvePreparation({ command, cwd }));

      await waitFor(() =>
        expect(launchSpy.mock.calls).toEqual([[{ command, cwd }]]),
      );
      expect(toastSuccessMock.mock.calls).toEqual([
        ["sessionManager.terminalLaunched"],
      ]);
      expect(copySpy).not.toHaveBeenCalled();
      expect(toastErrorMock).not.toHaveBeenCalled();
    },
  );

  it.each([
    ["codex", "Alpha Session", "会话源文件已不存在"],
    ["claude", "Claude Session", "原始文件中的会话 ID 与请求不符"],
  ])(
    "does not launch or copy a stale %s command when source preparation fails",
    async (providerId, title, reason) => {
      vi.spyOn(platform, "isMac").mockReturnValue(true);
      const user = userEvent.setup();
      const copySpy = vi
        .spyOn(navigator.clipboard, "writeText")
        .mockResolvedValue();
      const prepareSpy = vi
        .spyOn(sessionsApi, "prepareResume")
        .mockRejectedValue(new Error(reason));
      const launchSpy = vi
        .spyOn(sessionsApi, "launchTerminal")
        .mockResolvedValue(true);
      renderPage(providerId);

      await user.click(
        await screen.findByRole("button", { name: new RegExp(title) }),
      );
      await user.click(screen.getByRole("button", { name: "恢复会话" }));

      await waitFor(() =>
        expect(toastErrorMock.mock.calls).toEqual([[reason]]),
      );
      expect(prepareSpy).toHaveBeenCalledTimes(1);
      expect(launchSpy).not.toHaveBeenCalled();
      expect(copySpy).not.toHaveBeenCalled();
      expect(toastSuccessMock).not.toHaveBeenCalled();
    },
  );

  it("rejects a missing source without using the displayed stale resume command", async () => {
    vi.spyOn(platform, "isMac").mockReturnValue(true);
    const user = userEvent.setup();
    const copySpy = vi
      .spyOn(navigator.clipboard, "writeText")
      .mockResolvedValue();
    const prepareSpy = vi.spyOn(sessionsApi, "prepareResume");
    const launchSpy = vi
      .spyOn(sessionsApi, "launchTerminal")
      .mockResolvedValue(true);
    setSessionFixtures(
      [
        {
          providerId: "codex",
          sessionId: "missing-source",
          title: "Missing Source Session",
          resumeCommand: "codex resume stale-missing-source",
        },
      ],
      {},
    );
    renderPage();

    await user.click(
      await screen.findByRole("button", { name: /Missing Source Session/ }),
    );
    await user.click(screen.getByRole("button", { name: "恢复会话" }));

    await waitFor(() =>
      expect(toastErrorMock.mock.calls).toEqual([
        ["缺少原始会话文件，无法恢复"],
      ]),
    );
    expect(prepareSpy).not.toHaveBeenCalled();
    expect(launchSpy).not.toHaveBeenCalled();
    expect(copySpy).not.toHaveBeenCalled();
    expect(toastSuccessMock).not.toHaveBeenCalled();
  });

  it("copies only the prepared command when Mac terminal launch fails and reports the launch error", async () => {
    vi.spyOn(platform, "isMac").mockReturnValue(true);
    const user = userEvent.setup();
    const copySpy = vi
      .spyOn(navigator.clipboard, "writeText")
      .mockResolvedValue();
    const prepared = {
      command: "codex resume verified-fallback-session",
      cwd: "/prepared/fallback project",
    };
    vi.spyOn(sessionsApi, "prepareResume").mockResolvedValue(prepared);
    const launchSpy = vi
      .spyOn(sessionsApi, "launchTerminal")
      .mockRejectedValue(new Error("Terminal denied the launch request"));
    renderPage();

    await user.click(
      await screen.findByRole("button", { name: /Alpha Session/ }),
    );
    await user.click(screen.getByRole("button", { name: "恢复会话" }));

    await waitFor(() =>
      expect(toastErrorMock.mock.calls).toEqual([
        ["Terminal denied the launch request"],
      ]),
    );
    expect(launchSpy.mock.calls).toEqual([[prepared]]);
    expect(copySpy.mock.calls).toEqual([[prepared.command]]);
    expect(toastSuccessMock.mock.calls).toEqual([
      ["sessionManager.resumeFallbackCopied"],
    ]);
    expect(toastSuccessMock).not.toHaveBeenCalledWith(
      "sessionManager.terminalLaunched",
    );
  });

  it.each([
    [true, "codex", "Alpha Session"],
    [false, "claude", "Claude Session"],
  ])(
    "prepares the command before copying it without launching a terminal (Mac: %s, Agent: %s)",
    async (mac, providerId, title) => {
      vi.spyOn(platform, "isMac").mockReturnValue(mac);
      const user = userEvent.setup();
      const copySpy = vi
        .spyOn(navigator.clipboard, "writeText")
        .mockResolvedValue();
      const prepareSpy = vi
        .spyOn(sessionsApi, "prepareResume")
        .mockResolvedValue({
          command: `${providerId} --resume verified-copy-session`,
          cwd: "/prepared/copy project",
        });
      const launchSpy = vi
        .spyOn(sessionsApi, "launchTerminal")
        .mockResolvedValue(true);
      renderPage(providerId);

      await user.click(
        await screen.findByRole("button", { name: new RegExp(title) }),
      );
      await user.click(screen.getByRole("button", { name: "复制命令" }));

      await waitFor(() =>
        expect(copySpy.mock.calls).toEqual([
          [`${providerId} --resume verified-copy-session`],
        ]),
      );
      expect(prepareSpy.mock.calls).toEqual([
        [
          {
            providerId,
            sessionId: `${providerId}-session-1`,
            sourcePath: `/mock/${providerId}/session-1.jsonl`,
          },
        ],
      ]);
      expect(launchSpy).not.toHaveBeenCalled();
      expect(toastSuccessMock.mock.calls).toEqual([
        ["sessionManager.resumeCommandCopied"],
      ]);
      expect(toastErrorMock).not.toHaveBeenCalled();
    },
  );

  it.each([
    [true, "claude", "Claude Session"],
    [false, "codex", "Alpha Session"],
  ])(
    "does not copy the displayed command when preparation rejects its source (Mac: %s, Agent: %s)",
    async (mac, providerId, title) => {
      vi.spyOn(platform, "isMac").mockReturnValue(mac);
      const user = userEvent.setup();
      const copySpy = vi
        .spyOn(navigator.clipboard, "writeText")
        .mockResolvedValue();
      const prepareSpy = vi
        .spyOn(sessionsApi, "prepareResume")
        .mockRejectedValue(new Error("会话来源与所选 Agent 不匹配"));
      const launchSpy = vi
        .spyOn(sessionsApi, "launchTerminal")
        .mockResolvedValue(true);
      renderPage(providerId);

      await user.click(
        await screen.findByRole("button", { name: new RegExp(title) }),
      );
      await user.click(screen.getByRole("button", { name: "复制命令" }));

      await waitFor(() =>
        expect(toastErrorMock.mock.calls).toEqual([
          ["会话来源与所选 Agent 不匹配"],
        ]),
      );
      expect(prepareSpy).toHaveBeenCalledTimes(1);
      expect(copySpy).not.toHaveBeenCalled();
      expect(launchSpy).not.toHaveBeenCalled();
      expect(toastSuccessMock).not.toHaveBeenCalled();
    },
  );

  it("keeps pinned archives in the main list across remounts and unpinning preserves archive", async () => {
    const key = "codex:codex-session-1:/mock/codex/session-1.jsonl";
    const view = renderPage();
    const row = (name: RegExp) =>
      within(screen.getByRole("button", { name }).parentElement!);
    await screen.findByRole("button", { name: /Alpha Session/ });
    await userEvent.click(
      row(/Alpha Session/).getByRole("button", { name: "置顶" }),
    );
    await userEvent.click(
      row(/Alpha Session/).getByRole("button", { name: "归档" }),
    );
    expect(
      screen.getByRole("button", { name: /Alpha Session/ }),
    ).toHaveTextContent("已归档");
    expect(screen.getByText("已置顶")).toBeVisible();
    expect(
      JSON.parse(localStorage.getItem(ORGANIZER_STORAGE_KEY)!),
    ).toMatchObject({ pinned: [key], archived: [key] });
    view.unmount();
    renderPage();
    await screen.findByRole("button", { name: /Alpha Session/ });
    await userEvent.click(
      row(/Alpha Session/).getByRole("button", { name: "取消置顶" }),
    );
    expect(
      screen.queryByRole("button", { name: /Alpha Session/ }),
    ).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /^已归档/ }));
    await screen.findByRole("button", { name: /Alpha Session/ });
    await userEvent.click(
      row(/Alpha Session/).getByRole("button", { name: "置顶" }),
    );
    expect(
      row(/Alpha Session/).getByRole("button", { name: "取消置顶" }),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /^会话\s/ }));
    expect(
      await screen.findByRole("button", { name: /Alpha Session/ }),
    ).toHaveTextContent("已归档");
    expect(
      JSON.parse(localStorage.getItem(ORGANIZER_STORAGE_KEY)!),
    ).toMatchObject({ pinned: [key], archived: [key] });
  });

  it("persists a row rename through archive, search, grouped view and remount, then clears it from the header", async () => {
    const key = "codex:codex-session-1:/mock/codex/session-1.jsonl";
    vi.spyOn(sessionsApi, "searchContents").mockResolvedValue([]);
    const view = renderPage();
    const originalRow = await screen.findByRole("button", {
      name: /Alpha Session/,
    });
    await userEvent.click(
      within(originalRow.parentElement!).getByRole("button", {
        name: "重命名会话",
      }),
    );
    const dialog = screen.getByRole("dialog", { name: "重命名会话" });
    await userEvent.type(
      within(dialog).getByRole("textbox", { name: "会话名称" }),
      "  本地项目复盘  ",
    );
    await userEvent.click(within(dialog).getByRole("button", { name: "保存" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "本地项目复盘" })).toBeVisible();
    const renamedRow = screen.getByRole("button", { name: /本地项目复盘/ });
    await userEvent.click(
      within(renamedRow.parentElement!).getByRole("button", { name: "归档" }),
    );
    expect(
      screen.queryByRole("button", { name: /本地项目复盘/ }),
    ).not.toBeInTheDocument();
    await openSearch();
    await userEvent.type(screen.getByRole("textbox"), "本地项目复盘");
    expect(
      await screen.findByRole("button", { name: /本地项目复盘/ }),
    ).toHaveTextContent("已归档");
    expect(
      view.client
        .getQueryData<SessionMeta[]>(["sessions"])
        ?.find((session) => session.sessionId === "codex-session-1")?.title,
    ).toBe("Alpha Session");
    view.unmount();

    renderPage();
    await screen.findByRole("button", { name: /Beta Session/ });
    await userEvent.click(screen.getByRole("button", { name: /^已归档/ }));
    expect(
      await screen.findByRole("button", { name: /本地项目复盘/ }),
    ).toBeInTheDocument();
    await switchToGroupedView();
    expandDirectoryGroup("codex", "codex");
    expect(
      await screen.findByRole("button", { name: /本地项目复盘/ }),
    ).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: "重命名当前会话" }),
    );
    const resetDialog = screen.getByRole("dialog", { name: "重命名会话" });
    const input = within(resetDialog).getByRole("textbox", {
      name: "会话名称",
    });
    expect(input).toHaveValue("本地项目复盘");
    await userEvent.clear(input);
    await userEvent.click(
      within(resetDialog).getByRole("button", { name: "保存" }),
    );
    expect(
      screen.getByRole("heading", { name: "Alpha Session" }),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: /Alpha Session/ }),
    ).toHaveTextContent("已归档");
    expect(
      JSON.parse(localStorage.getItem(ORGANIZER_STORAGE_KEY)!).names[key],
    ).toBeUndefined();
  });

  it("validates rename length without losing the saved name and cancels without changes", async () => {
    const view = renderPage();
    await screen.findByRole("button", { name: /Alpha Session/ });
    await userEvent.click(
      screen.getByRole("button", { name: "重命名当前会话" }),
    );
    const dialog = screen.getByRole("dialog", { name: "重命名会话" });
    const input = within(dialog).getByRole("textbox", { name: "会话名称" });
    fireEvent.change(input, { target: { value: "名".repeat(121) } });
    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      "名称不能超过 120 个字符",
    );
    expect(within(dialog).getByRole("button", { name: "保存" })).toBeDisabled();
    fireEvent.submit(input.closest("form")!);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(localStorage.getItem(ORGANIZER_STORAGE_KEY)).toBeNull();
    fireEvent.change(input, {
      target: { value: "  " + "名".repeat(120) + "  " },
    });
    expect(within(dialog).getByRole("button", { name: "保存" })).toBeEnabled();
    await userEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    expect(
      screen.getByRole("heading", { name: "Alpha Session" }),
    ).toBeVisible();
    expect(
      view.client.getQueryData<SessionMeta[]>(["sessions"])?.[0].title,
    ).toBe("Alpha Session");
  });

  it("preserves organizer state when a scan temporarily omits a session and removes it only after deletion", async () => {
    const key = "codex:codex-session-1:/mock/codex/session-1.jsonl";
    localStorage.setItem(
      ORGANIZER_STORAGE_KEY,
      JSON.stringify({
        pinned: [key],
        archived: [key],
        names: { [key]: "持续保留名称" },
      }),
    );
    const view = renderPage();
    await screen.findByRole("button", { name: /持续保留名称/ });
    const fullList = view.client.getQueryData<SessionMeta[]>(["sessions"])!;
    act(() =>
      view.client.setQueryData(
        ["sessions"],
        fullList.filter((session) => session.sessionId !== "codex-session-1"),
      ),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /持续保留名称/ }),
      ).not.toBeInTheDocument(),
    );
    expect(
      JSON.parse(localStorage.getItem(ORGANIZER_STORAGE_KEY)!),
    ).toMatchObject({
      pinned: [key],
      archived: [key],
      names: { [key]: "持续保留名称" },
    });
    act(() => view.client.setQueryData(["sessions"], fullList));
    await userEvent.click(
      await screen.findByRole("button", { name: /持续保留名称/ }),
    );
    await userEvent.click(screen.getByRole("button", { name: "删除会话" }));
    await userEvent.click(
      within(screen.getByTestId("confirm-dialog")).getByRole("button", {
        name: "删除会话",
      }),
    );
    await waitFor(() =>
      expect(JSON.parse(localStorage.getItem(ORGANIZER_STORAGE_KEY)!)).toEqual({
        pinned: [],
        archived: [],
        names: {},
      }),
    );
  });

  it("mounts only viewport rows in a 942-session flat list and reaches later sessions by scrolling", async () => {
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(
      function (this: HTMLElement) {
        return this.getAttribute("aria-label") === "会话列表" ? 400 : 86;
      },
    );
    vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(320);
    const previousScrollTo = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype,
      "scrollTo",
    );
    Object.defineProperty(HTMLElement.prototype, "scrollTo", {
      configurable: true,
      value: vi.fn(),
    });
    try {
      setSessionFixtures(
        Array.from({ length: 942 }, (_, index) => ({
          providerId: "codex",
          sessionId: `large-${index}`,
          title: `Large Session ${index}`,
          lastActiveAt: 942 - index,
        })),
        {},
      );
      renderPage();
      expect(
        await screen.findByRole("button", { name: /Large Session 0\b/ }),
      ).toBeInTheDocument();
      expect(
        screen.getAllByRole("button", { name: /Large Session / }).length,
      ).toBeLessThan(30);
      expect(
        screen.queryByRole("button", { name: /Large Session 500\b/ }),
      ).not.toBeInTheDocument();
      fireEvent.scroll(screen.getByLabelText("会话列表"), {
        target: { scrollTop: 45000 },
      });
      expect(
        await screen.findByRole("button", { name: /Large Session 500\b/ }),
      ).toBeInTheDocument();
      expect(
        screen.getAllByRole("button", { name: /Large Session / }).length,
      ).toBeLessThan(30);
      expect(
        screen.queryByRole("button", { name: /Large Session 0\b/ }),
      ).not.toBeInTheDocument();
    } finally {
      if (previousScrollTo)
        Object.defineProperty(
          HTMLElement.prototype,
          "scrollTo",
          previousScrollTo,
        );
      else Reflect.deleteProperty(HTMLElement.prototype, "scrollTo");
    }
  });

  it("keeps both panes alive when a deeply scrolled 942-session list shrinks, results switch, and archived searches change", async () => {
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(
      function (this: HTMLElement) {
        return this.classList.contains("overflow-y-auto") ? 400 : 86;
      },
    );
    vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(320);
    const previousScrollTo = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype,
      "scrollTo",
    );
    Object.defineProperty(HTMLElement.prototype, "scrollTo", {
      configurable: true,
      value: vi.fn(function (this: HTMLElement, options: ScrollToOptions) {
        this.scrollTop = options.top ?? this.scrollTop;
      }),
    });
    const consoleError = vi.spyOn(console, "error");
    const searchContents = vi
      .spyOn(sessionsApi, "searchContents")
      .mockResolvedValue([]);
    const sessions: SessionMeta[] = Array.from({ length: 942 }, (_, index) => ({
      providerId: "codex",
      sessionId: `stable-${index}`,
      title:
        index === 500 || index === 501
          ? `Focus pair ${index}`
          : `Large session ${index}${index < 72 ? " Segment" : ""}`,
      sourcePath: `/mock/stable/session-${index}.jsonl`,
      lastActiveAt: 942 - index,
    }));
    const longMessages: SessionMessage[] = Array.from(
      { length: 250 },
      (_, index) => ({
        role: "user",
        content: `Long history row ${index}`,
        ts: index + 1,
      }),
    );
    setSessionFixtures(sessions, {
      "codex:/mock/stable/session-500.jsonl": longMessages,
      "codex:/mock/stable/session-501.jsonl": [
        { role: "user", content: "Short history for the other result", ts: 1 },
      ],
    });
    try {
      renderPage();
      await screen.findByRole("button", { name: /Large session 0\b/ });
      const list = screen.getByRole("region", { name: "会话列表" });
      fireEvent.scroll(list, { target: { scrollTop: 45000 } });
      await userEvent.click(
        await screen.findByRole("button", { name: /Focus pair 500/ }),
      );
      await screen.findByText("Long history row 0");
      const messageViewport = screen
        .getByText("Long history row 0")
        .closest(".overflow-y-auto")!;
      fireEvent.scroll(messageViewport, { target: { scrollTop: 9800 } });
      await waitFor(() =>
        expect(
          Number(
            screen
              .getAllByText(/^Long history row \d+$/)[0]
              .textContent!.split(" ")
              .at(-1),
          ),
        ).toBeGreaterThan(50),
      );
      const scrolledMessage = screen.getAllByText(/^Long history row \d+$/)[0]
        .textContent!;
      await openSearch();
      await userEvent.click(screen.getByRole("combobox", { name: /匹配方式/ }));
      await userEvent.click(
        await screen.findByRole("option", { name: /精准匹配/ }),
      );
      const input = screen.getByRole("textbox");
      fireEvent.change(input, { target: { value: "Focus pair" } });
      await waitFor(() =>
        expect(
          within(list).getAllByRole("button", { name: /Focus pair/ }),
        ).toHaveLength(2),
      );
      expect(list.scrollTop).toBe(0);
      await waitFor(() => expect(searchContents).toHaveBeenCalled());
      await userEvent.click(
        screen.getByRole("button", { name: /Focus pair 501/ }),
      );
      expect(
        await screen.findByRole("heading", { name: "Focus pair 501" }),
      ).toBeVisible();
      fireEvent.scroll(messageViewport, { target: { scrollTop: 0 } });
      expect(
        await screen.findByText("Short history for the other result"),
      ).toBeInTheDocument();
      expect(screen.queryByText(scrolledMessage)).not.toBeInTheDocument();
      await userEvent.click(
        screen.getByRole("button", { name: /Focus pair 500/ }),
      );
      expect(await screen.findByText("Long history row 0")).toBeInTheDocument();

      // The list stays virtualized when 942 becomes 72, then switches to a flat
      // handful and to no results; no stale virtual row may index the new array.
      fireEvent.change(input, { target: { value: "Segment" } });
      await waitFor(() =>
        expect(
          screen.queryByRole("heading", { name: "Focus pair 500" }),
        ).not.toBeInTheDocument(),
      );
      fireEvent.scroll(list, { target: { scrollTop: 0 } });
      expect(
        await screen.findByRole("button", { name: /Large session 0\b/ }),
      ).toBeInTheDocument();
      fireEvent.scroll(list, { target: { scrollTop: 6000 } });
      expect(
        await screen.findByRole("button", { name: /Large session 66\b/ }),
      ).toBeInTheDocument();
      fireEvent.change(input, {
        target: { value: "No matching conversation anywhere" },
      });
      await waitFor(() =>
        expect(
          within(list).queryByRole("button", { name: /Large session/ }),
        ).not.toBeInTheDocument(),
      );
      expect(list).toBeInTheDocument();
      expect(screen.getByRole("textbox")).toHaveValue(
        "No matching conversation anywhere",
      );
      fireEvent.change(input, { target: { value: "Focus pair" } });
      const toArchive = await screen.findByRole("button", {
        name: /Focus pair 501/,
      });
      await userEvent.click(toArchive);
      await userEvent.click(
        within(toArchive.parentElement!).getByRole("button", { name: "归档" }),
      );
      expect(
        screen.getByRole("button", { name: /Focus pair 501/ }),
      ).toHaveTextContent("已归档");
      await closeSearch();
      expect(
        await screen.findByRole("region", { name: "会话列表" }),
      ).toBeInTheDocument();
      await userEvent.click(screen.getByRole("button", { name: /^已归档/ }));
      expect(
        await screen.findByRole("button", { name: /Focus pair 501/ }),
      ).toBeInTheDocument();
      await openSearch();
      fireEvent.change(screen.getByRole("textbox"), {
        target: { value: "Focus pair 500" },
      });
      await userEvent.click(
        await screen.findByRole("button", { name: /Focus pair 500/ }),
      );
      expect(
        await screen.findByRole("heading", { name: "Focus pair 500" }),
      ).toBeVisible();
      expect(
        screen.getByRole("region", { name: "会话列表" }),
      ).toBeInTheDocument();
      expect(searchContents).toHaveBeenCalled();
      // TanStack's default synchronous scroll notification can warn during a
      // React 18 layout effect in development. Keep that exact warning visible
      // in the log; all other console errors and any runtime exception fail.
      const knownVirtualizerWarning =
        "Warning: flushSync was called from inside a lifecycle method. React cannot flush when React is already rendering. Consider moving this call to a scheduler task or micro task.%s";
      expect(
        consoleError.mock.calls.filter(
          ([message]) => message !== knownVirtualizerWarning,
        ),
      ).toEqual([]);
    } finally {
      if (previousScrollTo)
        Object.defineProperty(
          HTMLElement.prototype,
          "scrollTo",
          previousScrollTo,
        );
      else Reflect.deleteProperty(HTMLElement.prototype, "scrollTo");
    }
  });

  it("keeps search results and the detail pane usable when one result fails to read with an FD error", async () => {
    vi.spyOn(sessionsApi, "searchContents").mockResolvedValue([
      {
        providerId: "codex",
        sessionId: "codex-session-2",
        sourcePath: "/mock/codex/session-2.jsonl",
        snippet: "isolated body match",
        matchCount: 1,
        score: 1001,
      },
      {
        providerId: "codex",
        sessionId: "codex-session-3",
        sourcePath: "/mock/codex/session-3.jsonl",
        snippet: "isolated body match",
        matchCount: 1,
        score: 1001,
      },
    ]);
    vi.spyOn(sessionsApi, "getMessages").mockImplementation(
      async (_provider, sourcePath) => {
        if (sourcePath.endsWith("session-2.jsonl"))
          throw new Error("Too many open files (os error 24)");
        return [{ role: "user", content: "Readable original message", ts: 1 }];
      },
    );
    const view = renderPage();
    await screen.findByRole("button", { name: /Alpha Session/ });
    await openSearch();
    await userEvent.type(screen.getByRole("textbox"), "isolated body match");
    await userEvent.click(
      await screen.findByRole("button", { name: /Beta Session/ }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Too many open files (os error 24)",
    );
    expect(screen.getByRole("heading", { name: "Beta Session" })).toBeVisible();
    expect(
      screen.getByRole("region", { name: "会话列表" }),
    ).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: /Gamma Session/ }),
    );
    expect(
      await screen.findByRole("heading", { name: "Gamma Session" }),
    ).toBeVisible();
    await waitFor(() =>
      expect(
        view.client.getQueryData([
          "sessionMessages",
          "codex",
          "/mock/codex/session-3.jsonl",
        ]),
      ).toEqual([
        { role: "user", content: "Readable original message", ts: 1 },
      ]),
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("textbox")).toHaveValue("isolated body match");
    expect(
      screen.getByRole("button", { name: /Beta Session/ }),
    ).toBeInTheDocument();
  });

  it("includes archived keyword hits with an archived label while keeping empty-search tabs separate", async () => {
    window.localStorage.setItem(
      ORGANIZER_STORAGE_KEY,
      JSON.stringify({
        pinned: [],
        archived: ["codex:codex-session-2:/mock/codex/session-2.jsonl"],
      }),
    );
    renderPage();

    await screen.findByRole("button", { name: /Alpha Session/ });
    expect(
      screen.queryByRole("button", { name: /Beta Session/ }),
    ).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /^已归档/ }));
    expect(
      await screen.findByRole("button", { name: /Beta Session/ }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Alpha Session/ }),
    ).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /^会话\s/ }));
    await openSearch();
    await userEvent.type(screen.getByRole("textbox"), "Beta");

    const archivedHit = await screen.findByRole("button", {
      name: /Beta Session/,
    });
    expect(within(archivedHit).getByText("已归档")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: /Alpha Session/ }),
    ).not.toBeInTheDocument();

    await userEvent.clear(screen.getByRole("textbox"));
    expect(
      await screen.findByRole("button", { name: /Alpha Session/ }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Beta Session/ }),
    ).not.toBeInTheDocument();
  });

  it("finds archived conversations through full text from the normal tab", async () => {
    window.localStorage.setItem(
      ORGANIZER_STORAGE_KEY,
      JSON.stringify({
        pinned: [],
        archived: ["codex:codex-session-2:/mock/codex/session-2.jsonl"],
      }),
    );
    const searchContents = vi
      .spyOn(sessionsApi, "searchContents")
      .mockResolvedValue([
        {
          providerId: "codex",
          sessionId: "codex-session-2",
          sourcePath: "/mock/codex/session-2.jsonl",
          matchCount: 1,
          score: 1001,
          snippet: "归档正文中的专属关键词",
        },
      ]);
    renderPage();
    await screen.findByRole("button", { name: /Alpha Session/ });
    await openSearch();
    await userEvent.type(screen.getByRole("textbox"), "专属关键词");

    const archivedHit = await screen.findByRole("button", {
      name: /Beta Session/,
    });
    expect(within(archivedHit).getByText("已归档")).toBeVisible();
    expect(archivedHit).toHaveTextContent("归档正文中的专属关键词");
    expect(searchContents).toHaveBeenCalledWith(
      expect.arrayContaining([
        expect.objectContaining({ sessionId: "codex-session-2" }),
      ]),
      "专属关键词",
      "fuzzy",
      3,
      expect.any(String),
    );
  });

  it("starts with the current Agent and resets explicit merged search when appId changes", async () => {
    const searchContents = vi
      .spyOn(sessionsApi, "searchContents")
      .mockResolvedValue([]);
    const view = renderPage("codex");
    await screen.findByRole("button", { name: /Alpha Session/ });
    expect(
      screen.queryByRole("button", { name: /Claude Session/ }),
    ).not.toBeInTheDocument();
    await openSearch();
    await userEvent.type(screen.getByRole("textbox"), "Session");
    await waitFor(() => expect(searchContents).toHaveBeenCalled());
    expect(
      searchContents.mock.lastCall![0].map((item) => item.providerId),
    ).toEqual(["codex", "codex", "codex"]);
    await closeSearch();

    await switchProviderFilter(/^合并检索所有 Agent$/);
    expect(
      await screen.findByRole("button", { name: /Claude Session/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Alpha Session/ }),
    ).toBeInTheDocument();
    await openSearch();
    await userEvent.type(screen.getByRole("textbox"), "Session");
    await waitFor(() =>
      expect(searchContents.mock.lastCall![0]).toHaveLength(4),
    );

    view.rerender(
      <QueryClientProvider client={view.client}>
        <SessionManagerPage appId="claude" />
      </QueryClientProvider>,
    );
    expect(
      await screen.findByRole("button", { name: /Claude Session/ }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Alpha Session/ }),
    ).not.toBeInTheDocument();
    // Narrowing can reuse verified fulltext cache. A manual refresh must still
    // send only the newly selected Agent's candidates.
    await userEvent.click(screen.getByRole("button", { name: /刷新/ }));
    await waitFor(() =>
      expect(searchContents.mock.lastCall![0]).toEqual([
        {
          providerId: "claude",
          sessionId: "claude-session-1",
          sourcePath: "/mock/claude/session-1.jsonl",
        },
      ]),
    );

    view.rerender(
      <QueryClientProvider client={view.client}>
        <SessionManagerPage appId="codex" />
      </QueryClientProvider>,
    );
    expect(
      await screen.findByRole("button", { name: /Alpha Session/ }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Claude Session/ }),
    ).not.toBeInTheDocument();
  });

  it("offers recent activity windows and orders recent sessions by activity ahead of pins", async () => {
    const now = Date.UTC(2026, 8, 15, 12);
    vi.spyOn(Date, "now").mockReturnValue(now);
    const day = 24 * 60 * 60 * 1000;
    const entries: SessionMeta[] = [
      {
        providerId: "codex",
        sessionId: "newest",
        title: "Newest Activity",
        lastActiveAt: now - 60 * 60 * 1000,
      },
      {
        providerId: "codex",
        sessionId: "pinned",
        title: "Pinned Earlier",
        lastActiveAt: now - 2 * 60 * 60 * 1000,
      },
      {
        providerId: "codex",
        sessionId: "two-days",
        title: "Two Days Ago",
        lastActiveAt: now - 2 * day,
      },
      {
        providerId: "codex",
        sessionId: "ten-days",
        title: "Ten Days Ago",
        lastActiveAt: now - 10 * day,
      },
      {
        providerId: "codex",
        sessionId: "old",
        title: "Old Activity",
        lastActiveAt: now - 31 * day,
        createdAt: now,
      },
    ];
    setSessionFixtures(entries, {});
    window.localStorage.setItem(
      ORGANIZER_STORAGE_KEY,
      JSON.stringify({ pinned: ["codex:pinned:"], archived: [] }),
    );
    renderPage();
    await screen.findByRole("button", { name: /Newest Activity/ });
    const names = entries.map((entry) => entry.title!);
    const visibleNames = () =>
      screen.queryAllByRole("button").flatMap((button) => {
        const name = names.find((title) => button.textContent?.includes(title));
        return name ? [name] : [];
      });
    expect(visibleNames()).toEqual([
      "Pinned Earlier",
      "Newest Activity",
      "Two Days Ago",
      "Ten Days Ago",
      "Old Activity",
    ]);
    await userEvent.click(screen.getByRole("combobox", { name: "最近活动" }));
    expect(
      screen.getAllByRole("option").map((option) => option.textContent),
    ).toEqual(["全部时间", "最近 24 小时", "最近 7 天", "最近 30 天"]);
    await userEvent.click(screen.getByRole("option", { name: "最近 24 小时" }));
    expect(visibleNames()).toEqual(["Newest Activity", "Pinned Earlier"]);

    await userEvent.click(screen.getByRole("combobox", { name: "最近活动" }));
    await userEvent.click(screen.getByRole("option", { name: "最近 7 天" }));
    expect(visibleNames()).toEqual([
      "Newest Activity",
      "Pinned Earlier",
      "Two Days Ago",
    ]);
    await userEvent.click(screen.getByRole("combobox", { name: "最近活动" }));
    await userEvent.click(screen.getByRole("option", { name: "最近 30 天" }));
    expect(visibleNames()).toEqual([
      "Newest Activity",
      "Pinned Earlier",
      "Two Days Ago",
      "Ten Days Ago",
    ]);
    await userEvent.click(screen.getByRole("combobox", { name: "最近活动" }));
    await userEvent.click(screen.getByRole("option", { name: "全部时间" }));
    expect(visibleNames()).toEqual([
      "Pinned Earlier",
      "Newest Activity",
      "Two Days Ago",
      "Ten Days Ago",
      "Old Activity",
    ]);
  });

  it("refreshes current messages and unchanged-session fulltext results without clearing the query", async () => {
    const searchContents = vi
      .spyOn(sessionsApi, "searchContents")
      .mockResolvedValue([]);
    const getMessages = vi
      .spyOn(sessionsApi, "getMessages")
      .mockResolvedValue([{ role: "user", content: "before refresh", ts: 20 }]);
    const view = renderPage();
    await screen.findByRole("heading", { name: "Alpha Session" });
    await waitFor(() =>
      expect(
        view.client.getQueryData([
          "sessionMessages",
          "codex",
          "/mock/codex/session-1.jsonl",
        ]),
      ).toEqual([{ role: "user", content: "before refresh", ts: 20 }]),
    );
    await openSearch();
    await userEvent.type(screen.getByRole("textbox"), "Alpha");
    await waitFor(() => expect(searchContents).toHaveBeenCalled());
    const callsBeforeRefresh = searchContents.mock.calls.length;
    getMessages.mockResolvedValue([
      { role: "user", content: "after refresh", ts: 21 },
    ]);
    searchContents.mockResolvedValue([
      {
        providerId: "codex",
        sessionId: "codex-session-2",
        sourcePath: "/mock/codex/session-2.jsonl",
        matchCount: 1,
        score: 1001,
        snippet: "Alpha appeared in newly saved body",
      },
    ]);

    await userEvent.click(screen.getByRole("button", { name: /刷新/ }));
    await waitFor(() =>
      expect(searchContents.mock.calls.length).toBeGreaterThan(
        callsBeforeRefresh,
      ),
    );
    expect(
      await screen.findByRole("button", { name: /Beta Session/ }),
    ).toHaveTextContent("Alpha appeared in newly saved body");
    expect(screen.getByRole("textbox")).toHaveValue("Alpha");
    await waitFor(() =>
      expect(
        view.client.getQueryData([
          "sessionMessages",
          "codex",
          "/mock/codex/session-1.jsonl",
        ]),
      ).toEqual([{ role: "user", content: "after refresh", ts: 21 }]),
    );
  });

  it("keeps an empty search open while changing the match mode", async () => {
    renderPage();

    await screen.findByRole("heading", { name: "Alpha Session" });
    await openSearch();

    const searchInput = screen.getByRole("textbox");
    await waitFor(() => expect(searchInput).toHaveFocus());

    await userEvent.click(screen.getByRole("combobox", { name: /匹配方式/i }));
    await userEvent.click(
      await screen.findByRole("option", { name: /精准匹配/i }),
    );

    expect(screen.getByRole("textbox")).toBeInTheDocument();
    await waitFor(() =>
      expect(window.localStorage.getItem(SEARCH_MODE_STORAGE_KEY)).toBe(
        "exact",
      ),
    );
  });

  it("closes and clears search only through Escape or the close button", async () => {
    renderPage();

    await screen.findByRole("heading", { name: "Alpha Session" });
    await openSearch();

    await userEvent.type(screen.getByRole("textbox"), "Alpha");
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();

    await openSearch();
    expect(screen.getByRole("textbox")).toHaveValue("");
    await userEvent.type(screen.getByRole("textbox"), "Beta");
    await closeSearch();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();

    await openSearch();
    expect(screen.getByRole("textbox")).toHaveValue("");
  });

  it("surfaces a relative Pi sessionDir instead of presenting an empty scan as authoritative", async () => {
    const discovery = vi.spyOn(piApi, "getSessionDiscovery").mockResolvedValue({
      status: "requires_project_context",
      configuredPath: ".pi/sessions",
    });

    renderPage("pi");

    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent(".pi/sessions");
    expect(discovery).toHaveBeenCalledTimes(1);
    discovery.mockRestore();
  });

  it("deletes the selected session and selects the next visible session", async () => {
    renderPage();

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Alpha Session" }),
      ).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("button", { name: /删除会话/i }));

    const dialog = screen.getByTestId("confirm-dialog");
    expect(dialog).toBeInTheDocument();
    expect(within(dialog).getByText(/Alpha Session/)).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: /删除会话/i }));

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Beta Session" }),
      ).toBeInTheDocument(),
    );

    expect(screen.queryByText("Alpha Session")).not.toBeInTheDocument();
    expect(toastErrorMock).not.toHaveBeenCalled();
    expect(toastSuccessMock).toHaveBeenCalled();
  });

  it("removes a deleted session from filtered search results", async () => {
    renderPage();

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Alpha Session" }),
      ).toBeInTheDocument(),
    );

    await openSearch();

    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "Alpha" },
    });

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Alpha Session" }),
      ).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("button", { name: /删除会话/i }));

    const dialog = screen.getByTestId("confirm-dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /删除会话/i }));

    await waitFor(() =>
      expect(screen.queryByText("Alpha Session")).not.toBeInTheDocument(),
    );

    expect(
      screen.getByText("sessionManager.selectSession"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("sessionManager.emptySession"),
    ).not.toBeInTheDocument();
    expect(toastErrorMock).not.toHaveBeenCalled();
    expect(toastSuccessMock).toHaveBeenCalled();
  });

  it("restores batch delete controls when deleteMany rejects", async () => {
    const deleteManySpy = vi
      .spyOn(sessionsApi, "deleteMany")
      .mockRejectedValueOnce(new Error("network error"));

    renderPage();

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Alpha Session" }),
      ).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("button", { name: /批量管理/i }));
    fireEvent.click(screen.getByRole("button", { name: /全选当前/i }));
    fireEvent.click(screen.getByRole("button", { name: /批量删除/i }));

    const dialog = screen.getByTestId("confirm-dialog");
    fireEvent.click(
      within(dialog).getByRole("button", { name: /删除所选会话/i }),
    );

    await waitFor(() =>
      expect(toastErrorMock).toHaveBeenCalledWith("network error"),
    );

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /批量删除/i }),
      ).not.toBeDisabled(),
    );

    deleteManySpy.mockRestore();
  });

  it("keeps the exit batch mode button visible when search hides all sessions", async () => {
    renderPage();

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Alpha Session" }),
      ).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("button", { name: /批量管理/i }));
    await openSearch();
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "NoSuchSession" },
    });

    await waitFor(() => expect(screen.queryByText("Alpha Session")).toBeNull());

    expect(screen.getByRole("button", { name: /退出批量管理/i })).toBeVisible();
  });

  it("drops hidden selections when search narrows the result set", async () => {
    renderPage();

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Alpha Session" }),
      ).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("button", { name: /批量管理/i }));
    fireEvent.click(screen.getByRole("button", { name: /全选当前/i }));

    expect(screen.getByText("已选 3 项")).toBeInTheDocument();

    await openSearch();
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "Alpha" },
    });

    await waitFor(() =>
      expect(screen.queryByText("Beta Session")).not.toBeInTheDocument(),
    );

    await closeSearch();

    await waitFor(() =>
      expect(screen.getByText("已选 1 项")).toBeInTheDocument(),
    );
  });

  it("removes successfully deleted sessions from the UI before refetch completes", async () => {
    const view = renderPage();
    let resolveInvalidate!: () => void;
    const invalidateSpy = vi
      .spyOn(view.client, "invalidateQueries")
      .mockImplementation(
        () =>
          new Promise((resolve) => {
            resolveInvalidate = () => resolve(undefined);
          }),
      );

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Alpha Session" }),
      ).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("button", { name: /批量管理/i }));
    fireEvent.click(screen.getByRole("button", { name: /全选当前/i }));
    fireEvent.click(screen.getByRole("button", { name: /批量删除/i }));

    const dialog = screen.getByTestId("confirm-dialog");
    fireEvent.click(
      within(dialog).getByRole("button", { name: /删除所选会话/i }),
    );

    await waitFor(() => {
      expect(screen.queryByText("Alpha Session")).not.toBeInTheDocument();
      expect(screen.queryByText("Beta Session")).not.toBeInTheDocument();
    });

    await act(async () => {
      resolveInvalidate();
    });
    invalidateSpy.mockRestore();
  });

  it("switches to grouped view collapsed by default and shows collapse control", async () => {
    renderPage("all");

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Claude Session" }),
      ).toBeInTheDocument(),
    );

    await switchToGroupedView();

    expect(
      screen.getByRole("button", { name: /全部收起/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: /展开或折叠 codex 供应商分组/,
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: /展开或折叠 claude 供应商分组/,
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /展开或折叠 codex 目录分组/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Alpha Session/ }),
    ).not.toBeInTheDocument();
  });

  it("persists manual expansion and collapses all grouped sessions", async () => {
    renderPage();

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Alpha Session" }),
      ).toBeInTheDocument(),
    );

    await switchToGroupedView();
    expandDirectoryGroup("codex", "codex");

    expect(
      screen.getByRole("button", { name: /展开或折叠 codex 目录分组/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Alpha Session/ }),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(
        JSON.parse(window.localStorage.getItem(GROUP_EXPANSION_STORAGE_KEY)!),
      ).toEqual({
        expandedProviderIds: ["codex"],
        expandedDirectoryKeys: ["codex:/mock/codex"],
      }),
    );

    collapseAllGroups();

    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /展开或折叠 codex 目录分组/ }),
      ).not.toBeInTheDocument(),
    );
    await waitFor(() =>
      expect(
        JSON.parse(window.localStorage.getItem(GROUP_EXPANSION_STORAGE_KEY)!),
      ).toEqual({
        expandedProviderIds: [],
        expandedDirectoryKeys: [],
      }),
    );
  });

  it("keeps filtered grouped sessions collapsed until expanding the group", async () => {
    const view = renderPage("all");

    await screen.findByText("Alpha Session", {}, { timeout: 3000 });

    fireEvent.click(screen.getByRole("button", { name: /Alpha Session/ }));
    await switchToGroupedView();
    view.rerender(
      <QueryClientProvider client={view.client}>
        <SessionManagerPage appId="claude" />
      </QueryClientProvider>,
    );

    await waitFor(() =>
      expect(screen.queryByText("Alpha Session")).not.toBeInTheDocument(),
    );

    expect(
      screen.getByRole("heading", { name: "Claude Session" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: /展开或折叠 claude 供应商分组/,
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /展开或折叠 claude 目录分组/ }),
    ).not.toBeInTheDocument();

    expandDirectoryGroup("claude", "claude");

    expect(
      screen.getByRole("button", { name: /展开或折叠 claude 目录分组/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Claude Session/ }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Gamma Session")).not.toBeInTheDocument();
  });

  it("supports batch deletion from grouped view", async () => {
    renderPage();

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Alpha Session" }),
      ).toBeInTheDocument(),
    );

    await switchToGroupedView();
    fireEvent.click(screen.getByRole("button", { name: /批量管理/i }));
    fireEvent.click(screen.getByRole("button", { name: /全选当前/i }));
    fireEvent.click(screen.getByRole("button", { name: /批量删除/i }));

    const dialog = screen.getByTestId("confirm-dialog");
    fireEvent.click(
      within(dialog).getByRole("button", { name: /删除所选会话/i }),
    );

    await waitFor(() => {
      expect(screen.queryByText("Alpha Session")).not.toBeInTheDocument();
      expect(screen.queryByText("Beta Session")).not.toBeInTheDocument();
      expect(screen.queryByText("Gamma Session")).not.toBeInTheDocument();
    });

    expect(toastErrorMock).not.toHaveBeenCalled();
    expect(toastSuccessMock).toHaveBeenCalled();
  });

  it("selects visible deletable sessions by provider group in grouped batch mode", async () => {
    renderPage("all");

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Claude Session" }),
      ).toBeInTheDocument(),
    );

    await enterGroupedBatchMode();

    const codexProviderCheckbox = screen.getByRole("checkbox", {
      name: /选择 codex 供应商分组内会话/,
    });
    const claudeProviderCheckbox = screen.getByRole("checkbox", {
      name: /选择 claude 供应商分组内会话/,
    });

    fireEvent.click(codexProviderCheckbox);

    expect(codexProviderCheckbox).toBeChecked();
    expect(claudeProviderCheckbox).not.toBeChecked();
    expect(screen.getByText("已选 3 项")).toBeInTheDocument();

    fireEvent.click(codexProviderCheckbox);

    expect(codexProviderCheckbox).not.toBeChecked();
    expect(screen.getByText("已选 0 项")).toBeInTheDocument();
  });

  it("selects visible deletable sessions by directory group and marks the provider as mixed", async () => {
    renderPage();

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Alpha Session" }),
      ).toBeInTheDocument(),
    );

    await enterGroupedBatchMode();
    expandDirectoryGroup("codex", "codex");

    const providerCheckbox = screen.getByRole("checkbox", {
      name: /选择 codex 供应商分组内会话/,
    });
    const codexDirectoryCheckbox = screen.getByRole("checkbox", {
      name: /选择 codex 目录分组内会话/,
    });

    fireEvent.click(codexDirectoryCheckbox);

    expect(codexDirectoryCheckbox).toBeChecked();
    expect(providerCheckbox).toHaveAttribute("aria-checked", "mixed");
    expect(screen.getByText("已选 2 项")).toBeInTheDocument();
  });

  it("marks grouped batch checkboxes as mixed when only one session is selected", async () => {
    renderPage();

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Alpha Session" }),
      ).toBeInTheDocument(),
    );

    await enterGroupedBatchMode();
    expandDirectoryGroup("codex", "codex");

    fireEvent.click(screen.getAllByRole("checkbox", { name: "选择会话" })[0]);

    expect(
      screen.getByRole("checkbox", {
        name: /选择 codex 供应商分组内会话/,
      }),
    ).toHaveAttribute("aria-checked", "mixed");
    expect(
      screen.getByRole("checkbox", { name: /选择 codex 目录分组内会话/ }),
    ).toHaveAttribute("aria-checked", "mixed");
    expect(screen.getByText("已选 1 项")).toBeInTheDocument();
  });

  it("batch deletes only sessions selected from a grouped directory", async () => {
    renderPage();

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Alpha Session" }),
      ).toBeInTheDocument(),
    );

    await enterGroupedBatchMode();
    expandDirectoryGroup("codex", "codex");
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: /选择 codex 目录分组内会话/,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: /批量删除/i }));

    const dialog = screen.getByTestId("confirm-dialog");
    fireEvent.click(
      within(dialog).getByRole("button", { name: /删除所选会话/i }),
    );

    await waitFor(() => {
      expect(screen.queryByText("Alpha Session")).not.toBeInTheDocument();
      expect(screen.queryByText("Beta Session")).not.toBeInTheDocument();
    });

    expect(
      screen.getByRole("button", { name: /展开或折叠 未知目录 目录分组/ }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("checkbox", { name: "选择会话" }),
    ).not.toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: /展开或折叠 未知目录 目录分组/ }),
    );
    expect(
      screen.getByRole("checkbox", { name: "选择会话" }),
    ).toBeInTheDocument();
    expect(toastErrorMock).not.toHaveBeenCalled();
    expect(toastSuccessMock).toHaveBeenCalled();
  });
});
