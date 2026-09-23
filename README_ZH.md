# AI Coding

<p align="center">
  <img src="assets/branding/ai-coding-wordmark.png" alt="AI Coding" width="520" />
</p>

<p align="center">
  <strong>一个为 AI 编程工具而生的模型供应商切换中枢。</strong><br />
  统一管理 Claude Code、Claude Desktop、Codex、Gemini CLI、OpenCode、OpenClaw、Hermes、Grok Build、Pi、bincode，让国产模型和海外模型都能顺手切换。
</p>

AI Coding 不是普通聊天壳，而是给 AI 编程用户准备的桌面控制台：把供应商、模型、API Key、代理路由、MCP、Skills、提示词、会话记录、记忆同步和用量统计放到一个地方管理。它适合经常在国产模型和国外模型之间切换，也适合同时使用多个 AI 编程工具的开发者。

## 界面预览

> 以下截图均来自当前 AI Coding 版本，不使用第三方示例图。

| 跨工具记忆同步 | 添加供应商分类 |
| --- | --- |
| ![跨工具记忆同步](assets/screenshots/ai-coding-memory-sync.png) | ![添加供应商分类](assets/screenshots/ai-coding-add-provider.png) |

| macOS Dock 风格导航 | 会话导出 Markdown |
| --- | --- |
| ![macOS Dock 风格导航](assets/screenshots/ai-coding-dock.png) | ![会话导出 Markdown](assets/screenshots/ai-coding-session-export.png) |

![设置页面](assets/screenshots/ai-coding-settings.png)

## 核心亮点

- **国产模型 / 海外模型自由切换**：添加供应商时按“国模 / 自定义配置 / 美模（海外模型）”分区，内置 DeepSeek、Kimi、智谱 GLM、千帆、百炼、SiliconFlow、ModelScope、Claude、Gemini、OpenAI 兼容服务、AWS Bedrock 等常见选择。
- **同时管理 10 个 AI 编程 App**：支持 Claude Code、Claude Desktop、Codex、Gemini CLI、OpenCode、OpenClaw、Hermes、Grok Build、Pi 和 bincode；其中 bincode 按 OpenCode 兼容方式接入，可独立显示和切换。
- **跨工具记忆同步**：一键把 Claude Code 的 `CLAUDE.md` 记忆同步到 Codex、Gemini、Hermes、OpenCode、OpenClaw 等工具，不用每个工具手动复制一遍。
- **会话管理与 Markdown 导出**：按 Agent 查看会话，精准或模糊搜索完整消息内容，也可主动合并检索；支持置顶、归档、自定义命名、恢复指定会话和导出 `.md` 文件。
- **CLI 版本检查**：在设置中查看各工具的本地版本和已发布版本，涵盖 Claude Code、Codex、Gemini CLI、OpenCode、OpenClaw、Hermes、Grok Build、Pi 等。
- **Apple 风格深色界面**：默认深色模式，暖橙主色，底部 macOS Dock 风格导航，图标支持悬停动态放大和名称提示，整体更接近桌面 App 的使用体验。
- **统一 MCP 管理**：管理 MCP Server，并同步到 Claude、Codex、Gemini、OpenCode、Hermes 等工具，减少多份配置重复维护。
- **统一 Skills 管理**：支持 Skills 导入、安装、同步、备份和发现，可使用统一目录 `~/.agents/skills` 管理技能脚本。
- **提示词管理**：集中维护不同工具的提示词配置，支持导入已有提示词文件并同步到目标应用。
- **代理与路由能力**：内置本地代理、路由切换、健康检查、自动故障转移、熔断和模型映射能力，适合把不同模型供应商接入同一套 AI 编程工作流。
- **用量统计与成本分析**：解析本地会话和代理请求日志，按模型、供应商、时间趋势统计 token、请求量和费用，帮助你知道钱花到哪里了。
- **配置备份与迁移**：支持导入/导出配置、WebDAV/S3 同步、全局代理、语言与主题设置，方便多设备迁移或团队共享基础配置。

## 支持的应用

| 应用 | 说明 |
| --- | --- |
| Claude Code | Claude CLI 编程工具 |
| Claude Desktop | Claude 桌面端配置管理 |
| Codex | Codex CLI / Codex 相关配置 |
| Gemini CLI | Gemini 命令行工具 |
| OpenCode | OpenCode 协议与配置 |
| OpenClaw | OpenClaw 配置、工具与默认 Agents |
| Hermes | Hermes Agent 记忆与 Skills |
| Grok Build | xAI Grok Build 配置与官方登录（默认隐藏，可在设置中开启） |
| Pi | Pi 编程助手供应商、提示词与用量（默认隐藏，可在设置中开启） |
| bincode | 基于 OpenCode 兼容协议接入的独立应用入口 |

## 适合谁用

- 你经常在国产模型和国外模型之间切换，不想每个 CLI 都重复配置。
- 你同时使用 Claude Code、Codex、Gemini、OpenCode 等多个 AI 编程工具。
- 你想把 MCP、Skills、提示词和记忆沉淀成一套可复用的个人工作台。
- 你想把重要 AI 会话导出成 Markdown，方便沉淀成文档或发给朋友。
- 你希望本地代理、模型路由、失败切换和用量统计有一个统一入口。

## 开源版说明

这个仓库整理的是适合公开发布、二次开发和自行构建的开源版本。

- 当前开源版已经移除激活码入口和前后端激活校验，不需要输入激活码即可使用。
- 商业或私有版本如果需要授权体系，可以在自己的分支中重新接入。
- 为了兼容历史用户配置，部分底层目录仍会保留旧路径命名，这是迁移兼容设计，不影响产品名称。
- 如需使用某些 OAuth 能力，请通过环境变量配置自己的 Client ID / Secret，不要把密钥提交到仓库。

## 下载安装

### macOS（Apple Silicon）

[下载 AI Coding v3.22.5 macOS 安装包](https://github.com/bincode-model/ai-coding/releases/download/v3.22.5/AI-Coding-v3.22.5-macOS-aarch64.dmg)

打开 `.dmg` 文件，把 **AI Coding** 拖进「应用程序」文件夹即可。

#### 提示"已损坏，无法打开"怎么办

应用暂未经过 Apple 公证，首次打开可能被 macOS 拦截，提示"已损坏"或"无法验证开发者"。**文件本身没有问题**，按下面任一方式处理即可：

- 打开「系统设置 → 隐私与安全性」，往下滚动找到 AI Coding 的提示，点「仍要打开」；或
- 把应用拖进「应用程序」后，在终端执行一次下面的命令，然后正常打开：

```bash
xattr -cr "/Applications/AI Coding.app"
```

### Windows（64 位）

- [下载 EXE 安装包](https://github.com/bincode-model/ai-coding/releases/download/v3.22.5/AI-Coding-v3.22.5-windows-x64-setup.exe)：日常安装推荐。
- [下载 MSI 安装包](https://github.com/bincode-model/ai-coding/releases/download/v3.22.5/AI-Coding-v3.22.5-windows-x64.msi)：Windows Installer 格式。

适用于 Windows 10/11 x64。应用需要 Microsoft Edge WebView2；安装包包含引导程序，缺少运行环境时可能联网下载。安装包未使用商业代码签名证书，系统可能显示“未知发布者”或 SmartScreen 提示。安装包和 SHA-256 校验文件见 [v3.22.5 发布页](https://github.com/bincode-model/ai-coding/releases/tag/v3.22.5)。Windows 上恢复会话会给出对应 CLI 的命令，需要在终端执行。

## 本地开发

### 环境要求

- Node.js 20+
- pnpm
- Rust
- Tauri 开发环境

### 启动开发版

```bash
pnpm install
pnpm tauri dev
```

### 打包桌面应用

```bash
pnpm tauri build
```

macOS 构建产物通常位于：

```text
src-tauri/target/release/bundle/macos/AI Coding.app
src-tauri/target/release/bundle/dmg/
```

## 项目结构

```text
src/          React + TypeScript 前端
src-tauri/    Rust + Tauri 后端
assets/       品牌资源与产品截图
```

## License

MIT
