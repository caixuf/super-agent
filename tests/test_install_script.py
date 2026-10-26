"""install/install.sh 的端到端契约：插件落位 + 两条补丁接线 + 幂等。

只写 pytest 的 ``tmp_path``：``--hermes-home`` 指向临时目录，绝不触碰真实 profile。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL = REPO_ROOT / "install" / "install.sh"

#: 与源目录 snake_case 对应的落位名（现场与 config 的启用名都是连字符形态）。
PLUGIN_NAMES = ("durable-dispatch", "illustrated-delivery", "localvec", "longtask-guard")


def _run(home: Path, *extra: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # 隔离宿主环境：外部 profile/业务技能开关不得把安装器带偏。
    for key in ("HERMES_HOME", "HERMES_AGENT_DIR", "INSTALL_BUSINESS_SKILLS",
                "BUSINESS_SKILLS_SRC"):
        env.pop(key, None)
    return subprocess.run(
        ["bash", str(INSTALL), "--hermes-home", str(home), *extra],
        cwd=REPO_ROOT, env=env, text=True, capture_output=True, check=False,
    )


def test_dry_run_plans_plugins_and_both_patch_scripts(tmp_path):
    home = tmp_path / "home"
    proc = _run(home, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout

    assert "[plan]" in out
    # 插件按连字符名落位（源目录是下划线）。
    for name in PLUGIN_NAMES:
        assert f"plugins/{name}" in out, name
    assert "plugins/longtask_guard/" in out  # 源路径出现在计划里
    # 两个补丁脚本都必须出现在计划里，且都是先 --check。
    assert "apply_feishu_card_replies.py --check" in out
    assert "patch_card_finalize.py --check" in out
    assert "patch_card_finalize.py --apply" in out

    # dry-run 只打印，不建目录、不写文件。
    assert not home.exists()


def test_real_install_lands_hyphenated_plugin_dirs(tmp_path):
    home = tmp_path / "home"
    proc = _run(home, "--skip-patch")
    assert proc.returncode == 0, proc.stderr

    plugins = home / "plugins"
    for name in PLUGIN_NAMES:
        assert (plugins / name / "__init__.py").is_file(), name
    assert (plugins / "longtask-guard" / "plugin.yaml").is_file()
    # 不按下划线形态落位。
    assert not (plugins / "longtask_guard").exists()
    # install_tree 的排除规则生效。
    assert not list(plugins.rglob("__pycache__"))
    assert not list(plugins.rglob("*.pyc"))


def test_second_install_is_idempotent(tmp_path):
    home = tmp_path / "home"
    first = _run(home, "--skip-patch")
    assert first.returncode == 0, first.stderr

    second = _run(home, "--skip-patch")
    assert second.returncode == 0, second.stderr
    out = second.stdout

    # 第二次不应再产生任何拷贝/安装动作。
    assert "[plan]" not in out
    assert "[OK] 部署" not in out
    assert "[OK] 安装技能" not in out
    assert "[--] 已一致" in out

    # 插件文件逐条走「已一致」分支。
    plugin_lines = [line for line in out.splitlines() if "/plugins/" in line]
    assert plugin_lines
    assert all("[--] 已一致" in line for line in plugin_lines)


def test_symlinked_plugin_target_is_replaced_without_writing_through(tmp_path):
    """现场可能把插件目录做成指向开发仓的符号链接；安装器不得透过它写回去。"""
    home = tmp_path / "home"
    plugins = home / "plugins"
    plugins.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside_plugin = outside / "longtask_guard"
    outside_plugin.mkdir(parents=True)
    sentinel = outside_plugin / "sentinel.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    (plugins / "longtask-guard").symlink_to(outside_plugin, target_is_directory=True)

    proc = _run(home, "--skip-patch")
    assert proc.returncode == 0, proc.stderr
    assert "目标原是符号链接" in proc.stdout
    assert not (plugins / "longtask-guard").is_symlink()
    assert (plugins / "longtask-guard" / "__init__.py").is_file()

    # 链接目标原封不动：没有在别的仓里多出文件，也没有改内容。
    assert sorted(p.name for p in outside_plugin.iterdir()) == ["sentinel.txt"]
    assert sentinel.read_text(encoding="utf-8") == "untouched"
