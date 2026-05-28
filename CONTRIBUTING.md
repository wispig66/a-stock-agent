# 贡献指南

感谢你关注这个项目。A Stock Agent 是一个本地优先的 A 股研究和通知工作流，不是券商接口，也不是自动交易系统。任何改动都应保持这个边界：不自动下单、不隐藏网络副作用、不承诺收益。

## 开发环境

```bash
uv sync --group dev
cp .env.example .env
sqlite3 data/daily.db < stock_codex/schema/init_db.sql
uv run --no-sync pytest -q
```

不要提交 `.env`、`data/`、`logs/`、`holdings.yaml`、`risk_config.yaml`、`risk_state.yaml`。

## PR 要求

- 保持改动范围清晰。无关修复请拆成不同提交。
- 优先沿用现有模块和模式，不要为小改动引入新抽象。
- 行为变更需要补测试，尤其是 Codex skill contract、IM gateway、通知、风险、数据源解析和卡片校验。
- 不要提交生成数据、私有持仓、chat id、bot token 或日志。
- 金融相关措辞必须客观，不要写成收益承诺或荐股宣传。

## 代码风格

- Python 目标版本：3.11-3.12。
- 提交前至少运行：

```bash
git diff --check
uv run --no-sync pytest -q
```

- 注释只解释非显而易见的约束、边界或失败模式。

## 数据源变更

A 股数据源经常限流、改字段或受代理规则影响。修改 fetcher 时：

- 为观察到的 schema 增加测试或 fixture。
- 尽量保留降级路径，不要因为单个源失败阻断整个工作流。
- 不要写死私有 cookie、账号相关 header 或本机代理配置。

## 安全问题

如果发现 token 泄露、日志泄密或凭证处理问题，请按 [SECURITY.md](SECURITY.md) 处理，不要在公开 issue 里贴敏感信息。
