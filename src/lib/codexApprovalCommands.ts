/**
 * Codex reads approval_policy from the root of its user config.toml. A CLI
 * flag only affects one invocation, so these commands update the user config
 * while leaving the existing sandbox policy and provider settings untouched.
 */
export const CODEX_NO_APPROVAL_UNIX_COMMAND = `python3 - <<'PY'
from pathlib import Path
from datetime import datetime
import os
import re
import shutil
import stat
import tempfile

folder = Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()
folder.mkdir(parents=True, exist_ok=True)
config = (folder / "config.toml").resolve()
if config.exists():
    with config.open("r", encoding="utf-8", newline="") as source:
        original = source.read()
else:
    original = ""
table = re.search(r"(?m)^[ \\t]*\\[", original)
split = table.start() if table else len(original)
head, tail = original[:split], original[split:]
key = r"(?m)^[ \\t]*(?:approval_policy|\\\"approval_policy\\\"|'approval_policy')[ \\t]*=[^\\r\\n]*(?:\\r?\\n|$)"
head = re.sub(key, "", head)
newline = "\\r\\n" if "\\r\\n" in original else "\\n"
updated = 'approval_policy = "never"' + newline + head + tail
if updated != original:
    if config.exists():
        backup = config.with_name(config.name + ".bak." + datetime.now().strftime("%Y%m%d%H%M%S%f"))
        shutil.copy2(config, backup)
    mode = stat.S_IMODE(config.stat().st_mode) if config.exists() else 0o600
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=config.parent, prefix=".codex-config-", delete=False) as output:
        output.write(updated)
        temporary = Path(output.name)
    os.chmod(temporary, mode)
    os.replace(temporary, config)
print("Codex approval_policy = never:", config)
PY`;

export const CODEX_NO_APPROVAL_WINDOWS_COMMAND = `$ErrorActionPreference = 'Stop'
$dir = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
[IO.Directory]::CreateDirectory($dir) | Out-Null
$file = Join-Path $dir 'config.toml'
$original = if (Test-Path -LiteralPath $file) { [IO.File]::ReadAllText($file) } else { '' }
$table = [regex]::Match($original, '(?m)^[ \\t]*\\[')
$split = if ($table.Success) { $table.Index } else { $original.Length }
$head = $original.Substring(0, $split)
$tail = $original.Substring($split)
$head = [regex]::Replace($head, '(?m)^[ \\t]*(?:approval_policy|"approval_policy"|''approval_policy'')[ \\t]*=[^\\r\\n]*(?:\\r?\\n|$)', '')
$newline = if ($original.Contains("\`r\`n")) { "\`r\`n" } else { "\`n" }
$updated = 'approval_policy = "never"' + $newline + $head + $tail
if ($updated -cne $original) {
  $temporary = Join-Path $dir ([IO.Path]::GetRandomFileName())
  [IO.File]::WriteAllText($temporary, $updated, [Text.UTF8Encoding]::new($false))
  if (Test-Path -LiteralPath $file) {
    $backup = $file + '.bak.' + [DateTime]::UtcNow.ToString('yyyyMMddHHmmssfff')
    [IO.File]::Replace($temporary, $file, $backup)
  } else {
    [IO.File]::Move($temporary, $file)
  }
}
Write-Host "Codex approval_policy = never: $file"`;
