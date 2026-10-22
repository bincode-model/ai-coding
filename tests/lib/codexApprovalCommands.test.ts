import { execFileSync } from "node:child_process";
import {
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { parse } from "smol-toml";
import {
  CODEX_NO_APPROVAL_UNIX_COMMAND,
  CODEX_NO_APPROVAL_WINDOWS_COMMAND,
} from "@/lib/codexApprovalCommands";

const directories: string[] = [];
const runUnixCommand = (directory: string) =>
  execFileSync("/bin/sh", ["-c", CODEX_NO_APPROVAL_UNIX_COMMAND], {
    env: { ...process.env, CODEX_HOME: directory },
    encoding: "utf-8",
  });

afterEach(() => {
  for (const directory of directories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

describe("Codex global no-approval commands", () => {
  it("updates only the root approval policy, backs up existing config, and is idempotent", () => {
    const directory = mkdtempSync(join(tmpdir(), "ai-coding-codex-approval-"));
    directories.push(directory);
    const configPath = join(directory, "config.toml");
    const original = `model = "gpt-5"
approval_policy = "on-request"
sandbox_mode = "workspace-write"

[model_providers.custom]
name = "Private provider"
approval_policy = "provider-specific"
`;
    writeFileSync(configPath, original);

    runUnixCommand(directory);
    const result = readFileSync(configPath, "utf-8");
    const parsed = parse(result);
    expect(parsed.approval_policy).toBe("never");
    expect(parsed.sandbox_mode).toBe("workspace-write");
    expect(parsed.model_providers).toEqual({
      custom: {
        name: "Private provider",
        approval_policy: "provider-specific",
      },
    });
    expect(
      readdirSync(directory).filter((file) =>
        file.startsWith("config.toml.bak."),
      ),
    ).toHaveLength(1);

    runUnixCommand(directory);
    expect(readFileSync(configPath, "utf-8")).toBe(result);
    expect(
      readdirSync(directory).filter((file) =>
        file.startsWith("config.toml.bak."),
      ),
    ).toHaveLength(1);
  });

  it("creates a new config in a custom CODEX_HOME without changing sandbox mode", () => {
    const directory = mkdtempSync(
      join(tmpdir(), "ai-coding-codex-approval-new-"),
    );
    directories.push(directory);
    runUnixCommand(directory);
    expect(
      parse(readFileSync(join(directory, "config.toml"), "utf-8")),
    ).toEqual({
      approval_policy: "never",
    });
  });

  it("replaces a quoted root key before the first table without duplicating it", () => {
    const directory = mkdtempSync(
      join(tmpdir(), "ai-coding-codex-approval-quoted-"),
    );
    directories.push(directory);
    const configPath = join(directory, "config.toml");
    writeFileSync(
      configPath,
      `'approval_policy' = "on-request"\r\n[projects."/tmp/example"]\r\ntrust_level = "trusted"\r\n`,
    );
    runUnixCommand(directory);
    const text = readFileSync(configPath, "utf-8");
    expect(parse(text).approval_policy).toBe("never");
    expect(text).toContain("\r\n[projects.");
    expect(text.match(/approval_policy/g)).toHaveLength(1);
  });

  it("provides a Windows PowerShell equivalent without bypassing the sandbox", () => {
    expect(CODEX_NO_APPROVAL_WINDOWS_COMMAND).toContain("$env:CODEX_HOME");
    expect(CODEX_NO_APPROVAL_WINDOWS_COMMAND).toContain("[IO.File]::Replace");
    expect(CODEX_NO_APPROVAL_WINDOWS_COMMAND).toContain(
      'approval_policy = "never"',
    );
    expect(CODEX_NO_APPROVAL_WINDOWS_COMMAND).not.toContain(
      "danger-full-access",
    );
    expect(CODEX_NO_APPROVAL_UNIX_COMMAND).not.toContain("danger-full-access");
  });
});
