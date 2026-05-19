from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_premarket_uses_detected_uv_binary():
    script = (ROOT / "code" / "run_premarket.sh").read_text()

    assert 'export PATH="/opt/homebrew/bin:' in script
    assert 'for candidate in "$HOME/.local/bin/uv"' in script
    assert "ALREADY_PUSHED=$(\"$UV_BIN\" run --no-sync python -c" in script
    assert "/opt/homebrew/bin/uv run" not in script
    assert "PREMARKET_CLAUDE_TIMEOUT_SEC" in script
    assert "claude 仍在运行" in script


def test_postmarket_uv_probe_includes_anaconda_uv():
    script = (ROOT / "code" / "run_postmarket.sh").read_text()

    assert 'export PATH="/opt/homebrew/bin:' in script
    assert '"$HOME/anaconda3/bin/uv"' in script


def test_intraday_sets_launchd_safe_path():
    script = (ROOT / "code" / "run_intraday.sh").read_text()

    assert 'export PATH="/opt/homebrew/bin:' in script
