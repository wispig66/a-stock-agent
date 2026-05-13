# Claude Code Headless 模式 + Agent SDK
源: https://code.claude.com/docs/en/headless
     https://code.claude.com/docs/en/agent-sdk/overview
抓取日期: 2026-05-12

## Headless / -p 模式
`claude -p "你的指令"` 即非交互模式。所有 CLI flag 都可用。
- `--bare` 启动更快，跳过 hook/skill/plugin/MCP/CLAUDE.md 自动发现
- `--output-format json` 结构化输出（带 session_id、total_cost_usd）
- `--output-format stream-json` 流式 NDJSON
- `--json-schema` 约束输出 schema
- `--allowedTools "Read,Edit,Bash"` 预批准工具
- `--continue` / `--resume <session_id>` 续会话
- `--mcp-config` 加载 MCP 服务器
- `--append-system-prompt` 追加 system prompt

stdin 上限：v2.1.128 起 10MB。

**这是从 cron 跑 CC 的官方方式**：
```bash
30 8 * * 1-5 /usr/local/bin/claude --bare -p "$(cat /path/to/premarket-prompt.txt)" \
  --allowedTools "Read,Bash,WebFetch" \
  --output-format json > /tmp/premarket-$(date +%F).json
```

## Agent SDK
Python (`claude-agent-sdk`) 和 TypeScript (`@anthropic-ai/claude-agent-sdk`)。
Opus 4.7 需要 SDK v0.2.111+。

提供和 CLI 同样的工具/loop/上下文管理，但能写程序逻辑。
- 程序化决定提示词、轮询
- 程序化回调 hook（PreToolUse、PostToolUse、Stop、SessionStart 等）
- 程序化 subagent 定义
- 程序化 session resume

## SDK vs CLI vs Client SDK 选择
| 场景 | 选择 |
|---|---|
| 交互式开发 | Claude Code CLI |
| CI/CD | Agent SDK |
| 自定义应用、生产自动化 | Agent SDK |
| 一次性任务 | CLI |
| 需要完全控制 tool loop | Client SDK |

## 对 A 股自动化的启示
- 盘前盘后跑脚本：CLI `-p` 就够，写 shell + cron
- 需要循环、状态机、复杂调度：Agent SDK
- 极致低延迟（不可能用到）：直接用 Anthropic Client SDK 自管 tool loop，省一层
