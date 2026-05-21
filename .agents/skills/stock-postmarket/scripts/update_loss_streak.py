"""L4 盘后连亏状态更新 CLI。

由 SKILL.md Step 1.5 调用：
    uv run python .agents/skills/stock-postmarket/scripts/update_loss_streak.py

输出（stdout，单行 JSON）：
    {
      "today_pnl_pct": -3.1,
      "is_loss_today": true,
      "loss_streak": 2,
      "warn_active": true,
      "recent_pnl": [...]
    }

失败兜底：异常返回保守默认（warn_active=false），L4 流程继续。
"""
from __future__ import annotations
import json
import sys
import traceback
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]

from stock_codex.domain import risk, loss_streak  # noqa: E402
from stock_codex.domain.holdings import read_holdings  # noqa: E402


def main() -> int:
    try:
        cfg = risk.load_risk_config()
        holdings = read_holdings()
        holdings_dicts = [
            {"code": h.code, "name": h.name, "cost": h.cost, "shares": h.shares}
            for h in holdings
        ]
        today = date.today()

        # 空仓直接 0 pnl，不需要行情源
        if not holdings_dicts:
            prices, price_source = {}, "skip_empty_holdings"
            price_fn = lambda c: None
            pnl_pct = 0.0
            price_failed = False
        else:
            codes = [h["code"] for h in holdings_dicts]
            prices, price_source = risk.fetch_prices_for_codes(codes)
            price_failed = (price_source == "none")
            price_fn = lambda c: prices.get(str(c).zfill(6))
            pnl_pct = loss_streak.compute_daily_pnl(
                holdings_dicts, price_fn=price_fn, total_capital=cfg["total_capital"]
            )

        state = loss_streak.load_state()
        if price_failed:
            # 行情全部失败：保留昨日 history 不写今日，避免按 cost 兜底误判为"0 浮盈"
            new_history = state.get("daily_pnl", [])
            streak = loss_streak.count_loss_streak(new_history, today)
            is_loss_today = False
        else:
            new_history = loss_streak.update_pnl_history(
                state.get("daily_pnl", []), today, pnl_pct, cfg
            )
            streak = loss_streak.count_loss_streak(new_history, today)
            loss_streak.save_state({"daily_pnl": new_history})
            is_loss_today = new_history[-1]["is_loss"] if new_history else False

        warn_threshold = int(cfg.get("loss_streak_warn_threshold", 2))

        out = {
            "today_pnl_pct": pnl_pct,
            "is_loss_today": is_loss_today,
            "loss_streak": streak,
            "warn_active": (not price_failed) and streak >= warn_threshold,
            "recent_pnl": new_history[-5:],
            "price_source": price_source,
            "price_source_failed": price_failed,
        }
        print(json.dumps(out, ensure_ascii=False))
        return 0
    except Exception as e:
        print(f"[update_loss_streak] 异常: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        fallback = {
            "today_pnl_pct": 0.0,
            "is_loss_today": False,
            "loss_streak": 0,
            "warn_active": False,
            "recent_pnl": [],
        }
        print(json.dumps(fallback, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
