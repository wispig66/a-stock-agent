# Claude Code MCP 官方文档（节选）
源: https://code.claude.com/docs/en/mcp
抓取日期: 2026-05-12

## MCP 是什么
Model Context Protocol 是开放标准，让 Claude 通过统一接口调用外部数据源和工具。Anthropic 维护着官方 MCP registry（https://api.anthropic.com/mcp-registry/）。

## 三种传输方式
- stdio（本地子进程，最常用）
- SSE（远程，server-sent events）
- HTTP（远程）

## 配置
项目级写在 `.mcp.json`，用户级写在 `~/.claude.json`，运行时用 `--mcp-config` 传入。

## A 股语境下的适用方向
- 行情接口：Tushare / akshare / 同花顺 iFinD / Wind 都可以包成 MCP server（社区已有相关 Python 包，自己 wrap 一层很容易）
- 通用 SQL MCP server 接到本地行情数据库
- 浏览器自动化 MCP（Playwright）抓东方财富/雪球/同花顺等无开放 API 的页面
- 推送通知 MCP（Bark/PushDeer/企业微信机器人）

## 关键限制
MCP server 本身需要常驻或被按需启动。stdio 模式每次新会话拉起新进程，**冷启动有几百毫秒到几秒开销**。SSE/HTTP 模式可常驻但需要自己运维。
