import { StrictMode } from "react";
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
import * as core from "@tauri-apps/api/core";
import { ToolVersionsSection } from "@/components/settings/ToolVersionsSection";
import { settingsApi, type ToolVersion } from "@/lib/api/settings";

const names = [
  "claude",
  "codex",
  "gemini",
  "grok",
  "opencode",
  "openclaw",
  "hermes",
  "pi",
  "claude-desktop",
];
const fixture = (
  name: string,
  overrides: Partial<ToolVersion> = {},
): ToolVersion => ({
  name,
  version: "1.2.3",
  latest_version: "1.2.3",
  error: null,
  installed_but_broken: false,
  env_type: "macos",
  wsl_distro: null,
  checked_at: 1_789_689_600_000,
  source_url: "https://registry.npmjs.org/example/latest",
  ...overrides,
});
const row = (label: string) => within(screen.getByRole("row", { name: label }));
const settled = () =>
  waitFor(() =>
    expect(screen.getByRole("status")).toHaveTextContent("已检查 9 / 9"),
  );

describe("ToolVersionsSection", () => {
  beforeEach(() => {
    vi.spyOn(settingsApi, "getToolVersions").mockImplementation(async (tools) =>
      tools!.map((name) => fixture(name)),
    );
  });
  afterEach(() => vi.restoreAllMocks());

  it("checks all nine tools once, includes the BinCode shared channel, and never invokes install or upgrade", async () => {
    const lifecycle = vi.spyOn(settingsApi, "runToolLifecycleAction");
    const invoke = vi.spyOn(core, "invoke");
    render(
      <StrictMode>
        <ToolVersionsSection />
      </StrictMode>,
    );
    await settled();
    expect(
      vi.mocked(settingsApi.getToolVersions).mock.calls.map(([tools]) => tools),
    ).toEqual(names.map((name) => [name]));
    for (const label of [
      "Claude Code",
      "Codex",
      "Gemini CLI",
      "Grok Build",
      "OpenCode",
      "OpenClaw",
      "Hermes",
      "Pi",
      "Claude Desktop",
    ]) {
      expect(row(label).getByText("已最新")).toBeVisible();
      expect(
        row(label).getByRole("button", { name: `${label} 重新检查` }),
      ).toBeEnabled();
    }
    const alias = row("BinCode · 复用 OpenCode 通道");
    expect(
      alias.getByText("复用 OpenCode 通道；非独立 BinCode 版本"),
    ).toBeVisible();
    expect(alias.getByText("已最新")).toBeVisible();
    expect(alias.queryByRole("button")).not.toBeInTheDocument();
    expect(lifecycle).not.toHaveBeenCalled();
    expect(invoke).not.toHaveBeenCalled();
    expect(
      screen.queryByRole("button", { name: /安装|升级/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "检查全部" })).toBeEnabled();
  });

  it("separates outdated, ahead, absent, malformed, blocked and partial network failures", async () => {
    const overrides: Record<string, Partial<ToolVersion>> = {
      claude: {
        version: "v1.2.3+local.build",
        latest_version: "1.2.3+registry.build",
      },
      codex: { version: "1.0.0", latest_version: "1.1.0" },
      gemini: { version: "2.0.0-beta.1", latest_version: "1.9.0" },
      grok: {
        version: null,
        error: "Command not found",
        status: "not_installed",
      },
      opencode: { version: "1.2.3garbage" },
      openclaw: {
        latest_version: null,
        latest_error:
          "Network request failed\nTLS certificate details\nTry the official source again",
      },
      hermes: { latest_version: "unknown" },
      pi: {
        version: null,
        installed_but_broken: true,
        error: "Node runtime is too old",
      },
      "claude-desktop": {
        version: null,
        status: "blocked",
        local_error: "正在运行的桌面客户端不允许探测",
      },
    };
    vi.mocked(settingsApi.getToolVersions).mockImplementation(async (tools) =>
      tools!.map((name) => fixture(name, overrides[name])),
    );
    render(<ToolVersionsSection />);
    await settled();
    expect(row("Claude Code").getByText("已最新")).toBeVisible();
    expect(row("Codex").getByText("可更新")).toBeVisible();
    expect(row("Gemini CLI").getByText("本地版本领先")).toBeVisible();
    expect(row("Grok Build").getByText("未安装")).toBeVisible();
    expect(row("OpenCode").getByText("本地检查失败")).toBeVisible();
    expect(
      row("BinCode · 复用 OpenCode 通道").getByText("本地检查失败"),
    ).toBeVisible();
    expect(row("OpenClaw").getByText("远端检查失败")).toBeVisible();
    expect(row("Hermes").getByText("远端检查失败")).toBeVisible();
    expect(row("Pi").getByText("本地检查失败")).toBeVisible();
    expect(row("Claude Desktop").getByText("检查受阻")).toBeVisible();
    await userEvent.click(row("OpenClaw").getByText("查看错误详情"));
    expect(row("OpenClaw").getByText(/TLS certificate details/)).toBeVisible();
    expect(
      row("OpenClaw").getByText(/Try the official source again/),
    ).toBeVisible();
  });

  it.each([
    ["2.1.263", "2.1.274", null, "安装版本可更新 · 运行入口受限"],
    ["2.1.274", "2.1.274", null, "安装版本已最新 · 运行入口受限"],
    ["2.1.275", "2.1.274", null, "安装版本领先 · 运行入口受限"],
    [null, "2.1.274", null, "检查受阻"],
    ["2.1.263invalid", "2.1.274", null, "检查受阻"],
    ["2.1.263", "2.1.274", "Registry unavailable", "检查受阻"],
  ])(
    "compares protected installation metadata %s versus %s without claiming the launcher is runnable",
    async (version, latest_version, latest_error, status) => {
      vi.mocked(settingsApi.getToolVersions).mockImplementation(async (tools) =>
        tools!.map((name) =>
          fixture(
            name,
            name === "claude"
              ? {
                  version,
                  latest_version,
                  latest_error,
                  status: "blocked",
                  local_error: "受保护的 CLI 运行入口，未执行版本命令",
                }
              : {},
          ),
        ),
      );
      const lifecycle = vi.spyOn(settingsApi, "runToolLifecycleAction");
      render(<ToolVersionsSection />);
      await settled();
      expect(row("Claude Code").getByText(status)).toBeVisible();
      expect(
        row("Claude Code").queryByText("已最新", { exact: true }),
      ).not.toBeInTheDocument();
      await userEvent.click(row("Claude Code").getByText("查看错误详情"));
      expect(
        row("Claude Code").getByText(/受保护的 CLI 运行入口，未执行版本命令/),
      ).toBeVisible();
      expect(lifecycle).not.toHaveBeenCalled();
    },
  );

  it.each([
    ["1.2", "1.2.3", "本地检查失败"],
    ["1.2.3", "1.2.3invalid", "远端检查失败"],
    ["01.2.3", "1.2.3", "本地检查失败"],
    ["1.2.3-beta.01", "1.2.3", "本地检查失败"],
    ["1.2.3", null, "远端检查失败"],
    ["2.0.0", "1.9.9", "本地版本领先"],
    ["1.2.3-beta.2", "1.2.3-beta.11", "可更新"],
  ])(
    "compares %s and %s conservatively as %s",
    async (version, latest_version, status) => {
      vi.mocked(settingsApi.getToolVersions).mockImplementation(async (tools) =>
        tools!.map((name) =>
          fixture(name, name === "codex" ? { version, latest_version } : {}),
        ),
      );
      render(<ToolVersionsSection />);
      await settled();
      expect(row("Codex").getByText(status)).toBeVisible();
      expect(row("Codex").queryByText("已最新")).not.toBeInTheDocument();
    },
  );

  it("does not treat a missing response, an unsupported platform or a failed transport as an up-to-date tool", async () => {
    vi.mocked(settingsApi.getToolVersions).mockImplementation(async (tools) => {
      if (tools![0] === "claude") return [];
      if (tools![0] === "codex") throw { message: "IPC probe unavailable" };
      return tools!.map((name) =>
        fixture(
          name,
          name === "claude-desktop"
            ? {
                status: "unsupported",
                version: null,
                local_error: "当前系统不支持此桌面版本来源",
              }
            : {},
        ),
      );
    });
    render(<ToolVersionsSection />);
    await settled();
    expect(row("Claude Code").getByText("本地检查失败")).toBeVisible();
    expect(
      row("Claude Code").getByText("检查未返回该工具的版本信息"),
    ).toBeInTheDocument();
    expect(row("Codex").getByText("本地检查失败")).toBeVisible();
    expect(row("Codex").getByText("IPC probe unavailable")).toBeInTheDocument();
    expect(row("Claude Desktop").getByText("当前平台不支持")).toBeVisible();
  });

  it("clears the old successful badge while retrying and keeps a remote failure visible instead of stale success", async () => {
    render(<ToolVersionsSection />);
    await settled();
    let finish!: (value: ToolVersion[]) => void;
    vi.mocked(settingsApi.getToolVersions).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const retry = row("Claude Code").getByRole("button", {
      name: "Claude Code 重新检查",
    });
    fireEvent.click(retry);
    fireEvent.click(retry);
    await waitFor(() =>
      expect(settingsApi.getToolVersions).toHaveBeenCalledTimes(10),
    );
    expect(row("Claude Code").queryByText("已最新")).not.toBeInTheDocument();
    expect(retry).toBeDisabled();
    await act(async () =>
      finish([
        fixture("claude", {
          latest_error: "Registry unavailable",
          latest_version: "1.2.3",
        }),
      ]),
    );
    expect(row("Claude Code").getByText("远端检查失败")).toBeVisible();
    expect(row("Claude Code").queryByText("已最新")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "检查全部" }));
    fireEvent.click(screen.getByRole("button", { name: "检查全部" }));
    await settled();
    expect(settingsApi.getToolVersions).toHaveBeenCalledTimes(19);
    expect(row("Claude Code").getByText("已最新")).toBeVisible();
  });

  it("limits all checks to two concurrent requests and does not enqueue duplicates on repeated clicks", async () => {
    const resolvers: Array<() => void> = [];
    let active = 0;
    let maximumActive = 0;
    vi.mocked(settingsApi.getToolVersions).mockImplementation(
      (tools) =>
        new Promise((resolve) => {
          active++;
          maximumActive = Math.max(maximumActive, active);
          resolvers.push(() => {
            active--;
            resolve([fixture(tools![0])]);
          });
        }),
    );
    const view = render(<ToolVersionsSection />);
    try {
      await waitFor(() =>
        expect(settingsApi.getToolVersions).toHaveBeenCalledTimes(2),
      );
      fireEvent.click(screen.getByRole("button", { name: "检查全部" }));
      fireEvent.click(
        row("Claude Code").getByRole("button", {
          name: "Claude Code 重新检查",
        }),
      );
      expect(settingsApi.getToolVersions).toHaveBeenCalledTimes(2);
      for (let index = 0; index < names.length; index++) {
        await waitFor(() => expect(resolvers.length).toBeGreaterThan(index));
        await act(async () => resolvers[index]());
      }
      await settled();
      expect(maximumActive).toBe(2);
      expect(settingsApi.getToolVersions).toHaveBeenCalledTimes(9);
    } finally {
      view.unmount();
      await act(async () => {
        while (active > 0) resolvers.shift()?.();
      });
    }
  });

  it("stops unmounted queued work and ignores delayed results after a fresh mount", async () => {
    const oldResolvers: Array<() => void> = [];
    vi.mocked(settingsApi.getToolVersions).mockImplementation(
      (tools) =>
        new Promise((resolve) => {
          oldResolvers.push(() =>
            resolve([
              fixture(tools![0], { version: "9.9.9", latest_version: "9.9.9" }),
            ]),
          );
        }),
    );
    const first = render(<ToolVersionsSection />);
    await waitFor(() =>
      expect(settingsApi.getToolVersions).toHaveBeenCalledTimes(2),
    );
    first.unmount();
    vi.mocked(settingsApi.getToolVersions).mockImplementation(async (tools) =>
      tools!.map((name) =>
        fixture(name, { version: "2.0.0", latest_version: "2.0.0" }),
      ),
    );
    const second = render(<ToolVersionsSection />);
    try {
      await act(async () => Promise.resolve());
      expect(settingsApi.getToolVersions).toHaveBeenCalledTimes(2);
      await act(async () => oldResolvers.forEach((resolve) => resolve()));
      await settled();
      expect(settingsApi.getToolVersions).toHaveBeenCalledTimes(11);
      expect(row("Claude Code").getAllByText("2.0.0")).toHaveLength(2);
      expect(screen.queryByText("9.9.9")).not.toBeInTheDocument();
    } finally {
      second.unmount();
      await act(async () => oldResolvers.forEach((resolve) => resolve()));
    }
  });

  it("opens the reported HTTPS version source, exposes check time and rejects unsafe source links", async () => {
    vi.mocked(settingsApi.getToolVersions).mockImplementation(async (tools) =>
      tools!.map((name) =>
        fixture(
          name,
          name === "codex"
            ? { source_url: "javascript:alert(1)", checked_at: 1e30 }
            : {},
        ),
      ),
    );
    const open = vi.spyOn(settingsApi, "openExternal").mockResolvedValue();
    render(<ToolVersionsSection />);
    await settled();
    const source = row("Claude Code").getByRole("link", { name: "版本来源" });
    expect(source).toHaveAttribute(
      "href",
      "https://registry.npmjs.org/example/latest",
    );
    await userEvent.click(source);
    expect(open).toHaveBeenCalledWith(
      "https://registry.npmjs.org/example/latest",
    );
    expect(row("Codex").queryByRole("link")).not.toBeInTheDocument();
    expect(
      screen.getByRole("row", { name: "Claude Code" }).querySelector("time"),
    ).toHaveAttribute("datetime", new Date(1_789_689_600_000).toISOString());
  });
});
