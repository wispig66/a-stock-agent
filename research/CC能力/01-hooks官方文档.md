# Claude Code Hooks 官方文档（节选）
源: https://code.claude.com/docs/en/hooks
抓取日期: 2026-05-12

## 完整事件列表

### 会话生命周期
- SessionStart / Setup / SessionEnd

### 每轮对话
- UserPromptSubmit / UserPromptExpansion / Stop / StopFailure

### Agent 工具循环
- PreToolUse（可阻断工具调用）
- PostToolUse / PostToolUseFailure / PostToolBatch
- PermissionRequest / PermissionDenied

### Subagents & Tasks
- SubagentStart / SubagentStop / TaskCreated / TaskCompleted / TeammateIdle

### 文件 & 配置
- FileChanged（文件被改时触发）
- ConfigChange / InstructionsLoaded / CwdChanged

### 上下文 & 清理
- PreCompact / PostCompact / Notification

### MCP
- Elicitation / ElicitationResult

### Worktree
- WorktreeCreate / WorktreeRemove

## 关键能力：异步与回唤

Command hook 支持两种异步模式：
- `async: true` 后台跑、不阻塞 Claude
- `asyncRewake: true` 后台跑，退出码为 2 时把 stdout/stderr 作为 system reminder 喂回给 Claude，让 Claude 主动反应

这两个特性是"触发式信号"的工程基础：CC 在做别的事，后台 hook 一旦检测到行情/盘口异动就唤醒主 agent。

## Hook 处理器类型
command / http / mcp_tool / prompt / agent

## 关键判断
Hooks 本身**不提供 cron/定时调度**。它响应的是"CC 会话内发生的事件"。
要做定时任务，必须依赖**外部调度器（系统 cron / launchd）+ Claude Code 的 headless / `-p` 模式**。
