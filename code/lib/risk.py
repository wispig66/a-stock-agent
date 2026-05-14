"""组合层风控计算。

3 个纯函数对外：
- load_risk_config()  读 risk_config.yaml，缺失走默认值 + stderr warning
- compute_exposure()  根据持仓 + 实时价算总仓位
- preflight_check()   决定是否需要横幅、返回剩余可用额度

设计原则：失败兜底，风控模块异常不能阻塞 L1 推送。
"""
from __future__ import annotations
import sys
from pathlib import Path
from typing import Callable, Iterable

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = ROOT / "risk_config.yaml"

DEFAULT_CONFIG = {
    "total_capital": 500000,
    "max_total_exposure_pct": 70,
    "max_single_position_pct": 30,
    "loss_day_threshold_pct": -2.0,
    "loss_streak_warn_threshold": 2,
}


def load_risk_config() -> dict:
    """读 risk_config.yaml；缺失/解析失败走默认值 + stderr warning。"""
    if not CONFIG_FILE.exists():
        print(
            f"[risk] risk_config.yaml 未找到（{CONFIG_FILE}），使用默认值",
            file=sys.stderr,
        )
        return dict(DEFAULT_CONFIG)
    try:
        raw = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        print(f"[risk] risk_config.yaml 解析失败 ({e})，使用默认值", file=sys.stderr)
        return dict(DEFAULT_CONFIG)
    cfg = dict(DEFAULT_CONFIG)
    for k in DEFAULT_CONFIG:
        if k in raw:
            cfg[k] = raw[k]
    return cfg


def compute_exposure(
    holdings: Iterable[dict],
    total_capital: float,
    price_fn: Callable[[str], float | None],
) -> dict:
    """根据持仓 + 实时价算总仓位。

    Args:
        holdings: 字典序列，至少含 code / cost / shares。可以是 Holding dataclass
            的 list（dataclass 支持属性访问，这里只用 dict 接口便于测试）。
        total_capital: 总资金基准。
        price_fn: code -> 实时价（取不到返回 None，自动回退到 cost）。

    Returns:
        {total_value, exposure_pct, position_count}
    """
    total_value = 0.0
    count = 0
    for h in holdings:
        code = _attr(h, "code")
        cost = float(_attr(h, "cost"))
        shares = int(_attr(h, "shares"))
        if not code or shares <= 0:
            continue
        try:
            price = price_fn(code)
        except Exception:
            price = None
        unit = float(price) if price else cost
        total_value += unit * shares
        count += 1
    pct = (total_value / total_capital * 100) if total_capital > 0 else 0.0
    return {
        "total_value": total_value,
        "exposure_pct": round(pct, 2),
        "position_count": count,
    }


def preflight_check(exposure: dict, cfg: dict) -> dict:
    """根据 exposure 和配置决定是否需要横幅 + 剩余可用额度。

    Returns:
        {
          ok: bool,                # exposure_pct <= max_total_exposure_pct
          banner: str | None,      # 超额时为提示文案
          available_pct: float,    # max(0, 上限 - 当前)
        }
    """
    cap = float(cfg.get("max_total_exposure_pct", 70))
    cur = float(exposure.get("exposure_pct", 0))
    ok = cur <= cap
    available = max(0.0, cap - cur)
    banner = None
    if not ok:
        banner = f"⚠️ 当前总仓位 {cur:.0f}%（上限 {cap:.0f}%）· 今日建议先减仓再加新单"
    return {"ok": ok, "banner": banner, "available_pct": round(available, 2)}


def _attr(obj, key):
    """同时兼容 dict 和 dataclass 的字段访问。"""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def make_price_fn_from_df(df) -> Callable[[str], float | None]:
    """把 akshare stock_zh_a_spot_em() 返回的 DataFrame 转成 code -> price 闭包。

    停牌/缺失返回 None，调用方按 cost 兜底。
    """
    import math
    mapping: dict[str, float] = {}
    for _, row in df.iterrows():
        code = str(row.get("代码", "")).zfill(6)
        price = row.get("最新价")
        try:
            p = float(price)
        except (TypeError, ValueError):
            continue
        if math.isnan(p):
            continue
        mapping[code] = p
    return lambda c: mapping.get(str(c).zfill(6))


def fetch_spot_price_fn() -> Callable[[str], float | None]:
    """拉一次全市场实时价，返回 price_fn。失败返回 always-None 函数。"""
    try:
        import akshare as ak  # type: ignore
        df = ak.stock_zh_a_spot_em()
        return make_price_fn_from_df(df)
    except Exception as e:
        print(f"[risk] stock_zh_a_spot_em 失败: {e}，全部走 cost 兜底", file=sys.stderr)
        return lambda c: None
