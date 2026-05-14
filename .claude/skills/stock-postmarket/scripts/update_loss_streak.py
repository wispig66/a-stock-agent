"""L4 盘后连亏状态更新 CLI。

由 SKILL.md Step 1.5 调用：
    uv run python .claude/skills/stock-postmarket/scripts/update_loss_streak.py

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
sys.path.insert(0, str(ROOT / "code"))

from lib import risk, loss_streak  # noqa: E402
from lib.holdings import read_holdings  # noqa: E402


def main() -> int:
    try:
        cfg = risk.load_risk_config()
        holdings = read_holdings()
        holdings_dicts = [
            {"code": h.code, "name": h.name, "cost": h.cost, "shares": h.shares}
            for h in holdings
        ]
        price_fn = risk.fetch_spot_price_fn()
        today = date.today()
        pnl_pct = loss_streak.compute_daily_pnl(
            holdings_dicts, price_fn=price_fn, total_capital=cfg["total_capital"]
        )
        state = loss_streak.load_state()
        new_history = loss_streak.update_pnl_history(
            state.get("daily_pnl", []), today, pnl_pct, cfg
        )
        streak = loss_streak.count_loss_streak(new_history, today)
        loss_streak.save_state({"daily_pnl": new_history})

        warn_threshold = int(cfg.get("loss_streak_warn_threshold", 2))
        is_loss_today = new_history[-1]["is_loss"] if new_history else False

        out = {
            "today_pnl_pct": pnl_pct,
            "is_loss_today": is_loss_today,
            "loss_streak": streak,
            "warn_active": streak >= warn_threshold,
            "recent_pnl": new_history[-5:],
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
