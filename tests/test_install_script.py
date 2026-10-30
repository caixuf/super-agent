"""install/install.sh 的端到端契约：插件落位 + 启用记录 + 两条补丁接线 + 幂等。

只写 pytest 的 ``tmp_path``：``--hermes-home`` 指向临时目录，绝不触碰真实 profile。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL = REPO_ROOT / "install" / "install.sh"

#: 与源目录 snake_case 对应的落位名（现场与 config 的启用名都是连字符形态）。
PLUGIN_NAMES = ("capability-alert-outbox", "colleague-catalog", "colleague-team",
                "durable-dispatch", "illustrated-delivery", "local-model-grounding",
                "localvec", "longtask-guard", "markdown-delivery", "memory-bridge",
                "task-progress", "task-status", "write-guard")

#: 带 plugin.yaml 的插件才写启用记录；localvec 没有 manifest，是 memory provider。
ENABLE_NAMES = ("capability-alert-outbox", "colleague-catalog", "colleague-team",
                "durable-dispatch", "illustrated-delivery", "local-model-grounding",
                "longtask-guard", "markdown-delivery", "memory-bridge", "task-progress",
                "task-status", "write-guard")


def _run(home: Path, *extra: str, path_prefix: Path | None = None,
         env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # 隔离宿主环境：外部 profile/业务技能/启用模式开关不得把安装器带偏。
    for key in ("HERMES_HOME", "HERMES_AGENT_DIR", "INSTALL_BUSINESS_SKILLS",
                "BUSINESS_SKILLS_SRC", "PLUGIN_ENABLE_MODE"):
        env.pop(key, None)
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}:{env.get('PATH', '')}"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(INSTALL), "--hermes-home", str(home), *extra],
        cwd=REPO_ROOT, env=env, text=True, capture_output=True, check=False,
    )


def _plugin_list(text: str, key: str) -> list[str] | None:
    """Read ``plugins.<key>`` the way the installer writes it (2-space child indent).

    Deliberately a tiny line reader instead of a YAML dependency: the test asserts the
    textual contract the installer promises, including that it did not reformat the file.
    Inside ``plugins.entries.*.settings`` there is another ``enabled:`` key, so only the
    2-space child is considered. Returns None when the key is absent.
    """
    lines = text.splitlines()
    header = f"  {key}:"
    for index, line in enumerate(lines):
        if not line.startswith(header) or line.startswith("    "):
            continue
        inline = line[len(header):].strip()
        if inline.startswith("["):
            if inline == "[]":
                return []
            return [part.strip().strip("\"'") for part in inline.strip("[]").split(",")]
        found: list[str] = []
        for follower in lines[index + 1:]:
            if not follower.strip() or follower.lstrip().startswith("#"):
                continue
            if follower.lstrip().startswith("- "):
                found.append(follower.lstrip()[2:].strip().strip("\"'"))
                continue
            break
        return found
    return None


# --------------------------------------------------------------------------- #
# 落位（既有契约）
# --------------------------------------------------------------------------- #
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
    # 启用记录也进计划，但 dry-run 一个文件都不建。
    assert "plugins.enabled" in out
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

    # generic skills land under the target profile; examples stay templates
    skills = home / "skills"
    assert (skills / "research-before-build" / "SKILL.md").is_file()
    assert (skills / "im-card-markdown" / "SKILL.md").is_file()
    assert not (skills / "examples").exists(), "examples are templates, never installed"

    agentcore = home / "agentcore"
    for relative in ("tools/arch_doc.py", "tools/doc_browser.py",
                     "tools/assets/mermaid.min.js",
                     "patches/patch_card_finalize.py"):
        assert (agentcore / relative).is_file(), relative


# --------------------------------------------------------------------------- #
# 启用记录
# --------------------------------------------------------------------------- #
def test_fresh_install_enables_manifest_plugins(tmp_path):
    home = tmp_path / "home"
    proc = _run(home, "--skip-patch")
    assert proc.returncode == 0, proc.stderr

    config = home / "config.yaml"
    assert config.is_file()
    text = config.read_text(encoding="utf-8")
    assert _plugin_list(text, "enabled") == list(ENABLE_NAMES)
    assert _plugin_list(text, "disabled") in (None, [])
    # localvec 没有 manifest：它不是 plugins.enabled 项。
    assert "localvec" not in text
    for name in ENABLE_NAMES:
        assert f"[OK] 启用 {name}" in proc.stdout


def test_second_install_is_idempotent(tmp_path):
    home = tmp_path / "home"
    first = _run(home, "--skip-patch")
    assert first.returncode == 0, first.stderr

    config = home / "config.yaml"
    before = config.read_bytes()
    second = _run(home, "--skip-patch")
    assert second.returncode == 0, second.stderr
    out = second.stdout

    # 第二次不应再产生任何拷贝/安装/启用动作，配置逐字节不变。
    assert "[plan]" not in out
    assert "[OK] 部署" not in out
    assert "[OK] 安装技能" not in out
    assert "[OK] 启用" not in out
    assert "[--] 已一致" in out
    for name in ENABLE_NAMES:
        assert f"[--] 已启用 {name}" in out
    assert config.read_bytes() == before
    assert _plugin_list(config.read_text(encoding="utf-8"), "enabled") == list(ENABLE_NAMES)

    # 插件文件逐条走「已一致」分支。
    plugin_lines = [line for line in out.splitlines() if "/plugins/" in line]
    assert plugin_lines
    assert all("[--] 已一致" in line for line in plugin_lines)


def test_existing_config_keys_secrets_and_entries_preserved(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    config = home / "config.yaml"
    original = """# keep this comment
model:
  provider: custom
  api_key: "sk-live-SECRET-do-not-touch"
plugins:
  disabled:
  - longtask-guard
  - some-other-plugin
  enabled:
  - existing-plugin
  entries:
    durable-dispatch:
      settings:
        enabled: true
        engine_root: /opt/canonical-engine
        queue_root: /var/lib/analysis-queue
platform_toolsets:
  cli:
  - terminal
"""
    config.write_text(original, encoding="utf-8")

    proc = _run(home, "--skip-patch")
    assert proc.returncode == 0, proc.stderr
    text = config.read_text(encoding="utf-8")

    # 密钥、注释、entries/settings、其它键全部原样保留。
    assert "sk-live-SECRET-do-not-touch" in text
    assert "# keep this comment" in text
    assert "engine_root: /opt/canonical-engine" in text
    assert "queue_root: /var/lib/analysis-queue" in text
    assert "platform_toolsets:" in text and "- terminal" in text
    # 只增删两个启用列表：保留既有项顺序，追加缺失项；显式 disable 的项被移出。
    assert _plugin_list(text, "enabled") == ["existing-plugin", *ENABLE_NAMES]
    assert _plugin_list(text, "disabled") == ["some-other-plugin"]
    # 写入前留了可回滚的备份。
    backups = list(home.glob("config.yaml.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original


def test_null_plugins_value_is_replaced_with_enable_list(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    config = home / "config.yaml"
    config.write_text("model:\n  api_key: keep-me\nplugins: null\n", encoding="utf-8")

    proc = _run(home, "--skip-patch")
    assert proc.returncode == 0, proc.stderr
    text = config.read_text(encoding="utf-8")
    assert "api_key: keep-me" in text
    assert _plugin_list(text, "enabled") == list(ENABLE_NAMES)


def test_missing_plugins_key_is_appended_without_touching_other_keys(tmp_path):
    """现场 config 往往整份没有 plugins: 段（只有默认键）；只能追加，不能重写。"""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    config = home / "config.yaml"
    original = "# keep\nmodel:\n  api_key: sk-keep\nmemory:\n  provider: x\n"
    config.write_text(original, encoding="utf-8")

    proc = _run(home, "--skip-patch")
    assert proc.returncode == 0, proc.stderr
    text = config.read_text(encoding="utf-8")
    assert text.startswith(original)  # 原有内容逐字节在前
    assert "sk-keep" in text
    assert _plugin_list(text, "enabled") == list(ENABLE_NAMES)

    before = config.read_bytes()
    assert _run(home, "--skip-patch").returncode == 0
    assert config.read_bytes() == before


def test_cli_mode_never_falls_back_to_text_merge(tmp_path):
    """PLUGIN_ENABLE_MODE=cli 是操作员的明确选择：不可寻址就报告，不偷偷改文件。"""
    home = tmp_path / "home"
    proc = _run(home, "--skip-patch", env_extra={"PLUGIN_ENABLE_MODE": "cli"})
    assert proc.returncode == 0, proc.stderr
    assert "PLUGIN_ENABLE_MODE=cli" in proc.stdout
    assert not (home / "config.yaml").exists()


def test_skip_plugins_does_not_enable(tmp_path):
    home = tmp_path / "home"
    proc = _run(home, "--skip-patch", "--skip-plugins")
    assert proc.returncode == 0, proc.stderr
    assert "⑧ 跳过插件" in proc.stdout
    # 不落插件副本，也不创建/改动配置。
    assert not (home / "plugins").exists()
    assert not (home / "config.yaml").exists()

    home2 = tmp_path / "home2"
    home2.mkdir(parents=True)
    config = home2 / "config.yaml"
    original = "plugins:\n  enabled: []\n  disabled: [longtask-guard]\n"
    config.write_text(original, encoding="utf-8")
    proc2 = _run(home2, "--skip-patch", "--skip-plugins")
    assert proc2.returncode == 0, proc2.stderr
    assert config.read_text(encoding="utf-8") == original


# --------------------------------------------------------------------------- #
# 官方入口优先
# --------------------------------------------------------------------------- #
def test_official_hermes_entry_point_is_used_for_existing_profile(tmp_path):
    """已有身份标记且数据根有安装树 → 走 `hermes --profile <p> plugins enable`。"""
    root = tmp_path / "root"
    profile = root / "profiles" / "demo"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    (root / "hermes-agent").mkdir()

    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"
    stub = bindir / "hermes"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$*" >> "{calls}"\n'
        'name="${!#}"\n'
        'cfg="${HERMES_HOME:-}/profiles/$2/config.yaml"\n'
        '[ -f "$cfg" ] || exit 7\n'
        'grep -q "^plugins:" "$cfg" || printf "plugins:\\n  enabled:\\n" >> "$cfg"\n'
        'printf "  - %s\\n" "$name" >> "$cfg"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)

    proc = _run(profile, "--skip-patch", path_prefix=bindir)
    assert proc.returncode == 0, proc.stderr
    assert "官方入口" in proc.stdout

    log = calls.read_text(encoding="utf-8").splitlines()
    enable_calls = [line for line in log if "plugins enable" in line]
    assert len(enable_calls) == len(ENABLE_NAMES)
    for name in ENABLE_NAMES:
        assert any(line.endswith(f"plugins enable {name}") for line in enable_calls), name
        assert any("--profile demo" in line for line in enable_calls)
    # 官方命令写了每个 profile 的配置；没有回退合并，也没有多出备份。
    text = (profile / "config.yaml").read_text(encoding="utf-8")
    assert _plugin_list(text, "enabled") == list(ENABLE_NAMES)
    assert not list(profile.glob("config.yaml.bak.*"))
