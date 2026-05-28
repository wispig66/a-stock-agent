# 安全策略

## 支持范围

当前只支持 `main` 分支的最新版本。

## 漏洞报告

请不要在公开 issue 中粘贴以下内容：

- Telegram bot token
- 飞书/Lark app secret
- 私人 chat id
- 持仓、账户金额或交易日志
- SQLite 数据库或生成的运行态文件
- 含凭证的日志

如果仓库提供了私密联系方式，请优先私下报告。若没有私密渠道，可以开一个最小公开 issue，只说明“需要私下报告安全问题”，不要包含敏感细节。

## 凭证处理

项目默认把凭证放在 `.env` 这类本地忽略文件中。公开仓库或推送前建议运行：

```bash
git status --short
git grep -n -I -E 'TG_BOT_TOKEN=[0-9]{6,}:[A-Za-z0-9_-]{20,}|FEISHU_APP_SECRET=[A-Za-z0-9_-]{20,}|Authorization:[[:space:]]+Bearer[[:space:]]+[A-Za-z0-9._-]{20,}' -- . ':!uv.lock' ':!README.md' ':!SECURITY.md'
```

如果 token 曾经出现在日志、终端输出、截图、聊天记录或提交历史里，请直接轮换。

## 本地运行态

以下路径应始终保持私有：

- `.env`
- `data/`
- `logs/`
- `holdings.yaml`
- `risk_config.yaml`
- `risk_state.yaml`

这些文件可能包含个人交易数据、chat id、账号相关状态或运行日志。
