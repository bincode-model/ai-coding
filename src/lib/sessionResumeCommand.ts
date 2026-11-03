export function formatCopiedResumeCommand(
  prepared: { command: string; cwd?: string | null },
  windows: boolean,
): string {
  const { command, cwd } = prepared;
  if (!cwd) return command;
  if (cwd.includes("\0")) {
    throw new Error(
      "Session working directory must not contain a NUL character",
    );
  }

  if (windows) {
    // A terminating location error stops this block before the resume command.
    // PowerShell also treats typographic single quotes as string delimiters.
    const quotedCwd = cwd.replace(
      /['\u2018-\u201b]/g,
      (quote) => quote + quote,
    );
    return `& { Set-Location -LiteralPath '${quotedCwd}' -ErrorAction Stop; ${command}\n}`;
  }

  const quotedCwd = cwd.replace(/'/g, "'\\''");
  return `cd -- '${quotedCwd}' && ${command}`;
}
