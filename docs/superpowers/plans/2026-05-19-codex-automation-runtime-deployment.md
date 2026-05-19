# Codex Automation 远程运行机部署 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把项目迁移成开源友好的 pull-based 远程 runtime host 部署流程，并让 Codex automations、skills、docs、tests 都适合无人值守运行。

**Architecture:** 把部署职责拆成四层：`setup.sh` 只负责项目环境初始化；`install_runtime_services.sh` 只负责长时 launchd daemon；`install_codex_automations.sh` 只负责 Codex automation TOML；`deploy_remote_codex.sh` 只负责从开发机 SSH 触发远程机执行同一套 pull-based 命令。测试先锁住 automation 生成、skill sync、docs 口径和日期窗口行为，再改实现。

**Tech Stack:** Bash, Python 3.11+, pytest, SQLite, launchctl, Codex automation TOML, macOS launchd, uv.

---

## File Structure

- Modify: `scripts/install_codex_automations.sh`
  - Add `--dry-run`, `--output-dir`, centralized prompt templates, validation summary.
- Modify: `scripts/sync_codex_skills.sh`
  - Add testable source/destination env overrides and safer path rewriting.
- Modify: `scripts/setup.sh`
  - Make it environment setup only; remove Claude CLI requirement and default launchd install.
- Modify: `scripts/install_launchd.sh`
  - Narrow or wrap the new runtime-service installer for compatibility.
- Create: `scripts/install_runtime_services.sh`
  - Install only long-running non-LLM launchd services.
- Create: `scripts/deploy_remote_codex.sh`
  - SSH-triggered pull-based remote deployment helper.
- Create: `scripts/doctor_codex_runtime.sh`
  - Runtime readiness check without sending real trading cards.
- Create: `deploy.remote.example.env`
  - Open-source-safe sample config for remote staging deployment.
- Modify: `.gitignore`
  - Ignore `deploy.remote.env`.
- Modify: `code/lib/sector_pack.py`
  - Anchor recent windows to latest available DB date.
- Modify: `tests/test_sector_pack_panels.py`
  - Lock the DB-date anchored behavior.
- Create: `tests/test_codex_automations.py`
  - Test generated automation TOML and prompt contracts.
- Create: `tests/test_codex_skill_sync.py`
  - Test `.claude/skills` to `.agents/skills` sync and rewrite.
- Create: `tests/test_codex_runtime_scripts.py`
  - Static and dry-run tests for setup/runtime/deploy/doctor scripts.
- Create: `tests/test_docs_codex_migration.py`
  - Prevent README/docs drift back to launchd-only short LLM wording.
- Modify: `.claude/skills/stock-premarket/SKILL.md`
- Modify: `.claude/skills/stock-intraday/SKILL.md`
- Modify: `.claude/skills/stock-postmarket/SKILL.md`
- Modify: `.claude/skills/stock-weekly/SKILL.md`
  - Harden output, failure, idempotency, and Codex runtime contracts.
- Modify: `README.md`
- Modify: `docs/codex_automations.md`
- Modify: `docs/card_validator_enforce_switch.md`
  - Update open-source deployment and Codex automation operations docs.

---

### Task 1: Automation TOML 生成测试

**Files:**
- Create: `tests/test_codex_automations.py`
- Modify later: `scripts/install_codex_automations.sh`

- [ ] **Step 1: 写失败测试，要求 automation 安装脚本支持临时输出目录和 dry-run**

Create `tests/test_codex_automations.py`:

```python
from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install_codex_automations.sh"

EXPECTED_JOBS = {
    "stock-premarket": "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=8;BYMINUTE=30",
    "stock-intraday-09-30": "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=30",
    "stock-intraday-09-45": "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=45",
    "stock-intraday-11-30": "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=11;BYMINUTE=30",
    "stock-intraday-14-30": "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=14;BYMINUTE=30",
    "stock-postmarket": "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=15;BYMINUTE=35",
    "stock-weekly-review": "FREQ=WEEKLY;INTERVAL=1;BYDAY=SU;BYHOUR=21;BYMINUTE=0",
}


def run_installer(output_dir: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CODEX_AUTOMATIONS_DIR"] = str(output_dir)
    env["CODEX_AUTOMATION_MODEL"] = "gpt-5.4"
    env["CODEX_AUTOMATION_REASONING_EFFORT"] = "medium"
    return subprocess.run(
        ["bash", str(SCRIPT), "--dry-run", *extra_args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def load_job(output_dir: Path, job_id: str) -> dict:
    data = (output_dir / job_id / "automation.toml").read_bytes()
    return tomllib.loads(data.decode("utf-8"))


def test_codex_automation_dry_run_generates_all_jobs(tmp_path):
    out_dir = tmp_path / "automations"

    result = run_installer(out_dir)

    assert result.returncode == 0, result.stderr
    assert "[dry-run]" in result.stdout
    assert sorted(p.name for p in out_dir.iterdir()) == sorted(EXPECTED_JOBS)


def test_codex_automation_toml_contract(tmp_path):
    out_dir = tmp_path / "automations"
    result = run_installer(out_dir)
    assert result.returncode == 0, result.stderr

    for job_id, rrule in EXPECTED_JOBS.items():
        job = load_job(out_dir, job_id)
        assert job["version"] == 1
        assert job["id"] == job_id
        assert job["kind"] == "cron"
        assert job["status"] == "ACTIVE"
        assert job["rrule"] == rrule
        assert job["model"] == "gpt-5.4"
        assert job["reasoning_effort"] == "medium"
        assert job["execution_environment"] == "local"
        assert job["cwds"] == [str(ROOT)]
        assert isinstance(job["created_at"], int)
        assert isinstance(job["updated_at"], int)


def test_codex_automation_prompts_have_unattended_contract(tmp_path):
    out_dir = tmp_path / "automations"
    result = run_installer(out_dir)
    assert result.returncode == 0, result.stderr

    for job_id in EXPECTED_JOBS:
        prompt = load_job(out_dir, job_id)["prompt"]
        assert "Required behavior:" in prompt
        assert "Failure handling:" in prompt
        assert "Final response:" in prompt
        assert "do not claim success" in prompt
        assert "push.py" in prompt
        assert "claude -p" not in prompt


def test_codex_automation_installer_summary_lists_jobs(tmp_path):
    out_dir = tmp_path / "automations"
    result = run_installer(out_dir)

    assert result.returncode == 0, result.stderr
    for job_id in EXPECTED_JOBS:
        assert job_id in result.stdout
    assert "Installed Codex automations under" in result.stdout
```

- [ ] **Step 2: 运行测试，确认当前失败**

Run:

```bash
uv run pytest tests/test_codex_automations.py -q
```

Expected: FAIL，原因包括 `--dry-run` 未实现、prompt 缺少 `Required behavior:` / `Failure handling:`。

- [ ] **Step 3: 提交失败测试**

```bash
git add tests/test_codex_automations.py
git commit -m "test: cover codex automation generation contract"
```

---

### Task 2: 强化 `install_codex_automations.sh`

**Files:**
- Modify: `scripts/install_codex_automations.sh`
- Test: `tests/test_codex_automations.py`

- [ ] **Step 1: 将脚本替换为支持 dry-run、输出目录覆盖、结构化 prompt 的版本**

Replace `scripts/install_codex_automations.sh` with:

```bash
#!/usr/bin/env bash
# Install Codex app cron automations for short stock LLM jobs.
# Run this on the machine whose Codex app should execute the jobs.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
AUTOMATIONS_DIR="${CODEX_AUTOMATIONS_DIR:-$CODEX_HOME/automations}"
MODEL="${CODEX_AUTOMATION_MODEL:-gpt-5.4}"
REASONING_EFFORT="${CODEX_AUTOMATION_REASONING_EFFORT:-medium}"
DRY_RUN=0

usage() {
    cat <<'USAGE'
Usage: bash scripts/install_codex_automations.sh [--dry-run] [--output-dir DIR]

Options:
  --dry-run        Generate files and print a summary without implying the Codex app reloaded them.
  --output-dir DIR Write automation files to DIR instead of ~/.codex/automations.
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --output-dir)
            [ "$#" -ge 2 ] || { echo "--output-dir requires a value" >&2; exit 2; }
            AUTOMATIONS_DIR="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

mkdir -p "$AUTOMATIONS_DIR"

now_ms() {
    python3 - <<'PY'
import time
print(int(time.time() * 1000))
PY
}

skill_prompt() {
    local skill="$1"
    local outputs="$2"
    local extra="$3"
    cat <<EOF
Use the $skill skill in this repository.

Required behavior:
1. Run the skill workflow from the current repository checkout.
2. Produce the required output artifacts: $outputs.
3. Push user-facing cards through the unified push.py path when a push is required.
4. Preserve the workflow idempotency rules documented in the skill.
$extra

Failure handling:
- If a required data source, database, validator, or Telegram push step fails, report the exact failed step.
- Do not invent missing market data.
- Do not claim success unless the required output and push steps completed.

Final response:
- Return one short operational summary, or the concrete failure reason.
EOF
}

write_automation() {
    local id="$1"
    local name="$2"
    local rrule="$3"
    local prompt="$4"
    local dir="$AUTOMATIONS_DIR/$id"
    local ts
    ts="$(now_ms)"

    mkdir -p "$dir"
    cat > "$dir/automation.toml" <<EOF
version = 1
id = "$id"
kind = "cron"
name = "$name"
prompt = "$prompt"
status = "ACTIVE"
rrule = "$rrule"
model = "$MODEL"
reasoning_effort = "$REASONING_EFFORT"
execution_environment = "local"
cwds = ["$ROOT"]
created_at = $ts
updated_at = $ts
EOF

    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] generated $id | $rrule | cwd=$ROOT | model=$MODEL | effort=$REASONING_EFFORT"
    else
        echo "[+] installed $id | $rrule | cwd=$ROOT | model=$MODEL | effort=$REASONING_EFFORT"
    fi
}

if [ ! -d "$ROOT/.agents/skills" ]; then
    echo "[warn] .agents/skills not found. Run: bash scripts/sync_codex_skills.sh" >&2
fi

write_automation \
    "stock-premarket" \
    "stock premarket" \
    "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=8;BYMINUTE=30" \
    "$(skill_prompt "stock-premarket" "data/last_card.md and Telegram push_log row" "5. Use only facts allowed by the generated fact pack.")"

write_automation \
    "stock-intraday-09-30" \
    "stock intraday 09:30" \
    "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=30" \
    "$(skill_prompt "stock-intraday" "data/last_intraday_card.md and Telegram push_log row" "5. Run the current-time branch for the scheduled intraday checkpoint.")"

write_automation \
    "stock-intraday-09-45" \
    "stock intraday 09:45" \
    "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=45" \
    "$(skill_prompt "stock-intraday" "data/last_intraday_card.md and Telegram push_log row" "5. Run the current-time branch for the scheduled intraday checkpoint.")"

write_automation \
    "stock-intraday-11-30" \
    "stock intraday 11:30" \
    "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=11;BYMINUTE=30" \
    "$(skill_prompt "stock-intraday" "data/last_intraday_card.md and Telegram push_log row" "5. Run the half-day branch for the scheduled intraday checkpoint.")"

write_automation \
    "stock-intraday-14-30" \
    "stock intraday 14:30" \
    "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=14;BYMINUTE=30" \
    "$(skill_prompt "stock-intraday" "data/last_intraday_card.md and Telegram push_log row" "5. Run the end-day branch for the scheduled intraday checkpoint.")"

write_automation \
    "stock-postmarket" \
    "stock postmarket" \
    "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=15;BYMINUTE=35" \
    "$(skill_prompt "stock-postmarket" "data/last_postmarket_card.md, sentiment updates, stock_basic refresh, and Telegram push_log row" "5. Refresh stock_basic if the main postmarket push completed; report refresh failure without hiding the postmarket result.")"

write_automation \
    "stock-weekly-review" \
    "stock weekly review" \
    "FREQ=WEEKLY;INTERVAL=1;BYDAY=SU;BYHOUR=21;BYMINUTE=0" \
    "$(skill_prompt "stock-weekly" "data/weekly_review/YYYY-WW.md and Telegram push_log row" "5. Skip cleanly when the current weekly review already exists unless explicitly forced.")"

echo
echo "Installed Codex automations under $AUTOMATIONS_DIR"
```

- [ ] **Step 2: 运行 automation 测试**

Run:

```bash
uv run pytest tests/test_codex_automations.py -q
```

Expected: PASS.

- [ ] **Step 3: 运行 shell 语法检查**

Run:

```bash
bash -n scripts/install_codex_automations.sh
```

Expected: no output, exit 0.

- [ ] **Step 4: 提交实现**

```bash
git add scripts/install_codex_automations.sh
git commit -m "feat: harden codex automation installer"
```

---

### Task 3: Skill sync 可测试化和路径改写测试

**Files:**
- Create: `tests/test_codex_skill_sync.py`
- Modify: `scripts/sync_codex_skills.sh`

- [ ] **Step 1: 写失败测试**

Create `tests/test_codex_skill_sync.py`:

```python
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
        "\n".join([
            "# stock-demo",
            "Run .claude/skills/stock-demo/scripts/fetch.py",
            "Old typo path .Codex/skills/stock-demo should also be fixed.",
            "Plain Claude Code prose should not be changed.",
        ]),
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
    assert (dst_root / "stock-demo" / "scripts" / "fetch.py").read_text(encoding="utf-8") == "print('ok')\n"
```

- [ ] **Step 2: 运行测试，确认当前失败**

Run:

```bash
uv run pytest tests/test_codex_skill_sync.py -q
```

Expected: FAIL，因为脚本不支持 `CODEX_SKILL_SOURCE_DIR` / `CODEX_SKILL_DEST_DIR`。

- [ ] **Step 3: 修改 `scripts/sync_codex_skills.sh`**

Replace the top-level directory setup and loop with this complete script:

```bash
#!/usr/bin/env bash
# Build Codex-local stock skills from the canonical .claude/skills tree.
# .agents/ is gitignored, so run this on each machine that should use Codex
# skills or Codex automations.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SRC_DIR="${CODEX_SKILL_SOURCE_DIR:-$ROOT/.claude/skills}"
DST_DIR="${CODEX_SKILL_DEST_DIR:-$ROOT/.agents/skills}"

mkdir -p "$DST_DIR"

for src in "$SRC_DIR"/stock-*; do
    [ -d "$src" ] || continue
    name="$(basename "$src")"
    dst="$DST_DIR/$name"
    rm -rf "$dst"
    cp -R "$src" "$dst"
done

tmp_files="$(mktemp)"
trap 'rm -f "$tmp_files"' EXIT

if command -v rg >/dev/null 2>&1; then
    rg --no-ignore -l '\.claude/skills|\.Codex/skills|\.claude -> stock' "$DST_DIR"/stock-* > "$tmp_files" 2>/dev/null || true
else
    grep -RIlE '\.claude/skills|\.Codex/skills|\.claude -> stock' "$DST_DIR"/stock-* > "$tmp_files" 2>/dev/null || true
fi

if [ -s "$tmp_files" ]; then
    xargs perl -pi -e 's#\.Codex/skills#.agents/skills#g; s#\.claude/skills#.agents/skills#g; s#\.claude -> stock#.agents -> stock#g' < "$tmp_files"
fi

echo "Synced Codex skills into $DST_DIR:"
find "$DST_DIR" -maxdepth 2 -name SKILL.md -print | sort
```

- [ ] **Step 4: 运行测试和语法检查**

Run:

```bash
uv run pytest tests/test_codex_skill_sync.py -q
bash -n scripts/sync_codex_skills.sh
```

Expected: pytest PASS; `bash -n` no output.

- [ ] **Step 5: 提交**

```bash
git add tests/test_codex_skill_sync.py scripts/sync_codex_skills.sh
git commit -m "test: cover codex skill sync path rewriting"
```

---

### Task 4: 修复日期窗口基准，消除 `sector_pack` 漂移

**Files:**
- Modify: `tests/test_sector_pack_panels.py`
- Modify: `code/lib/sector_pack.py`

- [ ] **Step 1: 加一个明确的滞后数据测试**

Append to `tests/test_sector_pack_panels.py`:

```python
def test_sentiment_panel_anchors_to_latest_db_date_when_data_is_stale(db):
    panel = sector_pack._fetch_sentiment_panel("光伏")

    assert panel["as_of_date"] == "2026-05-14"
    assert panel["window_start"] == "2026-05-09"
    assert panel["limit_up_count"] == 10
```

- [ ] **Step 2: 运行目标测试，确认失败**

Run:

```bash
uv run pytest tests/test_sector_pack_panels.py::test_sentiment_panel_anchors_to_latest_db_date_when_data_is_stale -q
```

Expected: FAIL，当前返回没有 `as_of_date` / `window_start`。

- [ ] **Step 3: 修改 `code/lib/sector_pack.py`，新增 DB 锚点 helper**

Add below `DB = ROOT / "data" / "daily.db"`:

```python
def _latest_table_date(conn: sqlite3.Connection, table: str, where: str = "", params: tuple = ()) -> str | None:
    sql = f"SELECT MAX(date) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    row = conn.execute(sql, params).fetchone()
    return row[0] if row and row[0] else None


def _window_start_from_anchor(anchor: str | None, days: int) -> tuple[str, str]:
    anchor_date = date.fromisoformat(anchor) if anchor else date.today()
    return (anchor_date - timedelta(days=days)).isoformat(), anchor_date.isoformat()
```

Replace `_fetch_sentiment_panel()` with:

```python
def _fetch_sentiment_panel(sector: str) -> dict:
    """近 5 日命中该题材的涨停记录数 + 候选骨架。
    窗口锚定到数据库里该题材最新可用日期，避免周末/节假日/数据滞后时误判热度为 0。"""
    with sqlite3.connect(DB) as conn:
        latest = _latest_table_date(conn, "limit_up", "concept LIKE ?", (f"%{sector}%",))
        since, as_of = _window_start_from_anchor(latest, 5)
        lu_rows = conn.execute(
            "SELECT date, code, name FROM limit_up WHERE date >= ? AND date <= ? AND concept LIKE ?",
            (since, as_of, f"%{sector}%"),
        ).fetchall()
        limit_up_count = len(lu_rows)
        codes = {c for _, c, _ in lu_rows}
        candidates = []
        if codes:
            placeholders = ",".join("?" * len(codes))
            for code, name, is_st in conn.execute(
                f"SELECT code, name, is_st FROM stock_basic WHERE code IN ({placeholders})",
                tuple(codes),
            ):
                candidates.append({
                    "code": code, "name": name, "is_st": bool(is_st),
                    "ret_5d": 0, "main_inflow_3d": 0, "dist_high_20d_pct": 99,
                    "limit_up_lock": False,
                })
    return {
        "limit_up_count": limit_up_count,
        "leader_consecutive": 0,
        "ret_5d_pct": 0,
        "ret_3d_pct": 0,
        "as_of_date": as_of,
        "window_start": since,
        "candidates": candidates,
    }
```

- [ ] **Step 4: 运行相关测试**

Run:

```bash
uv run pytest tests/test_sector_pack_panels.py tests/test_sector_pack.py -q
```

Expected: PASS.

- [ ] **Step 5: 提交**

```bash
git add tests/test_sector_pack_panels.py code/lib/sector_pack.py
git commit -m "fix: anchor sector sentiment windows to latest data"
```

---

### Task 5: Runtime 脚本边界测试

**Files:**
- Create: `tests/test_codex_runtime_scripts.py`
- Modify later: `scripts/setup.sh`
- Create later: `scripts/install_runtime_services.sh`
- Create later: `scripts/deploy_remote_codex.sh`
- Create later: `scripts/doctor_codex_runtime.sh`

- [ ] **Step 1: 写静态和 dry-run 测试**

Create `tests/test_codex_runtime_scripts.py`:

```python
from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_setup_no_longer_requires_claude_or_installs_launchd():
    script = read("scripts/setup.sh")

    assert "command -v claude" not in script
    assert "Claude Code CLI" not in script
    assert "launchctl bootstrap" not in script
    assert "scripts/install_runtime_services.sh" in script
    assert "scripts/install_codex_automations.sh" in script


def test_runtime_services_installer_only_scans_long_running_templates():
    script = read("scripts/install_runtime_services.sh")

    assert "stockwatchloop" in script
    assert "stockanomalyloop" in script
    assert "stockthemeloop" in script
    assert "stockpremarket" not in script
    assert "stockintraday" not in script
    assert "stockpostmarket" not in script
    assert "stockweekly" not in script


def test_remote_deploy_uses_pull_based_flow_not_rsync():
    script = read("scripts/deploy_remote_codex.sh")

    assert "deploy.remote.env" in script
    assert "git clone" in script
    assert "git pull --ff-only" in script
    assert "rsync" not in script
    assert "scripts/install_codex_automations.sh" in script
    assert "scripts/doctor_codex_runtime.sh" in script


def test_doctor_does_not_send_real_telegram_push():
    script = read("scripts/doctor_codex_runtime.sh")

    assert "notify.py test" not in script
    assert "sendMessage" not in script
    assert "TG_BOT_TOKEN" in script
    assert "automations" in script
    assert "launchctl list" in script


def test_new_shell_scripts_parse():
    for script in [
        "scripts/install_runtime_services.sh",
        "scripts/deploy_remote_codex.sh",
        "scripts/doctor_codex_runtime.sh",
    ]:
        result = subprocess.run(
            ["bash", "-n", str(ROOT / script)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
```

- [ ] **Step 2: 运行测试，确认失败**

Run:

```bash
uv run pytest tests/test_codex_runtime_scripts.py -q
```

Expected: FAIL，因为新脚本不存在，`setup.sh` 仍检查 Claude/安装 launchd。

- [ ] **Step 3: 提交失败测试**

```bash
git add tests/test_codex_runtime_scripts.py
git commit -m "test: cover codex runtime deployment scripts"
```

---

### Task 6: 重构 `setup.sh` 并新增 runtime services 安装器

**Files:**
- Modify: `scripts/setup.sh`
- Create: `scripts/install_runtime_services.sh`
- Modify: `scripts/install_launchd.sh`
- Test: `tests/test_codex_runtime_scripts.py`

- [ ] **Step 1: 修改 `scripts/setup.sh` 文案和行为**

Edit `scripts/setup.sh`:

- Delete the `command -v claude` check block.
- Replace Step 6 launchd install block with a guidance-only block:

```bash
# ── Step 6: Codex / runtime services 指引 ─────────────────────
step "[6/8] 调度安装指引"
warn "setup.sh 只初始化项目环境，不安装定时任务。"
echo "短时 LLM jobs（盘前/盘中/盘后/周报）："
echo "  bash scripts/sync_codex_skills.sh"
echo "  bash scripts/install_codex_automations.sh"
echo "长时 daemon（watch/anomaly/theme）："
echo "  bash scripts/install_runtime_services.sh"
```

- Keep `chmod +x code/run_*.sh scripts/*.sh`.
- Keep Telegram test, but make missing `.env` values stop before the test as today.
- Update final message to show:

```bash
echo "安装 Codex automations："
echo "  bash scripts/sync_codex_skills.sh"
echo "  bash scripts/install_codex_automations.sh"
echo
echo "安装长时 launchd daemon："
echo "  bash scripts/install_runtime_services.sh"
```

- [ ] **Step 2: 新增 `scripts/install_runtime_services.sh`**

Create `scripts/install_runtime_services.sh`:

```bash
#!/usr/bin/env bash
# Install long-running non-LLM launchd services.
# Short LLM jobs are installed through Codex automations, not launchd.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

LAUNCHD_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$LAUNCHD_DIR"

SERVICES=(
    "com.user.stockwatchloop"
    "com.user.stockanomalyloop"
    "com.user.stockthemeloop"
)

if [ "${ENABLE_TG_LISTENER_LAUNCHD:-0}" = "1" ]; then
    SERVICES+=("disabled/com.user.stocktglistener")
fi

for service in "${SERVICES[@]}"; do
    plist_template="launchd/$service.plist"
    if [ ! -f "$plist_template" ]; then
        echo "[warn] missing template $plist_template" >&2
        continue
    fi

    plist_name="$(basename "$plist_template")"
    target="$LAUNCHD_DIR/$plist_name"
    label="${plist_name%.plist}"

    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
        launchctl bootout "gui/$(id -u)" "$target" 2>/dev/null || true
        echo "[+] bootout $label"
    fi

    sed "s|{{PROJECT_ROOT}}|$PROJECT_ROOT|g" "$plist_template" > "$target"
    launchctl bootstrap "gui/$(id -u)" "$target"
    echo "[+] bootstrap $label"
done

echo
echo "已安装的 stock runtime services："
launchctl list | awk '/com.user.stock/ {print "  " $0}'
```

- [ ] **Step 3: 将 `scripts/install_launchd.sh` 改成兼容 wrapper**

Replace `scripts/install_launchd.sh` with:

```bash
#!/usr/bin/env bash
# Compatibility wrapper. Short LLM jobs now use Codex automations.

set -euo pipefail

echo "[info] scripts/install_launchd.sh is kept for compatibility."
echo "[info] Installing long-running runtime services only."
exec "$(cd "$(dirname "$0")" && pwd)/install_runtime_services.sh"
```

- [ ] **Step 4: 运行测试和语法检查**

Run:

```bash
uv run pytest tests/test_codex_runtime_scripts.py -q
bash -n scripts/setup.sh scripts/install_runtime_services.sh scripts/install_launchd.sh
```

Expected: PASS and no shell syntax output.

- [ ] **Step 5: 提交**

```bash
git add scripts/setup.sh scripts/install_runtime_services.sh scripts/install_launchd.sh
git commit -m "refactor: split setup from runtime service install"
```

---

### Task 7: 远程部署和 runtime doctor 脚本

**Files:**
- Create: `deploy.remote.example.env`
- Modify: `.gitignore`
- Create: `scripts/deploy_remote_codex.sh`
- Create: `scripts/doctor_codex_runtime.sh`
- Test: `tests/test_codex_runtime_scripts.py`

- [ ] **Step 1: 新增远程部署示例配置**

Create `deploy.remote.example.env`:

```bash
# Copy to deploy.remote.env and edit locally. Do not commit deploy.remote.env.
REMOTE_HOST=user@example-host
REMOTE_ROOT=~/stock
REMOTE_REPO_URL=https://github.com/your-org/a-stock-agent.git
REMOTE_BRANCH=main
REMOTE_RUN_TESTS=1
```

Add to `.gitignore`:

```gitignore
deploy.remote.env
```

- [ ] **Step 2: 新增 `scripts/deploy_remote_codex.sh`**

Create `scripts/deploy_remote_codex.sh`:

```bash
#!/usr/bin/env bash
# Trigger pull-based deployment on the remote runtime host.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${DEPLOY_REMOTE_ENV:-$ROOT/deploy.remote.env}"

if [ -f "$CONFIG" ]; then
    # shellcheck disable=SC1090
    source "$CONFIG"
fi

: "${REMOTE_HOST:?Set REMOTE_HOST in deploy.remote.env or environment}"
: "${REMOTE_ROOT:?Set REMOTE_ROOT in deploy.remote.env or environment}"
: "${REMOTE_REPO_URL:?Set REMOTE_REPO_URL in deploy.remote.env or environment}"
REMOTE_BRANCH="${REMOTE_BRANCH:-main}"
REMOTE_RUN_TESTS="${REMOTE_RUN_TESTS:-1}"

ssh "$REMOTE_HOST" "bash -s" <<EOF
set -euo pipefail

REMOTE_ROOT="$REMOTE_ROOT"
REMOTE_REPO_URL="$REMOTE_REPO_URL"
REMOTE_BRANCH="$REMOTE_BRANCH"
REMOTE_RUN_TESTS="$REMOTE_RUN_TESTS"

if [ ! -d "\$REMOTE_ROOT/.git" ]; then
    mkdir -p "\$(dirname "\$REMOTE_ROOT")"
    git clone "\$REMOTE_REPO_URL" "\$REMOTE_ROOT"
fi

cd "\$REMOTE_ROOT"
git fetch origin "\$REMOTE_BRANCH"
git checkout "\$REMOTE_BRANCH"
git pull --ff-only origin "\$REMOTE_BRANCH"

bash scripts/setup.sh
bash scripts/sync_codex_skills.sh
bash scripts/install_codex_automations.sh
bash scripts/install_runtime_services.sh
bash scripts/disable_legacy_claude_launchd.sh
bash scripts/doctor_codex_runtime.sh

if [ "\$REMOTE_RUN_TESTS" = "1" ]; then
    uv run pytest tests/
else
    echo "[skip] REMOTE_RUN_TESTS=\$REMOTE_RUN_TESTS"
fi

echo
echo "Remote deployment summary:"
echo "  root: \$(pwd)"
echo "  branch: \$(git branch --show-current)"
echo "  commit: \$(git rev-parse --short HEAD)"
EOF
```

- [ ] **Step 3: 新增 `scripts/doctor_codex_runtime.sh`**

Create `scripts/doctor_codex_runtime.sh`:

```bash
#!/usr/bin/env bash
# Check runtime readiness without sending real trading cards.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ok() { printf "[ok] %s\n" "$1"; }
warn() { printf "[warn] %s\n" "$1"; }
fail() { printf "[fail] %s\n" "$1" >&2; exit 1; }

command -v uv >/dev/null 2>&1 && ok "uv: $(uv --version)" || fail "uv not found"
command -v sqlite3 >/dev/null 2>&1 && ok "sqlite3 found" || fail "sqlite3 not found"

[ -d "$HOME/.codex/automations" ] && ok "Codex automations dir exists" || warn "Codex automations dir missing"
[ -d ".agents/skills" ] && ok ".agents/skills exists" || fail ".agents/skills missing; run scripts/sync_codex_skills.sh"

for skill in stock-premarket stock-intraday stock-postmarket stock-weekly; do
    [ -f ".agents/skills/$skill/SKILL.md" ] && ok "skill $skill exists" || fail "skill $skill missing"
done

if [ -f ".env" ]; then
    grep -q '^TG_BOT_TOKEN=' .env && ok "TG_BOT_TOKEN present in .env" || warn "TG_BOT_TOKEN missing in .env"
    grep -q '^TG_CHAT_ID=' .env && ok "TG_CHAT_ID present in .env" || warn "TG_CHAT_ID missing in .env"
else
    warn ".env missing"
fi

[ -f "data/daily.db" ] && ok "data/daily.db exists" || fail "data/daily.db missing"
sqlite3 data/daily.db "SELECT name FROM sqlite_master WHERE type='table' AND name='push_log';" | grep -q push_log \
    && ok "daily.db push_log table exists" || fail "daily.db missing push_log"

[ -f "data/trade_calendar.csv" ] && ok "trade calendar exists" || fail "data/trade_calendar.csv missing"

for job in stock-premarket stock-intraday-09-30 stock-intraday-09-45 stock-intraday-11-30 stock-intraday-14-30 stock-postmarket stock-weekly-review; do
    f="$HOME/.codex/automations/$job/automation.toml"
    if [ -f "$f" ]; then
        grep -q "cwds = \\[\"$ROOT\"\\]" "$f" && ok "automation $job cwd ok" || warn "automation $job cwd does not point to $ROOT"
    else
        warn "automation $job missing"
    fi
done

for label in com.user.stockpremarket com.user.stockintraday com.user.stockpostmarket com.user.stockweekly; do
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
        fail "legacy short LLM launchd job still loaded: $label"
    else
        ok "legacy short LLM launchd job not loaded: $label"
    fi
done

echo
echo "Loaded stock launchd jobs:"
launchctl list | awk '/com.user.stock/ {print "  " $0}' || true

echo
echo "CARD_VALIDATOR_MODE: ${CARD_VALIDATOR_MODE:-warn(default)}"
```

- [ ] **Step 4: 运行测试和语法检查**

Run:

```bash
uv run pytest tests/test_codex_runtime_scripts.py -q
bash -n scripts/deploy_remote_codex.sh scripts/doctor_codex_runtime.sh
```

Expected: PASS and no shell syntax output.

- [ ] **Step 5: 提交**

```bash
git add deploy.remote.example.env .gitignore scripts/deploy_remote_codex.sh scripts/doctor_codex_runtime.sh
git commit -m "feat: add pull-based remote codex deployment"
```

---

### Task 8: 文档漂移测试

**Files:**
- Create: `tests/test_docs_codex_migration.py`
- Modify later: `README.md`
- Modify later: `docs/codex_automations.md`
- Modify later: `docs/card_validator_enforce_switch.md`

- [ ] **Step 1: 写失败测试**

Create `tests/test_docs_codex_migration.py`:

```python
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_readme_describes_codex_as_short_llm_scheduler():
    text = read("README.md")

    assert "Codex automations" in text
    assert "短时 LLM" in text
    assert "launchd 运行长时 daemon" in text
    assert "com.user.stockpremarket.plist" not in text
    assert "bash scripts/install_codex_automations.sh" in text
    assert "bash scripts/install_runtime_services.sh" in text


def test_codex_runbook_documents_remote_pull_deploy():
    text = read("docs/codex_automations.md")

    assert "pull-based" in text
    assert "runtime host" in text
    assert "deploy.remote.env" in text
    assert "scripts/deploy_remote_codex.sh" in text
    assert "scripts/doctor_codex_runtime.sh" in text
    assert "launchd/disabled/claude/" in text


def test_validator_doc_mentions_codex_strategy_not_only_launchd():
    text = read("docs/card_validator_enforce_switch.md")

    assert "Codex automation" in text
    assert "launchd daemon" in text
    assert "short LLM" in text
    assert "launchd/com.user.stockweekly.plist" not in text
```

- [ ] **Step 2: 运行测试，确认失败**

Run:

```bash
uv run pytest tests/test_docs_codex_migration.py -q
```

Expected: FAIL，README 和 validator 文档仍是旧口径。

- [ ] **Step 3: 提交失败测试**

```bash
git add tests/test_docs_codex_migration.py
git commit -m "test: prevent docs drift from codex migration"
```

---

### Task 9: 更新 README 和 Codex automation runbook

**Files:**
- Modify: `README.md`
- Modify: `docs/codex_automations.md`
- Test: `tests/test_docs_codex_migration.py`

- [ ] **Step 1: 更新 README 调度章节**

In `README.md`, replace the `## 调度` section with:

```markdown
## 调度

本项目把调度分成两类：

- **Codex automations**：短时 LLM jobs，包括 L1 盘前、L2 盘中 4 时点、L4 盘后、周复盘。
- **launchd 运行长时 daemon**：不需要 LLM 长时间驻留的 watcher，包括 watch_loop、anomaly_loop、theme_loop，以及可选 tg_listener。

| 类型 | 任务 | 时间 | 入口 |
|------|------|------|------|
| Codex automation | L1 盘前 | 工作日 08:30 | `stock-premarket` |
| Codex automation | L2 开盘纪律 | 工作日 09:30 | `stock-intraday-09-30` |
| Codex automation | L2 关键时段 | 工作日 09:45 | `stock-intraday-09-45` |
| Codex automation | L2 半日叙事 | 工作日 11:30 | `stock-intraday-11-30` |
| Codex automation | L2 尾盘叙事 | 工作日 14:30 | `stock-intraday-14-30` |
| Codex automation | L4 盘后 | 工作日 15:35 | `stock-postmarket` |
| Codex automation | 周复盘 | 周日 21:00 | `stock-weekly-review` |
| launchd daemon | watch_loop | 工作日 09:25-15:00 | `com.user.stockwatchloop` |
| launchd daemon | anomaly_loop | 工作日 09:25-15:00 | `com.user.stockanomalyloop` |
| launchd daemon | theme_loop | 工作日 09:25-15:00 | `com.user.stockthemeloop` |

Codex automations 必须安装在实际运行交易 workflow 的 runtime host 上。开发机可以通过 SSH 触发远程部署，但不承担长时运行职责。
```

- [ ] **Step 2: 更新 README 安装章节**

Change install commands to include:

```markdown
### Runtime host 安装

```bash
uv sync --group dev
sqlite3 data/daily.db < code/init_db.sql
uv run python code/refresh_calendar.py
bash scripts/sync_codex_skills.sh
bash scripts/install_codex_automations.sh
bash scripts/install_runtime_services.sh
bash scripts/doctor_codex_runtime.sh
```

### 开发机触发远程 staging 部署

```bash
cp deploy.remote.example.env deploy.remote.env
# 编辑 deploy.remote.env
bash scripts/deploy_remote_codex.sh
```
```

- [ ] **Step 3: 扩展 `docs/codex_automations.md`**

Replace the file with a runbook containing these sections:

```markdown
# Codex Automations Runbook

## 部署模型

本项目使用 pull-based runtime host 部署。runtime host 自己 clone/pull 仓库，并在本机安装 Codex automations。

## Runtime host 本地安装

```bash
bash scripts/setup.sh
bash scripts/sync_codex_skills.sh
bash scripts/install_codex_automations.sh
bash scripts/install_runtime_services.sh
bash scripts/disable_legacy_claude_launchd.sh
bash scripts/doctor_codex_runtime.sh
```

## 开发机 SSH 触发远程部署

```bash
cp deploy.remote.example.env deploy.remote.env
bash scripts/deploy_remote_codex.sh
```

`deploy.remote.env` 不入库。

## Active Codex Jobs

| Job ID | Schedule | Task |
|---|---:|---|
| `stock-premarket` | Mon-Fri 08:30 | Run `stock-premarket` and push Telegram |
| `stock-intraday-09-30` | Mon-Fri 09:30 | Run `stock-intraday` current-time branch |
| `stock-intraday-09-45` | Mon-Fri 09:45 | Run `stock-intraday` current-time branch |
| `stock-intraday-11-30` | Mon-Fri 11:30 | Run `stock-intraday` current-time branch |
| `stock-intraday-14-30` | Mon-Fri 14:30 | Run `stock-intraday` current-time branch |
| `stock-postmarket` | Mon-Fri 15:35 | Run `stock-postmarket` and refresh daily data |
| `stock-weekly-review` | Sun 21:00 | Run `stock-weekly` |

## What Still Uses launchd

launchd 只保留长时 daemon：`stockwatchloop`、`stockanomalyloop`、`stockthemeloop`，以及 opt-in 的 `stocktglistener`。

## Legacy Claude launchd

旧 short LLM launchd templates 保留在 `launchd/disabled/claude/`，只作为 fallback。默认安装不会扫描这个目录。

## Verification

```bash
bash scripts/doctor_codex_runtime.sh
```
```

- [ ] **Step 4: 运行文档测试**

Run:

```bash
uv run pytest tests/test_docs_codex_migration.py -q
```

Expected: PASS.

- [ ] **Step 5: 提交**

```bash
git add README.md docs/codex_automations.md
git commit -m "docs: document codex runtime deployment"
```

---

### Task 10: 更新 validator 文档

**Files:**
- Modify: `docs/card_validator_enforce_switch.md`
- Test: `tests/test_docs_codex_migration.py`

- [ ] **Step 1: 替换旧 launchd-only 操作说明**

Edit `docs/card_validator_enforce_switch.md`:

- Keep the mode definitions.
- Replace “切换 enforce 操作（6 处统一改）” with:

```markdown
## 切换 enforce 操作

### Codex automation short LLM jobs

短时 LLM jobs 由 Codex automation 触发，不再通过 short-job launchd plist 注入环境变量。

当前默认策略仍是 `warn`，因为 `push.py` 和 `tg_listener.py` 在 env 不设时默认 warn。切换 enforce 前，需要在 implementation 中选择一种项目级策略：

1. 在 automation prompt 中明确 validator mode 期望；
2. 或新增项目级 runtime config，由 push.py 读取；
3. 或在 Codex automation 支持环境配置后，通过 automation 配置注入。

切换后必须重新运行：

```bash
bash scripts/install_codex_automations.sh
bash scripts/doctor_codex_runtime.sh
```

### launchd daemon

长时 launchd daemon 仍可通过 shell 脚本里的 `CARD_VALIDATOR_MODE` 控制，例如 tg_listener、anomaly_loop。
```

- Remove references to `launchd/com.user.stockweekly.plist`.

- [ ] **Step 2: 运行文档测试**

Run:

```bash
uv run pytest tests/test_docs_codex_migration.py -q
```

Expected: PASS.

- [ ] **Step 3: 提交**

```bash
git add docs/card_validator_enforce_switch.md
git commit -m "docs: clarify validator mode for codex automations"
```

---

### Task 11: Skill 契约硬化

**Files:**
- Modify: `.claude/skills/stock-premarket/SKILL.md`
- Modify: `.claude/skills/stock-intraday/SKILL.md`
- Modify: `.claude/skills/stock-postmarket/SKILL.md`
- Modify: `.claude/skills/stock-weekly/SKILL.md`
- Test: `tests/test_codex_skill_contracts.py`

- [ ] **Step 1: 新增 skill contract 静态测试**

Create `tests/test_codex_skill_contracts.py`:

```python
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

SCHEDULED_SKILLS = {
    "stock-premarket": ["data/last_card.md", "push.py", "失败"],
    "stock-intraday": ["data/last_intraday_card.md", "push.py", "PREMARKET_MISSING"],
    "stock-postmarket": ["data/last_postmarket_card.md", "push.py", "refresh_stock_basic.py"],
    "stock-weekly": ["data/weekly_review", "data/last_weekly_card.md", "push.py"],
}


def skill_text(skill: str) -> str:
    return (ROOT / ".claude" / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")


def test_scheduled_skills_define_required_outputs_and_failures():
    for skill, required_terms in SCHEDULED_SKILLS.items():
        text = skill_text(skill)
        assert "Codex automation" in text
        assert "无人值守" in text
        assert "不要只回复完成" in text
        assert "失败" in text
        for term in required_terms:
            assert term in text


def test_scheduled_skills_do_not_tell_codex_to_call_claude_wrapper():
    for skill in SCHEDULED_SKILLS:
        text = skill_text(skill)
        assert "claude -p" not in text
        assert "run_premarket.sh" not in text
        assert "run_intraday.sh" not in text
        assert "run_postmarket.sh" not in text
```

- [ ] **Step 2: 运行测试，确认失败**

Run:

```bash
uv run pytest tests/test_codex_skill_contracts.py -q
```

Expected: FAIL，skills 还没有统一 Codex automation 契约。

- [ ] **Step 3: 在每个 scheduled skill 顶部加入统一契约段**

Add near the top of each listed `SKILL.md`:

```markdown
## Codex automation 契约

本 skill 会被 Codex automation 无人值守触发。执行时必须产出下面列出的文件和推送副作用；不要只回复“完成”。如果任一步骤失败，必须说明具体失败步骤，并停止声称成功。
```

Then add skill-specific bullets:

- `stock-premarket`:

```markdown
- 必须生成 fact pack，并只使用 allowed facts。
- 必须写入 `data/last_card.md`。
- 必须通过 `.agents/skills/stock-premarket/scripts/push.py` 推送。
- 非交易日、数据源失败、解禁检查失败、Telegram 失败时必须报告具体原因。
```

- `stock-intraday`:

```markdown
- 必须根据当前系统时间进入 09:30、09:45、11:30 或 14:30 分支。
- 必须写入 `data/last_intraday_card.md`。
- 必须通过 `.agents/skills/stock-premarket/scripts/push.py --source stock-intraday` 推送。
- 遇到 `PREMARKET_MISSING` 时必须降级报告，不要编造观察池。
```

- `stock-postmarket`:

```markdown
- 必须写入 `data/last_postmarket_card.md`。
- 必须通过 `.agents/skills/stock-premarket/scripts/push.py --source stock-postmarket` 推送。
- 主流程完成后必须刷新 `stock_basic`；刷新失败只报告为副作用失败，不掩盖盘后主流程结果。
```

- `stock-weekly`:

```markdown
- 必须先检查本周 `data/weekly_review/YYYY-WW.md` 是否已存在。
- 已存在且未 force 时必须跳过并报告，不重复推送。
- 需要生成周报时必须写入 `data/weekly_review/YYYY-WW.md` 和 `data/last_weekly_card.md`。
- 必须通过 `.agents/skills/stock-premarket/scripts/push.py --source stock-weekly` 推送摘要。
```

- [ ] **Step 4: 运行测试**

Run:

```bash
uv run pytest tests/test_codex_skill_contracts.py -q
```

Expected: PASS.

- [ ] **Step 5: 同步 Codex skills 并确认无残留运行路径问题**

Run:

```bash
bash scripts/sync_codex_skills.sh
rg -n "run_premarket.sh|run_intraday.sh|run_postmarket.sh|claude -p" .agents/skills/stock-premarket .agents/skills/stock-intraday .agents/skills/stock-postmarket .agents/skills/stock-weekly
```

Expected: `rg` exits 1 with no matches for legacy wrappers in scheduled skills.

- [ ] **Step 6: 提交**

```bash
git add .claude/skills/stock-premarket/SKILL.md .claude/skills/stock-intraday/SKILL.md .claude/skills/stock-postmarket/SKILL.md .claude/skills/stock-weekly/SKILL.md tests/test_codex_skill_contracts.py
git commit -m "docs: harden scheduled skill codex contracts"
```

---

### Task 12: 全量验证和最终整理

**Files:**
- Verify all changed files

- [ ] **Step 1: 运行 shell 语法检查**

Run:

```bash
bash -n scripts/setup.sh \
  scripts/install_launchd.sh \
  scripts/install_runtime_services.sh \
  scripts/install_codex_automations.sh \
  scripts/sync_codex_skills.sh \
  scripts/deploy_remote_codex.sh \
  scripts/doctor_codex_runtime.sh \
  scripts/disable_legacy_claude_launchd.sh
```

Expected: no output, exit 0.

- [ ] **Step 2: 运行 dry-run automation 生成**

Run:

```bash
tmpdir="$(mktemp -d)"
CODEX_AUTOMATIONS_DIR="$tmpdir" bash scripts/install_codex_automations.sh --dry-run
find "$tmpdir" -maxdepth 2 -name automation.toml | sort
rm -rf "$tmpdir"
```

Expected: output lists 7 automation TOML files.

- [ ] **Step 3: 运行测试套件**

Run:

```bash
uv run pytest tests/
```

Expected: PASS.

- [ ] **Step 4: 检查 README/docs 旧入口漂移**

Run:

```bash
rg -n "com.user.stockpremarket.plist|com.user.stockintraday.plist|com.user.stockpostmarket.plist|com.user.stockweekly.plist|Claude Code CLI（claude|claude -p" README.md docs scripts .claude/skills
```

Expected: Any matches are in legacy/fallback context only. If a match describes default production scheduling, edit the file and rerun.

- [ ] **Step 5: 检查 git diff**

Run:

```bash
git status --short
git diff --stat
```

Expected: only intended migration files are modified.

- [ ] **Step 6: 最终提交**

If prior tasks were committed individually, create a final cleanup commit only if there are remaining changes:

```bash
git add README.md docs scripts tests code .claude .gitignore deploy.remote.example.env
git commit -m "chore: finalize codex runtime deployment migration"
```

If there are no remaining changes, skip this commit and record the latest task commit as final.

