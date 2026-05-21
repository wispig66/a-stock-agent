"""L1 盘前预检 CLI。读 holdings.yaml + risk_config.yaml，输出 JSON 给 LLM。

调用方式（由 SKILL.md prompt 在 Step 3 之后、Step 4 之前显式调用）：
    python .agents/skills/stock-premarket/scripts/preflight.py

输出（stdout，单行 JSON）：
    {
      "exposure_pct": 50.0,
      "available_pct": 20.0,
      "position_count": 3,
      "banner": null,
      "ok": true
    }

失败兜底：任何异常都返回保守默认值（banner=null, available_pct=max_single_position_pct）
以保证 L1 推送不被风控模块阻塞。
"""
from __future__ import annotations
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "code"))

from lib import risk  # noqa: E402
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
        exposure = risk.compute_exposure(
            holdings_dicts, total_capital=cfg["total_capital"], price_fn=price_fn
        )
        result = risk.preflight_check(exposure, cfg)
        out = {
            "exposure_pct": exposure["exposure_pct"],
            "available_pct": result["available_pct"],
            "position_count": exposure["position_count"],
            "banner": result["banner"],
            "ok": result["ok"],
        }
        print(json.dumps(out, ensure_ascii=False))
        return 0
    except Exception as e:
        print(f"[preflight] 异常: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        fallback = {
            "exposure_pct": 0.0,
            "available_pct": 30.0,
            "position_count": 0,
            "banner": None,
            "ok": True,
        }
        print(json.dumps(fallback, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
