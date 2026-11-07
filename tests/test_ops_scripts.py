"""scripts/ops 两个官方运维脚本的布局感知契约。

真相源判定规则必须与 ``agentcore/profile_spec.py:get_engine_root()`` 同源：
显式注入 > 受管 ``current`` > ``${HERMES_HOME}/agentcore``；真相源确实不存在时
仍然拒绝继续。

只写 pytest 的 ``tmp_path``：整个沙箱在临时 HOME 里，``systemctl`` 被桩替换，
绝不触碰真实 profile 与真实 systemd 会话。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_DIR = REPO_ROOT / "scripts" / "ops"
UNINSTALL = OPS_DIR / "uninstall-legacy.sh"
PURGE = OPS_DIR / "purge_and_rebuild_host.sh"


def _sandbox(tmp_path: Path) -> dict[str, Path]:
    """搭一套隔离的 HOME：工作区、受管根、hermes home、systemd 用户目录。"""
    home = tmp_path / "home"
    (home / "code" / "super-agent").mkdir(parents=True)
    (home / ".config" / "systemd" / "user").mkdir(parents=True)
    (home / ".hermes").mkdir(parents=True)
    (home / "super_agent").mkdir(parents=True)
    # systemctl 桩：脚本里的 daemon-reload / stop / disable 不许碰到真实会话
    stub_bin = home / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "systemctl"
    stub.write_text('#!/usr/bin/env bash\nexit 0\n', encoding="utf-8")
    stub.chmod(0o755)
    return {
        "home": home,
        "worktree": home / "code" / "super-agent",
        "hermes_home": home / ".hermes",
        "managed_root": home / "super_agent",
        "systemd_dir": home / ".config" / "systemd" / "user",
        "stub_bin": stub_bin,
    }


def _run(script: Path, box: dict[str, Path], *args: str,
         env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for key in ("HERMES_HOME", "HERMES_AGENT_DIR", "AGENTCORE_ENGINE_ROOT",
                "AGENTCORE_HERMES_TREE", "AGENTCORE_PROFILE_NAME",
                "AGENTCORE_ROOT", "AGENTCORE_WORKTREE_REPO", "AGENTCORE_INSTALLER",
                "AGENTCORE_HERMES_HOME"):
        env.pop(key, None)
    env["HOME"] = str(box["home"])
    env["PATH"] = f"{box['stub_bin']}:{env.get('PATH', '')}"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(script), *args], cwd=REPO_ROOT, env=env, text=True,
        capture_output=True, check=False,
    )


def _unit_with_worktree_pythonpath(box: dict[str, Path], name: str) -> Path:
    unit = box["systemd_dir"] / name
    unit.write_text(
        "[Unit]\nDescription=test\n\n[Service]\n"
        f"Environment=\"PYTHONPATH={box['worktree']}:{box['home']}/.config/agent-pool\"\n"
        "ExecStart=/usr/bin/python3 -m agentcore.pool\n",
        encoding="utf-8",
    )
    return unit


# --------------------------------------------------------------------------- #
# 真相源解析
# --------------------------------------------------------------------------- #

def test_install_sh_layout_resolves_hermes_home_agentcore(tmp_path):
    """单写者布局：真相源 = ${HERMES_HOME}/agentcore。"""
    box = _sandbox(tmp_path)
    (box["hermes_home"] / "agentcore").mkdir()

    proc = _run(UNINSTALL, box)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "布局=install.sh" in proc.stdout
    assert f"真相源={box['hermes_home'] / 'agentcore'}" in proc.stdout


def test_managed_layout_resolves_managed_current(tmp_path):
    """受管布局：真相源 = ${AGENTCORE_ROOT}/<profile>/current。"""
    box = _sandbox(tmp_path)
    current = box["managed_root"] / "default" / "current"
    current.mkdir(parents=True)

    proc = _run(UNINSTALL, box)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "布局=managed" in proc.stdout
    assert f"真相源={current}" in proc.stdout


def test_no_truth_source_refuses_to_continue(tmp_path):
    """两个候选都不在 → 拒绝继续（不放宽官方的安全边界）。"""
    box = _sandbox(tmp_path)

    proc = _run(UNINSTALL, box)

    assert proc.returncode == 1
    assert "真相源不存在，拒绝继续" in proc.stderr


def test_explicit_engine_root_wins_over_both_candidates(tmp_path):
    """显式注入优先于受管 current 与 ${HERMES_HOME}/agentcore。"""
    box = _sandbox(tmp_path)
    (box["hermes_home"] / "agentcore").mkdir()
    (box["managed_root"] / "default" / "current").mkdir(parents=True)
    explicit = box["home"] / "explicit-engine"
    explicit.mkdir()

    proc = _run(UNINSTALL, box, env_extra={"AGENTCORE_ENGINE_ROOT": str(explicit)})

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "布局=explicit" in proc.stdout
    assert f"真相源={explicit}" in proc.stdout


def test_explicit_engine_root_missing_refuses(tmp_path):
    """显式注入指向不存在的路径 → 拒绝（不许用兜底掩盖配置错误）。"""
    box = _sandbox(tmp_path)
    (box["hermes_home"] / "agentcore").mkdir()

    proc = _run(UNINSTALL, box,
                env_extra={"AGENTCORE_ENGINE_ROOT": str(box["home"] / "nope")})

    assert proc.returncode == 1
    assert "显式注入的真相源不存在" in proc.stderr


# --------------------------------------------------------------------------- #
# 漂移对账与纠偏
# --------------------------------------------------------------------------- #

def test_worktree_pythonpath_is_reported_as_drift(tmp_path):
    box = _sandbox(tmp_path)
    (box["hermes_home"] / "agentcore").mkdir()
    unit = _unit_with_worktree_pythonpath(box, "agentcore-worker@.service")

    proc = _run(UNINSTALL, box)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[DRIFT] 服务代码来源指向工作区" in proc.stdout
    assert unit.name in proc.stdout
    assert str(box["worktree"]) in unit.read_text(encoding="utf-8"), "只读模式不得改文件"


def test_worktree_pythonpath_with_home_specifier_is_drift(tmp_path):
    """unit 里写 %h/code/super-agent（systemd 展开符）同样算指向工作区。"""
    box = _sandbox(tmp_path)
    (box["hermes_home"] / "agentcore").mkdir()
    unit = box["systemd_dir"] / "agentcore-worker@.service"
    unit.write_text(
        "[Unit]\nDescription=test\n\n[Service]\n"
        "WorkingDirectory=%h/code/super-agent\n"
        "Environment=\"PYTHONPATH=%h/code/super-agent:%h/.config/agent-pool\"\n",
        encoding="utf-8",
    )

    proc = _run(UNINSTALL, box)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[DRIFT] 服务代码来源指向工作区" in proc.stdout


def test_apply_rewrites_working_directory_to_truth_source(tmp_path):
    """WorkingDirectory 指向工作区同样算漂移：只修 PYTHONPATH 不够。"""
    box = _sandbox(tmp_path)
    engine = box["hermes_home"] / "agentcore"
    engine.mkdir()
    unit = box["systemd_dir"] / "agentcore-worker@.service"
    unit.write_text(
        "[Service]\n"
        "WorkingDirectory=%h/code/super-agent\n"
        "Environment=\"PYTHONPATH=%h/code/super-agent:%h/.config/agent-pool\"\n",
        encoding="utf-8",
    )

    proc = _run(UNINSTALL, box, "--apply")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = unit.read_text(encoding="utf-8")
    assert "%h/code/super-agent" not in text
    assert f"WorkingDirectory={engine}" in text
    assert f"PYTHONPATH={engine}" in text


def test_apply_rewrites_home_specifier_form(tmp_path):
    box = _sandbox(tmp_path)
    engine = box["hermes_home"] / "agentcore"
    engine.mkdir()
    unit = box["systemd_dir"] / "agentcore-worker@.service"
    unit.write_text(
        "[Service]\nEnvironment=\"PYTHONPATH=%h/code/super-agent:%h/.config/agent-pool\"\n",
        encoding="utf-8",
    )

    proc = _run(UNINSTALL, box, "--apply")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = unit.read_text(encoding="utf-8")
    assert "%h/code/super-agent" not in text
    assert f"PYTHONPATH={engine}" in text


def test_apply_rewrites_pythonpath_to_truth_source_and_backs_up(tmp_path):
    box = _sandbox(tmp_path)
    engine = box["hermes_home"] / "agentcore"
    engine.mkdir()
    unit = _unit_with_worktree_pythonpath(box, "agentcore-worker@.service")

    proc = _run(UNINSTALL, box, "--apply")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = unit.read_text(encoding="utf-8")
    assert str(box["worktree"]) not in text
    assert f"PYTHONPATH={engine}" in text
    backups = list((box["managed_root"] / "default" / "state" / "drift-backups").glob("backup_*/agentcore-worker@.service"))
    assert backups, "纠偏前必须备份原 unit"


def test_single_config_layout_reports_no_ambiguity(tmp_path):
    """单写者布局只有一份 config.yaml，不得误报「两份配置不同」。"""
    box = _sandbox(tmp_path)
    (box["hermes_home"] / "agentcore").mkdir()
    (box["hermes_home"] / "config.yaml").write_text("plugins: {}\n", encoding="utf-8")

    proc = _run(UNINSTALL, box)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "两份 config.yaml 内容不同" not in proc.stdout
    assert "只有一份 config.yaml" in proc.stdout


def test_managed_layout_still_detects_two_different_configs(tmp_path):
    """受管布局的「两份配置不同」判定必须保留（回归护栏）。"""
    box = _sandbox(tmp_path)
    (box["managed_root"] / "default" / "current").mkdir(parents=True)
    (box["hermes_home"] / "config.yaml").write_text("plugins: {}\n", encoding="utf-8")
    profile_dir = box["hermes_home"] / "profiles" / "default"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text("plugins: {enabled: []}\n", encoding="utf-8")

    proc = _run(UNINSTALL, box)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "两份 config.yaml 内容不同" in proc.stdout


def test_install_sh_layout_verifies_with_official_readiness_gate(tmp_path):
    """单写者布局的验证方式是官方就绪门禁，不是受管 doctor。"""
    box = _sandbox(tmp_path)
    (box["hermes_home"] / "agentcore").mkdir()

    proc = _run(UNINSTALL, box)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "验证方式=官方就绪门禁" in proc.stdout
    assert "验证方式=受管 doctor" not in proc.stdout


def test_managed_layout_verifies_with_managed_doctor(tmp_path):
    box = _sandbox(tmp_path)
    (box["managed_root"] / "default" / "current").mkdir(parents=True)

    proc = _run(UNINSTALL, box)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "验证方式=受管 doctor" in proc.stdout


# --------------------------------------------------------------------------- #
# purge_and_rebuild_host.sh
# --------------------------------------------------------------------------- #

def test_purge_dry_run_uses_official_installer_for_install_sh_layout(tmp_path):
    """单写者布局的重建交回官方 install.sh（单一写者）。"""
    box = _sandbox(tmp_path)
    (box["hermes_home"] / "agentcore").mkdir()
    installer = box["worktree"] / "install" / "install.sh"
    installer.parent.mkdir(parents=True)
    installer.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")

    proc = _run(PURGE, box)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "布局=install.sh" in proc.stdout
    assert "官方 install.sh 重建" in proc.stdout
    assert "受管 update + doctor" not in proc.stdout


def test_purge_dry_run_keeps_managed_flow_for_managed_layout(tmp_path):
    box = _sandbox(tmp_path)
    (box["managed_root"] / "default" / "current").mkdir(parents=True)

    proc = _run(PURGE, box)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "布局=managed" in proc.stdout
    assert "受管 update + doctor" in proc.stdout


def test_purge_refuses_without_truth_source(tmp_path):
    box = _sandbox(tmp_path)

    proc = _run(PURGE, box)

    assert proc.returncode == 1
    assert "真相源不存在，拒绝继续" in proc.stderr


def test_archive_list_skips_paths_referenced_by_live_config(tmp_path):
    """现场配置指向的路径不许进归档清单（归档死状态，不归档活状态）。"""
    box = _sandbox(tmp_path)
    (box["hermes_home"] / "agentcore").mkdir()
    liveness = box["home"] / ".local" / "state" / "agentcore" / "liveness.db"
    liveness.parent.mkdir(parents=True)
    liveness.write_text("sqlite", encoding="utf-8")
    (box["hermes_home"] / "config.yaml").write_text(
        f"plugins:\n  entries:\n    durable-dispatch:\n      settings:\n"
        f"        liveness_db: {liveness}\n",
        encoding="utf-8",
    )

    proc = _run(PURGE, box)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert str(liveness) not in proc.stdout
