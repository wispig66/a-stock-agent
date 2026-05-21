"""ladder_gap 判定回归。5/18 教训：6→4 跨级 5 板真空是次日加速失败的领先指标。"""
from __future__ import annotations
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".agents/skills/stock-postmarket/scripts"))

from fetch_postmarket import compute_ladder_gap  # noqa: E402


def _zt(consecs: list[int]) -> pd.DataFrame:
    return pd.DataFrame({"连板数": consecs})


def test_no_gap_smooth_ladder():
    """1→2→3→4 连续，无断层。"""
    res = compute_ladder_gap(_zt([1] * 70 + [2] * 8 + [3] * 3 + [4] * 1))
    assert res["ladder_gap"] is False
    assert res["max_gap_size"] == 1
    assert res["missing_steps"] == []


def test_gap_jump_6_to_4_real_5_18():
    """今日（5/18）真实分布：1×73 / 2×5 / 3×1 / 4×2 / 6×1，缺 5 板。"""
    res = compute_ladder_gap(_zt([1] * 73 + [2] * 5 + [3] * 1 + [4] * 2 + [6] * 1))
    assert res["ladder_gap"] is True
    assert res["max_gap_size"] == 2
    assert res["missing_steps"] == [5]


def test_multi_gap():
    """2/5/8 板，缺 3/4/6/7。最大跨级 3。"""
    res = compute_ladder_gap(_zt([1] * 10 + [2] * 3 + [5] * 1 + [8] * 1))
    assert res["ladder_gap"] is True
    assert res["max_gap_size"] == 3
    assert set(res["missing_steps"]) == {3, 4, 6, 7}


def test_only_one_level():
    """只有 1 板，无梯队判定意义。"""
    res = compute_ladder_gap(_zt([1] * 50))
    assert res["ladder_gap"] is False
    assert res["max_gap_size"] == 0


def test_empty_zt():
    res = compute_ladder_gap(pd.DataFrame())
    assert res["ladder_gap"] is False
    assert res["top_consec_dist"] == {}
