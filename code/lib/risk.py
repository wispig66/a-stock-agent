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
    """拉一次全市场实时价，返回 price_fn。失败返回 always-None 函数。

    仅用于需要全市场快照的场景（如盘前扫板）；持仓盘点请用
    fetch_prices_for_codes()，它只查目标 codes 且不依赖代理。
    """
    try:
        import akshare as ak  # type: ignore
        df = ak.stock_zh_a_spot_em()
        return make_price_fn_from_df(df)
    except Exception as e:
        print(f"[risk] stock_zh_a_spot_em 失败: {e}，全部走 cost 兜底", file=sys.stderr)
        return lambda c: None


def _code_with_market_prefix(code: str) -> str:
    """000001 → sz000001 / 600000 → sh600000 / 688xxx → sh688xxx / 8/4 → bj"""
    c = str(code).zfill(6)
    if c.startswith(("60", "68", "90", "11", "13", "5")):
        return f"sh{c}"
    if c.startswith(("00", "30", "20", "15", "16", "18")):
        return f"sz{c}"
    if c.startswith(("8", "4", "92")):
        return f"bj{c}"
    return f"sh{c}"  # 兜底


def _fetch_prices_sina(codes: list[str]) -> dict[str, float]:
    """新浪行情：hq.sinajs.cn/list=sh600000,sz000001
    返回 {code: price}。失败/缺失抛异常或返回部分字典。
    """
    import requests
    if not codes:
        return {}
    syms = ",".join(_code_with_market_prefix(c) for c in codes)
    url = f"https://hq.sinajs.cn/list={syms}"
    headers = {"Referer": "https://finance.sina.com.cn"}
    r = requests.get(url, headers=headers, timeout=6)
    r.encoding = "gbk"
    out: dict[str, float] = {}
    for line in r.text.splitlines():
        # var hq_str_sh600000="浦发银行,15.20,15.10,15.30,..."
        if "=\"" not in line or "\"" not in line:
            continue
        head, _, rest = line.partition("=\"")
        sym = head.rsplit("_", 1)[-1]
        code = sym[2:] if sym[:2] in ("sh", "sz", "bj") else sym
        fields = rest.strip("\";").split(",")
        if len(fields) < 4:
            continue
        try:
            price = float(fields[3])  # 当前价（盘后为收盘价）
        except (TypeError, ValueError):
            continue
        if price > 0:
            out[code] = price
    return out


def _fetch_prices_tencent(codes: list[str]) -> dict[str, float]:
    """腾讯行情：qt.gtimg.cn/q=sh600000,sz000001
    返回 {code: price}。失败/缺失抛异常或返回部分字典。
    """
    import requests
    if not codes:
        return {}
    syms = ",".join(_code_with_market_prefix(c) for c in codes)
    url = f"https://qt.gtimg.cn/q={syms}"
    r = requests.get(url, timeout=6)
    r.encoding = "gbk"
    out: dict[str, float] = {}
    for line in r.text.splitlines():
        # v_sh600000="1~浦发银行~600000~15.20~..."
        if "=\"" not in line:
            continue
        head, _, rest = line.partition("=\"")
        sym = head.lstrip("v_").strip()
        code = sym[2:] if sym[:2] in ("sh", "sz", "bj") else sym
        fields = rest.strip("\";").split("~")
        if len(fields) < 4:
            continue
        try:
            price = float(fields[3])
        except (TypeError, ValueError):
            continue
        if price > 0:
            out[code] = price
    return out


def fetch_prices_for_codes(codes: list[str]) -> tuple[dict[str, float], str]:
    """按需查特定 code 列表的实时价。

    返回 (prices, source)。source 取值：
      - "sina" / "tencent" / "akshare" — 至少拿到一条
      - "none" — 所有源都失败 → 调用方应跳过判定而非 cost 兜底

    源优先级：sina（直连不走代理） → tencent（直连备用） → akshare（最后兜底）。
    """
    codes = [str(c).zfill(6) for c in codes if c]
    if not codes:
        return {}, "none"
    for name, fn in (("sina", _fetch_prices_sina), ("tencent", _fetch_prices_tencent)):
        try:
            prices = fn(codes)
            if prices:
                return prices, name
        except Exception as e:
            print(f"[risk] {name} 行情失败: {e}", file=sys.stderr)
    # 最后兜底 akshare（可能被代理拦）
    try:
        import akshare as ak  # type: ignore
        df = ak.stock_zh_a_spot_em()
        full = make_price_fn_from_df(df)
        prices = {c: full(c) for c in codes if full(c) is not None}
        if prices:
            return prices, "akshare"
    except Exception as e:
        print(f"[risk] akshare 兜底失败: {e}", file=sys.stderr)
    return {}, "none"
