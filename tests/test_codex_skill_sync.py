from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync_codex_skills.sh"


def test_sync_codex_skills_rewrites_runtime_paths(tmp_path):
    src_root = tmp_path / ".claude" / "skills"
    dst_root = tmp_path / ".agents" / "skills"
    skill_dir = src_root / "stock-demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "# stock-demo",
                "Run .claude/skills/stock-demo/scripts/fetch.py",
                "Old typo path .Codex/skills/stock-demo should also be fixed.",
                "Plain Claude Code prose should not be changed.",
            ]
        ),
        encoding="utf-8",
    )
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    (scripts / "fetch.py").write_text("print('ok')\n", encoding="utf-8")

    env = os.environ.copy()
    env["CODEX_SKILL_SOURCE_DIR"] = str(src_root)
    env["CODEX_SKILL_DEST_DIR"] = str(dst_root)
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    synced = dst_root / "stock-demo" / "SKILL.md"
    assert synced.exists()
    text = synced.read_text(encoding="utf-8")
    assert ".agents/skills/stock-demo/scripts/fetch.py" in text
    assert ".agents/skills/stock-demo should also be fixed" in text
    assert "Plain Claude Code prose should not be changed." in text
    assert ".claude/skills" not in text
    assert ".Codex/skills" not in text


def test_sync_codex_skills_rewrites_files_with_spaces(tmp_path):
    src_root = tmp_path / ".claude" / "skills"
    dst_root = tmp_path / ".agents" / "skills"
    skill_dir = src_root / "stock-demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# demo\n", encoding="utf-8")
    (skill_dir / "file with spaces.md").write_text(
        "Run .claude/skills/stock-demo/scripts/fetch.py\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["CODEX_SKILL_SOURCE_DIR"] = str(src_root)
    env["CODEX_SKILL_DEST_DIR"] = str(dst_root)
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    copied = dst_root / "stock-demo" / "file with spaces.md"
    text = copied.read_text(encoding="utf-8")
    assert ".agents/skills/stock-demo/scripts/fetch.py" in text
    assert ".claude/skills" not in text


def test_sync_codex_skills_keeps_nested_files(tmp_path):
    src_root = tmp_path / ".claude" / "skills"
    dst_root = tmp_path / ".agents" / "skills"
    nested = src_root / "stock-demo" / "scripts"
    nested.mkdir(parents=True)
    (src_root / "stock-demo" / "SKILL.md").write_text("# demo\n", encoding="utf-8")
    (nested / "fetch.py").write_text("print('ok')\n", encoding="utf-8")

    env = os.environ.copy()
    env["CODEX_SKILL_SOURCE_DIR"] = str(src_root)
    env["CODEX_SKILL_DEST_DIR"] = str(dst_root)
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    copied = dst_root / "stock-demo" / "scripts" / "fetch.py"
    assert copied.read_text(encoding="utf-8") == "print('ok')\n"


def test_sync_codex_skills_rejects_same_source_and_dest(tmp_path):
    src_root = tmp_path / ".claude" / "skills"
    skill_dir = src_root / "stock-demo"
    skill_dir.mkdir(parents=True)
    source_skill = skill_dir / "SKILL.md"
    source_skill.write_text("# demo\n", encoding="utf-8")

    env = os.environ.copy()
    env["CODEX_SKILL_SOURCE_DIR"] = str(src_root)
    env["CODEX_SKILL_DEST_DIR"] = str(src_root)
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "must not contain each other" in result.stderr
    assert source_skill.read_text(encoding="utf-8") == "# demo\n"


def test_sync_codex_skills_rejects_dest_inside_source(tmp_path):
    src_root = tmp_path / ".claude" / "skills"
    skill_dir = src_root / "stock-demo"
    skill_dir.mkdir(parents=True)
    source_skill = skill_dir / "SKILL.md"
    source_skill.write_text("# demo\n", encoding="utf-8")

    env = os.environ.copy()
    env["CODEX_SKILL_SOURCE_DIR"] = str(src_root)
    env["CODEX_SKILL_DEST_DIR"] = str(src_root / "stock-output")
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "must not contain each other" in result.stderr
    assert source_skill.read_text(encoding="utf-8") == "# demo\n"
    assert not (src_root / "stock-output").exists()


def test_sync_codex_skills_rejects_nested_dest_inside_source_without_creating_parent(tmp_path):
    src_root = tmp_path / ".claude" / "skills"
    skill_dir = src_root / "stock-demo"
    skill_dir.mkdir(parents=True)
    source_skill = skill_dir / "SKILL.md"
    source_skill.write_text("# demo\n", encoding="utf-8")

    env = os.environ.copy()
    env["CODEX_SKILL_SOURCE_DIR"] = str(src_root)
    env["CODEX_SKILL_DEST_DIR"] = str(src_root / "nested" / "stock-output")
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "must not contain each other" in result.stderr
    assert source_skill.read_text(encoding="utf-8") == "# demo\n"
    assert not (src_root / "nested").exists()


def test_sync_codex_skills_rejects_missing_source_inside_dest_without_creating_dirs(tmp_path):
    dst_root = tmp_path / ".agents" / "skills"
    src_root = dst_root / "source"

    env = os.environ.copy()
    env["CODEX_SKILL_SOURCE_DIR"] = str(src_root)
    env["CODEX_SKILL_DEST_DIR"] = str(dst_root)
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "must not contain each other" in result.stderr
    assert not src_root.exists()
    assert not dst_root.exists()
