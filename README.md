# AI Coding

<p align="center">
  <img src="assets/branding/ai-coding-wordmark.png" alt="AI Coding" width="520" />
</p>

<p align="center">
  <strong>A desktop control center for AI coding tools, model providers, routing, memory, sessions, MCP, Skills, and prompts.</strong><br />
  Switch freely between Chinese domestic model providers and overseas model providers across Claude Code, Claude Desktop, Codex, Gemini CLI, OpenCode, OpenClaw, Hermes, Grok Build, Pi, and bincode.
</p>

AI Coding is not another chat wrapper. It is a practical desktop toolkit for developers who use multiple AI coding CLIs and want one clean place to manage providers, API keys, model routes, local proxy settings, MCP servers, Skills, prompts, session history, shared memory, and usage analytics.

If you often switch between DeepSeek, Kimi, Zhipu GLM, Qwen/Bailian, SiliconFlow, ModelScope, Claude, Gemini, OpenAI-compatible APIs, AWS Bedrock, or custom gateways, AI Coding is designed to make that workflow faster and less fragile.

## Screenshots

> All screenshots below are from the current AI Coding app.

| Cross-Tool Memory Sync | Categorized Provider Picker |
| --- | --- |
| ![Cross-tool memory sync](assets/screenshots/ai-coding-memory-sync.png) | ![Categorized provider picker](assets/screenshots/ai-coding-add-provider.png) |

| macOS-Style Dynamic Dock | Export Sessions To Markdown |
| --- | --- |
| ![macOS-style dynamic dock](assets/screenshots/ai-coding-dock.png) | ![Export sessions to Markdown](assets/screenshots/ai-coding-session-export.png) |

![Settings page](assets/screenshots/ai-coding-settings.png)

## Highlights

- **Chinese and overseas model switching**: providers are organized into Domestic Models, Custom Configuration, and Overseas Models, so users can quickly choose the right route for each AI coding tool.
- **10 supported AI coding apps**: Claude Code, Claude Desktop, Codex, Gemini CLI, OpenCode, OpenClaw, Hermes, Grok Build, Pi, and bincode. bincode is integrated as an OpenCode-compatible app with its own branding and switch entry.
- **Cross-tool memory synchronization**: push Claude Code memory from `~/.claude/CLAUDE.md` to Codex, Gemini, Hermes, OpenCode, and OpenClaw with one click.
- **Session management and Markdown export**: search complete local conversations with exact or fuzzy matching, filter by tool or search all agents, keep pinned and archived sessions, rename sessions, restore them in the matching CLI, and export a conversation as a `.md` file.
- **CLI version checks**: compare installed CLI versions with their published versions from Settings, including Claude Code, Codex, Gemini CLI, OpenCode, OpenClaw, Hermes, Grok Build, and Pi.
- **Apple-inspired desktop UI**: dark mode by default, warm Claude-style orange accents, large rounded corners, glass effects, and a macOS Dock-style bottom navigation with hover magnification and app labels.
- **Unified MCP management**: add, edit, validate, import, and sync MCP servers across multiple coding apps instead of maintaining separate config files manually.
- **Unified Skills management**: install, import, export, update, discover, and sync Skills. AI Coding can use the unified `~/.agents/skills` layout for cleaner cross-tool skill sharing.
- **Prompt management**: centralize prompts for different tools, import existing prompt files, edit them in one place, and sync them back to target apps.
- **Local proxy and routing**: configure local routing, model mapping, provider health checks, automatic failover, circuit breaker behavior, and request transformation for mixed provider workflows.
- **Usage analytics and cost insight**: parse local sessions and proxy request logs to track requests, token usage, model/provider distribution, trends, and estimated cost.
- **Backup and migration**: import/export app configuration, sync through WebDAV/S3, configure global proxy settings, and migrate between machines more easily.

## Supported Apps

| App | What AI Coding Manages |
| --- | --- |
| Claude Code | CLI providers, memory, prompts, MCP, sessions, usage |
| Claude Desktop | Desktop provider routing and configuration |
| Codex | Providers, prompts, MCP, memory target, sessions, usage |
| Gemini CLI | Providers, prompts, MCP, memory target, sessions, usage |
| OpenCode | OpenCode-compatible providers, MCP, Skills, memory target |
| OpenClaw | Providers, tools, default agents, memory target |
| Hermes | Hermes memory, Skills, MCP, and provider-related config |
| Grok Build | xAI provider configuration, OAuth and sessions (hidden by default) |
| Pi | Providers, prompts, sessions and usage (hidden by default) |
| bincode | OpenCode-compatible app entry and model switching |

## Why It Exists

AI coding tools are powerful, but their configuration is scattered: every CLI has its own provider file, memory file, MCP format, prompt location, and session storage. AI Coding brings these moving parts into one desktop app so switching models, sharing memory, managing Skills, and exporting sessions becomes a normal workflow instead of a collection of manual edits.

## Open-Source Build

This repository is prepared as an open-source build for self-hosting, learning, and secondary development.

- The activation-code gate has been removed from this open-source version.
- You do not need an activation code to run this repository build.
- Commercial or private builds can reintroduce licensing separately if needed.
- Some internal storage paths may keep historical names for compatibility with existing user data.
- OAuth-related features require your own Client ID / Client Secret through environment variables. Do not commit secrets to the repository.

## Download

### macOS

Download the current Apple Silicon build:

[Download AI Coding v3.22.5 for macOS Apple Silicon](https://github.com/bincode-model/ai-coding/releases/download/v3.22.5/AI-Coding-v3.22.5-macOS-aarch64.dmg)

Open the `.dmg` file, then drag **AI Coding** into the Applications folder.

#### macOS says the app "is damaged" or won't open

The app is not notarized with Apple yet, so Gatekeeper may block the first launch after download. The file itself is fine. Fix it either way below:

- Open **System Settings → Privacy & Security**, scroll down to the AI Coding message, and click **Open Anyway**; or
- Run this once in Terminal after copying the app into Applications, then open it normally:

```bash
xattr -cr "/Applications/AI Coding.app"
```

> All installers and SHA-256 checksums are available on the [v3.22.5 release page](https://github.com/bincode-model/ai-coding/releases/tag/v3.22.5). The repository also keeps the Mac `.dmg` under `downloads/`.

The macOS build output is usually located at:

```text
src-tauri/target/release/bundle/macos/AI Coding.app
src-tauri/target/release/bundle/dmg/
```

### Windows (64-bit)

- [Download EXE installer](https://github.com/bincode-model/ai-coding/releases/download/v3.22.5/AI-Coding-v3.22.5-windows-x64-setup.exe) — recommended for normal installation.
- [Download MSI installer](https://github.com/bincode-model/ai-coding/releases/download/v3.22.5/AI-Coding-v3.22.5-windows-x64.msi) — alternative Windows Installer package.

Use either installer on Windows 10/11 x64. Both install for the current user. The EXE installer offers Simplified Chinese and English. Microsoft Edge WebView2 is required; the installer includes its bootstrapper and may need an Internet connection if the runtime is missing.

These installers do not have a commercial code-signing certificate, so Windows may show an unknown-publisher or SmartScreen prompt. Download from this repository's release page and compare the file with `WINDOWS-SHA256SUMS` when needed.

Session search supports exact and fuzzy matching on Windows too. To resume a session, copy its resume command and run it in your terminal with the corresponding CLI installed.

## Development

### Requirements

- Node.js 20+
- pnpm
- Rust
- Tauri development environment

### Run In Development

```bash
pnpm install
pnpm tauri dev
```

### Build Desktop App

```bash
pnpm tauri build
```

### Windows Release Build

Build on Windows with the Tauri prerequisites (Visual Studio C++ Build Tools, Rust MSVC and WebView2):

```powershell
pnpm install --frozen-lockfile
pnpm tauri build --target x86_64-pc-windows-msvc --bundles nsis,msi
```

The `Windows build and release` GitHub Actions workflow checks frontend types, formatting and tests, checks/lints/tests Rust on Windows, builds both installers, and validates MSI installation, process startup and uninstall data preservation. It publishes the same-version Mac and Windows packages after these checks pass. A new source release needs a new numeric version in `package.json`, `src-tauri/Cargo.toml`, `src-tauri/Cargo.lock` and `src-tauri/tauri.conf.json`, a matching Mac package/checksum in `downloads/`, and release notes in `docs/release-notes/`. Published tags are never moved to a different commit.

## Project Structure

```text
src/          React + TypeScript frontend
src-tauri/    Rust + Tauri backend
assets/       Branding assets and product screenshots
```

## License

MIT
