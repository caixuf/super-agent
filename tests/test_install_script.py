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
        ["bash", str(INSTALL), "--hermes-home", str(home), "--skip-deps", *extra],
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
    # 补丁脚本都必须出现在计划里，且都是先 --check。
    assert "apply_feishu_card_replies.py --check" in out
    assert "patch_card_finalize.py --check" in out
    assert "patch_card_finalize.py --apply" in out
    assert "patch_card_markdown_native.py --check" in out
    assert "patch_card_markdown_native.py --apply" in out
    assert "patch_feishu_delivery_uuid.py --hermes-root" in out

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
    assert "engine_root:" in text
    assert "team_id:" in text
    assert (home / "agentcore-state" / "colleague-team").stat().st_mode & 0o077 == 0
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
    # 保留显式关闭；其余名字按原顺序追加，已有部署 entry 不覆盖。
    assert _plugin_list(text, "enabled") == [
        "existing-plugin", *(name for name in ENABLE_NAMES if name != "longtask-guard")
    ]
    assert _plugin_list(text, "disabled") == ["longtask-guard", "some-other-plugin"]
    assert "engine_root: /opt/canonical-engine" in text
    # 写入前留了可回滚的备份。
    backups = list(home.glob("config.yaml.bak.*"))
    assert len(backups) == 2
    assert original in [backup.read_text(encoding="utf-8") for backup in backups]


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


def test_explicit_entry_opt_out_is_preserved(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.yaml"
    original = """plugins:
  enabled: []
  disabled: [longtask-guard]
  entries:
    task-status:
      enabled: false
      settings:
        enabled: false
        api_key: keep-this
"""
    config.write_text(original, encoding="utf-8")

    proc = _run(home, "--skip-patch")
    assert proc.returncode == 0, proc.stderr
    text = config.read_text(encoding="utf-8")
    assert "  - longtask-guard" not in text
    assert "enabled: false" in text
    assert "api_key: keep-this" in text
    assert "task-status:" in text
    import yaml
    entries = yaml.safe_load(text)["plugins"]["entries"]
    assert "longtask-guard" not in entries
    assert entries["task-status"]["settings"] == {"enabled": False, "api_key": "keep-this"}


def test_require_ready_fails_closed_without_runtime_evidence(tmp_path):
    home = tmp_path / "home"
    proc = _run(home, "--skip-patch", "--require-ready")
    assert proc.returncode == 1
    assert "runtime_tool_registration_unverified" in proc.stdout
    assert "就绪证据不完整" in proc.stderr


def test_cli_mode_never_falls_back_to_text_merge(tmp_path):
    """PLUGIN_ENABLE_MODE=cli 是操作员的明确选择：不可寻址就报告，不偷偷改文件。"""
    home = tmp_path / "home"
    proc = _run(home, "--skip-patch", env_extra={"PLUGIN_ENABLE_MODE": "cli"})
    assert proc.returncode == 1, proc.stderr
    assert "PLUGIN_ENABLE_MODE=cli" in proc.stderr
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
    # 官方命令写启用列表；安全默认 entry 仍由本地最小写入补齐。
    text = (profile / "config.yaml").read_text(encoding="utf-8")
    assert _plugin_list(text, "enabled") == list(ENABLE_NAMES)
    assert "team_id:" in text
    assert list(profile.glob("config.yaml.bak.*"))


def test_missing_hermes_tree_fails_unless_patches_explicitly_skipped(tmp_path):
    missing = tmp_path / "missing-hermes"
    proc = _run(tmp_path / "profile", env_extra={"HERMES_AGENT_DIR": str(missing)})
    assert proc.returncode != 0
    assert "Hermes 安装树不存在" in proc.stderr
    assert _run(tmp_path / "profile2", "--skip-patch",
                env_extra={"HERMES_AGENT_DIR": str(missing)}).returncode == 0


def test_patch_failure_is_not_reported_as_success(tmp_path):
    tree = tmp_path / "incompatible-hermes"
    tree.mkdir()
    proc = _run(tmp_path / "profile", env_extra={"HERMES_AGENT_DIR": str(tree)})
    assert proc.returncode != 0
    assert "[FAIL]" in proc.stderr


def test_default_capabilities_install_engine_and_preserve_existing_settings(tmp_path):
    import yaml
    home = tmp_path / "profile"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text('''# retain comment
plugins:
  entries:
    task-progress:
      settings:
        enabled: false
        poll_seconds: 25
other: value
''')
    proc = _run(home, "--skip-patch")
    assert proc.returncode == 0, proc.stderr
    text = config.read_text()
    data = yaml.safe_load(text)
    assert "# retain comment" in text and data["other"] == "value"
    entries = data["plugins"]["entries"]
    assert entries["task-progress"]["settings"]["enabled"] is False
    assert entries["task-progress"]["settings"]["poll_seconds"] == 25
    assert entries["task-status"]["settings"]["enabled"] is True
    assert entries["illustrated-delivery"]["settings"]["enabled"] is True
    deploy = home / "agentcore"
    assert (deploy / "agentcore" / "liveness.py").is_file()
    assert (deploy / "skills" / "illustrated-doc-rendering" / "SKILL.md").is_file()
    assert (deploy / "tools" / "arch_doc.py").is_file()
    assert entries["task-status"]["settings"]["engine_root"] == str(deploy)
    assert entries["longtask-guard"]["settings"]["liveness_db"] == entries["task-status"]["settings"]["liveness_db"]
    assert _run(home, "--skip-patch").returncode == 0
    assert config.read_text() == text


def test_defaults_refuse_unsupported_yaml_without_changing_it(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("plugins: {entries: {}}\n")
    engine = tmp_path / "engine" / "agentcore"
    engine.mkdir(parents=True)
    (engine / "liveness.py").touch()
    before = config.read_bytes()
    result = subprocess.run(
        ["python3", str(REPO_ROOT / "install/configure_defaults.py"), str(config), str(engine.parent)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert config.read_bytes() == before


def test_saved_platform_toolsets_include_default_task_status(tmp_path):
    import yaml
    home = tmp_path / "profile"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text("platform_toolsets:\n  feishu:\n    - terminal # retained\n  api_server: [file, web]\n")
    result = _run(home, "--skip-patch")
    assert result.returncode == 0, result.stderr
    text = config.read_text()
    values = yaml.safe_load(text)["platform_toolsets"]
    assert values["feishu"] == ["terminal", "task_status", "colleague_catalog", "colleague_team"]
    assert values["api_server"] == ["file", "web", "task_status", "colleague_catalog", "colleague_team"]
    assert "# retained" in text
    assert _run(home, "--skip-patch").returncode == 0
    assert config.read_text() == text


def test_explicit_analysis_root_configures_new_dispatch_without_overwriting_existing(tmp_path):
    import yaml
    home = tmp_path / "profile"
    home.mkdir()
    root = tmp_path / "approved"
    root.mkdir()
    config = home / "config.yaml"
    config.write_text("platform_toolsets:\n  api_server: [file]\n")
    proc = _run(home, "--skip-patch", "--analysis-root", str(root))
    assert proc.returncode == 0, proc.stderr
    data = yaml.safe_load(config.read_text())
    settings = data["plugins"]["entries"]["durable-dispatch"]["settings"]
    assert settings["enabled"] is True
    assert settings["allowed_roots"] == [str(root)]
    assert settings["queue_root"] == str(home / "state" / "analysis-queue")
    assert "durable_dispatch" in data["platform_toolsets"]["api_server"]
    assert "task_status" in data["platform_toolsets"]["api_server"]
    before = config.read_text()
    assert _run(home, "--skip-patch", "--analysis-root", str(root)).returncode == 0
    assert config.read_text() == before


def test_invalid_analysis_root_fails_without_creating_dispatch_settings(tmp_path):
    home = tmp_path / "profile"
    result = _run(home, "--skip-patch", "--analysis-root", str(tmp_path / "missing"))
    assert result.returncode != 0
    assert "--analysis-root" in result.stderr
    assert not home.exists()


def test_explicit_analysis_root_refuses_conflicting_existing_policy(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    root = tmp_path / "approved"
    root.mkdir()
    config = home / "config.yaml"
    config.write_text("plugins:\n  entries:\n    durable-dispatch:\n      settings:\n        enabled: false\n")
    result = _run(home, "--skip-patch", "--analysis-root", str(root))
    assert result.returncode != 0
    assert "explicitly disabled" in result.stderr


def test_full_patch_install_configures_fenced_delivery_from_real_host_copy(tmp_path):
    import shutil
    import pytest
    import yaml
    host = Path.home() / ".hermes/hermes-agent"
    files = ["gateway/stream_consumer.py", "gateway/stream_consumer_transport.py",
             "plugins/platforms/feishu/adapter.py"]
    if not all((host / path).is_file() for path in files):
        pytest.skip("Hermes source tree unavailable")
    scratch = tmp_path / "hermes"
    for rel in files:
        target = scratch / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(host / rel, target)
    source = tmp_path / "source"
    source.mkdir()
    home = tmp_path / "profile"
    result = _run(home, "--analysis-root", str(source), "--skip-worker",
                  env_extra={"HERMES_AGENT_DIR": str(scratch), "PLUGIN_ENABLE_MODE": "merge"})
    assert result.returncode == 0, result.stderr
    settings = yaml.safe_load((home / "config.yaml").read_text())["plugins"]["entries"]["durable-dispatch"]["settings"]
    assert settings["deliver_completions"] is True
    assert settings["direct_delivery_enabled"] is True
    assert isinstance(settings["delivery_not_before"], (int, float))
    assert Path(settings["delivery_transport_file"]).is_file()


def test_skill_upgrade_backs_up_local_edits(tmp_path):
    home = tmp_path / "profile"
    skill = home / "skills/illustrated-doc-rendering"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("local custom content")
    proc = _run(home, "--skip-patch")
    assert proc.returncode == 0, proc.stderr
    backups = list((home / "skill-backups").glob("illustrated-doc-rendering.*"))
    assert len(backups) == 1
    assert (backups[0] / "SKILL.md").read_text() == "local custom content"
    assert (skill / "SKILL.md").read_text() != "local custom content"


def test_enabled_existing_plugins_are_added_to_saved_platform_toolsets(tmp_path):
    import yaml
    home = tmp_path / "profile"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text("platform_toolsets:\n  cli: [file]\nplugins:\n  entries:\n    durable-dispatch:\n      settings:\n        enabled: true\n    markdown-delivery:\n      settings:\n        enabled: true\n    colleague-catalog:\n      settings:\n        enabled: false\n")
    result = _run(home, "--skip-patch")
    assert result.returncode == 0, result.stderr
    selections = yaml.safe_load(config.read_text())["platform_toolsets"]["cli"]
    assert "task_status" in selections
    assert "durable_dispatch" in selections
    assert "markdown_delivery" in selections
    assert "colleague_catalog" not in selections


def test_fresh_profile_is_created_and_gateway_started(tmp_path):
    import shutil
    import pytest
    host = Path.home() / ".hermes/hermes-agent"
    files = ["gateway/stream_consumer.py", "gateway/stream_consumer_transport.py",
             "plugins/platforms/feishu/adapter.py"]
    if not all((host / rel).is_file() for rel in files):
        pytest.skip("Hermes source tree unavailable")
    copy = tmp_path / "hermes-source"
    for rel in files:
        target = copy / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(host / rel, target)
    user_home = tmp_path / "user"
    user_home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "hermes-calls"
    hermes = bindir / "hermes"
    hermes.write_text('''#!/bin/sh
printf "%s\n" "$*" >> "$TEST_HERMES_CALLS"
if [ "$1" = profile ] && [ "$2" = create ]; then
    mkdir -p "$HOME/.hermes/profiles/$3"
fi
''')
    hermes.chmod(0o755)
    env = dict(os.environ, HOME=str(user_home), PATH=str(bindir) + os.pathsep + os.environ["PATH"],
               HERMES_AGENT_DIR=str(copy), PLUGIN_ENABLE_MODE="merge", TEST_HERMES_CALLS=str(calls))
    env.pop("HERMES_HOME", None)
    proc = subprocess.run(["bash", str(INSTALL), "--profile", "fresh", "--skip-deps", "--skip-memory"],
                          env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    recorded = calls.read_text()
    assert "profile create fresh --no-alias --no-skills" in recorded
    assert "-p fresh gateway install --if-missing --start-now --start-on-login" in recorded
    assert "-p fresh gateway ensure --timeout 45" in recorded
    # Reinstalling an existing profile must not restart or start its gateway.
    calls.write_text("")
    again = subprocess.run(["bash", str(INSTALL), "--profile", "fresh", "--skip-deps", "--skip-memory"],
                           env=env, capture_output=True, text=True, timeout=60)
    assert again.returncode == 0, again.stderr
    assert "gateway install" not in calls.read_text()
    assert "gateway ensure" not in calls.read_text()
    pending = user_home / ".hermes/profiles/fresh/.superagent-gateway-pending"
    pending.touch()
    calls.write_text("")
    resumed = subprocess.run(["bash", str(INSTALL), "--profile", "fresh", "--skip-deps", "--skip-memory"],
                             env=env, capture_output=True, text=True, timeout=60)
    assert resumed.returncode == 0, resumed.stderr
    assert "gateway ensure --timeout 45" in calls.read_text()
    assert not pending.exists()


def test_invalid_profile_name_is_rejected_before_writing(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path))
    proc = subprocess.run(["bash", str(INSTALL), "--profile", "../escape", "--skip-deps", "--skip-patch"],
                          env=env, capture_output=True, text=True)
    assert proc.returncode != 0
    assert not (tmp_path / ".hermes").exists()
