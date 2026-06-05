# Codex Automations Runbook（已迁移）

本文档已迁移到 [docs/automations.md](automations.md)，支持多 Agent 自动化调度。

Codex 仍是默认 agent。如需继续使用 Codex，无需额外操作：

```bash
bash scripts/install_automations.sh install            # 等价于旧 install_codex_automations.sh
bash scripts/install_automations.sh install --agent codex --dry-run
```

`--dry-run` 默认写临时目录，不会覆盖真实 `~/.codex/automations/`。如需检查生成的 TOML：

```bash
bash scripts/install_automations.sh install --agent codex --dry-run --output-dir /tmp/stock-codex-automations
```
