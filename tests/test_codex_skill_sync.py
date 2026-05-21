from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync_codex_skills.sh"


def test_sync_codex_skills_validates_tracked_agents_tree(tmp_path):
    skill_root = tmp_path / ".agents" / "skills"
    skill_dir = skill_root / "stock-demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# stock-demo\n", encoding="utf-8")

    env = os.environ.copy()
    env["CODEX_SKILL_DIR"] = str(skill_root)
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Codex skills ready" in result.stdout


def test_sync_codex_skills_rejects_missing_agents_tree(tmp_path):
    env = os.environ.copy()
    env["CODEX_SKILL_DIR"] = str(tmp_path / ".agents" / "skills")
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "CODEX_SKILL_DIR does not exist" in result.stderr
