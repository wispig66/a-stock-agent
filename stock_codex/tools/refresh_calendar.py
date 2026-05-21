"""幂等刷新本地交易日历。
数据源：akshare.tool_trade_date_hist_sina()（返回 1990 至次年底全部 A 股交易日）。
失败时不删除现有 csv，仅打印错误并退非零码。
"""
from __future__ import annotations
import sys
from datetime import date

import akshare as ak  # type: ignore
from stock_codex.paths import TRADE_CALENDAR_FILE as CSV



def main() -> int:
    CSV.parent.mkdir(parents=True, exist_ok=True)
    try:
        df = ak.tool_trade_date_hist_sina()
    except Exception as e:
        print(f"[refresh_calendar] akshare 拉取失败：{e}", file=sys.stderr)
        return 1

    # akshare 返回列名为 trade_date，类型可能是 datetime.date 或字符串
    col = "trade_date"
    if col not in df.columns:
        print(f"[refresh_calendar] 接口返回无 {col} 列，实际列：{list(df.columns)}", file=sys.stderr)
        return 2

    dates = sorted({_to_date(v) for v in df[col]})
    lines = ["trade_date"] + [d.isoformat() for d in dates]
    CSV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[refresh_calendar] 写入 {len(dates)} 个交易日 → {CSV} (最新 {dates[-1]})")
    return 0


def _to_date(v) -> date:
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


if __name__ == "__main__":
    sys.exit(main())
