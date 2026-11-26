import { afterEach, describe, expect, it } from "vitest";
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { formatCopiedResumeCommand } from "@/lib/sessionResumeCommand";

const temporaryRoots: string[] = [];
afterEach(() => {
  for (const root of temporaryRoots.splice(0))
    rmSync(root, { recursive: true, force: true });
});

function fixture() {
  const root = mkdtempSync(join(tmpdir(), "gg-copy-command-"));
  temporaryRoots.push(root);
  const originalCwd = join(root, "another-project");
  const cwd = join(
    root,
    "project 'quoted' $HOME `echo unexpected` ; & [x]\nnext-line",
  );
  mkdirSync(originalCwd);
  mkdirSync(cwd);
  const wrapper = join(root, "claude-gg");
  writeFileSync(wrapper, '#!/bin/sh\nprintf \'%s\\000\' "$PWD" "$@"\n');
  chmodSync(wrapper, 0o700);
  const uuid = "ac7743b2-30b4-4b81-aee9-ff2cda7ea849";
  const command = `'${wrapper}' --resume '${uuid}'`;
  return { root, originalCwd, cwd, wrapper, uuid, command };
}

describe("copied session resume command", () => {
  it.each([undefined, null, ""])(
    "preserves the prepared command when cwd is %s",
    (cwd) => {
      const command = "  '/safe/claude-gg' --resume 'selected-session'  ";
      expect(formatCopiedResumeCommand({ command, cwd }, false)).toBe(command);
      expect(formatCopiedResumeCommand({ command, cwd }, true)).toBe(command);
    },
  );

  it("executes the exact prepared GG wrapper and UUID in the original project from another shell directory", () => {
    const f = fixture();
    const result = spawnSync(
      "/bin/sh",
      ["-c", formatCopiedResumeCommand(f, false)],
      {
        cwd: f.originalCwd,
        env: { HOME: f.root, PATH: "/usr/bin:/bin" },
        encoding: "utf8",
      },
    );
    expect(result.status).toBe(0);
    expect(result.stderr).toBe("");
    expect(result.stdout.split("\0")).toEqual([f.cwd, "--resume", f.uuid, ""]);
  });

  it("does not execute the GG wrapper if the original working directory is gone", () => {
    const f = fixture();
    const result = spawnSync(
      "/bin/sh",
      [
        "-c",
        formatCopiedResumeCommand({ ...f, cwd: join(f.root, "gone") }, false),
      ],
      {
        cwd: f.originalCwd,
        env: { HOME: f.root, PATH: "/usr/bin:/bin" },
        encoding: "utf8",
      },
    );
    expect(result.status).not.toBe(0);
    expect(result.stdout).toBe("");
  });

  it("keeps the PowerShell directory literal and preserves the original wrapper command", () => {
    const command =
      "& 'C:\\GG\\bin\\claude-gg.cmd' --resume 'selected-session'";
    const cwd = "C:\\Users\\O'Brien\\work $HOME `echo x` [a]";
    const result = formatCopiedResumeCommand({ command, cwd }, true);
    expect(result).toContain(
      "Set-Location -LiteralPath 'C:\\Users\\O''Brien\\work $HOME `echo x` [a]' -ErrorAction Stop",
    );
    expect(result).toContain(command);
    expect(result).not.toContain("CLAUDE_CONFIG_DIR");
  });

  it.each([false, true])(
    "rejects a NUL directory before producing a command (Windows: %s)",
    (windows) => {
      expect(() =>
        formatCopiedResumeCommand(
          { command: "should-not-run", cwd: "bad\0path" },
          windows,
        ),
      ).toThrow();
    },
  );
});
