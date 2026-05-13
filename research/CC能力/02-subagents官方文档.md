# Claude Code Subagents 官方文档（节选）
源: https://code.claude.com/docs/en/sub-agents
抓取日期: 2026-05-12

## 定义
Subagent 是有独立 context、独立 system prompt、独立工具集和权限的子 AI。主 Claude 把任务委派给子 agent，子 agent 在自己上下文里做完，只返回摘要。

## 价值
- 保护主上下文（探索/检索/日志类活动隔离）
- 限制工具权限
- 用户级 subagent 跨项目复用
- 把任务路由到更便宜的模型（如 Haiku）以省钱

## 调用方式
主 Claude 根据 subagent 的 description 自动委派。Subagent 在**同一 session 内**运行。

## 与"背景 agents"和"agent teams"的关系
- Subagents 在单 session 内
- Background agents：多 session 并行独立跑，可在一处监控
- Agent teams：多 session 互相通信

## 用于 A 股短线的含义
可以建立专门角色：
- 「数据采集 agent」只有 WebFetch / Bash / MCP 行情工具
- 「龙虎榜分析 agent」专责复盘
- 「风控 agent」对所有信号做最后一道审核

这些可在主 agent 编排下并行跑，节省主上下文。
