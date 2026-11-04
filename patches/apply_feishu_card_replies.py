#!/usr/bin/env python3
"""宿主安装树本地补丁：消息平台流式卡片（card_replies）。

为什么需要本脚本
    `plugins/platforms/feishu/adapter.py` 属于宿主 Hermes 安装树，`hermes update` 会覆盖它。
    补丁本身要能**幂等重放**，且要有一个**廉价的在位校验**，否则哪天悄悄丢了没人知道
    （表现为：功能没了、但不报错，只是回复退回纯文本）。

在位校验同时检查内容标记和当前补丁的反向可应用性。
仅有标记不能证明已安装的补丁版本；版本或上下文不匹配时必须拒绝并人工核对。

用法
    python3 patches/apply_feishu_card_replies.py --check     # 在位吗
    python3 patches/apply_feishu_card_replies.py --apply     # 不在位就补上（幂等）
    python3 patches/apply_feishu_card_replies.py --config    # 适配器解析出的生效值

退出码：--check / --config 用 0=在位 / 1=不在位 / 2=**读不到**（解释器缺 PyYAML，或 profile 目录不对）；--apply 用 0=成功（含本来就在位）。

环境变量
    HERMES_AGENT_DIR    宿主安装树，默认 ~/.hermes/hermes-agent
    HERMES_PROFILE_DIR  --config 用的 profile 目录；未设时按 $HERMES_HOME/active_profile
                        回落，仍解析不到就用 $HERMES_HOME 本身
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import tempfile
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PATCH = HERE / "feishu_card_replies.patch"
PREVIOUS_PATCH = HERE / "legacy" / "feishu_card_replies_1704d06.patch.template"
ADAPTER_REL = "plugins/platforms/feishu/adapter.py"
_PREVIOUS_ADAPTER_SHA256 = "8e40fb34bdc5cb7b03ef2bcf4dd1417768099e5bd6f73596484498490e84ad40"

#: 在位探针：标记函数 + 补丁引入的两个开关字段，**全部**出现才算整块在位。
#: 只查其中一个会被「零散痕迹」骗过（例如只改了字段没改渲染路径）。
MARKER = "_build_reply_card"
IN_PLACE_PROBES = (MARKER, "card_replies", "exec_approval_text")

HERMES_TREE = Path(os.environ.get("HERMES_AGENT_DIR")
                   or (Path.home() / ".hermes" / "hermes-agent")).expanduser()


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()


def _profile_dir() -> Path:
    """解析 --config 要读的 profile 目录（不硬编码任何画像名）。"""
    env = os.environ.get("HERMES_PROFILE_DIR")
    if env:
        return Path(env).expanduser()
    home = _hermes_home()
    try:
        name = (home / "active_profile").read_text(encoding="utf-8").strip()
    except OSError:
        name = ""
    if name:
        cand = home / "profiles" / name
        if (cand / "config.yaml").is_file():
            return cand
    return home


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=HERMES_TREE, capture_output=True, text=True)


def _forward_applies() -> bool:
    return _git("apply", "--check", str(PATCH)).returncode == 0


def _reverse_applies() -> bool:
    if _git("apply", "--check", "-R", str(PATCH)).returncode == 0:
        return True
    # The lifecycle overlay changes the same adapter. Verify the exact overlay,
    # remove it only in a temporary copy, then verify the exact underlying patch.
    overlay = HERE / "feishu_card_lifecycle.patch"
    if not overlay.is_file() or _git("apply", "--check", "-R", str(overlay)).returncode != 0:
        return False
    paths = [line[6:] for line in overlay.read_text().splitlines() if line.startswith("--- a/")]
    if not paths or any(Path(path).is_absolute() or ".." in Path(path).parts for path in paths):
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="card-layer-check-") as tmp:
            root = Path(tmp)
            for path in paths:
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(HERMES_TREE / path, target)
            undone = subprocess.run(["git", "apply", "-R", str(overlay)], cwd=root, capture_output=True)
            base = subprocess.run(["git", "apply", "--check", "-R", str(PATCH)], cwd=root, capture_output=True)
            return undone.returncode == 0 and base.returncode == 0
    except OSError:
        return False


def _adapter_text() -> str | None:
    adapter = HERMES_TREE / ADAPTER_REL
    if not adapter.is_file():
        return None
    return adapter.read_text(encoding="utf-8")


def _probes_hit() -> dict[str, bool]:
    text = _adapter_text() or ""
    return {probe: (probe in text) for probe in IN_PLACE_PROBES}


def _in_place() -> bool:
    # Markers can also occur in an older patch. Require the exact released
    # patch to be reversible before claiming that this version is installed.
    return all(_probes_hit().values()) and _reverse_applies()


def check() -> int:
    if _adapter_text() is None:
        print(f"[FAIL] 找不到适配器：{HERMES_TREE / ADAPTER_REL}（HERMES_AGENT_DIR 指对了吗？）")
        return 1
    hits = _probes_hit()
    in_place = all(hits.values()) and _reverse_applies()
    print(f"[{'PASS' if in_place else 'FAIL'}] 消息平台卡片补丁：{'在位' if in_place else '不在位或版本不匹配'}"
          f"（探针 {hits}）")
    print(f"[INFO] 版本核对：git 可反向应用={_reverse_applies()}")
    return 0 if in_place else 1


def _migration_candidate() -> bytes | None:
    """Build a replacement off-host only when the known previous patch matches."""
    adapter = HERMES_TREE / ADAPTER_REL
    if not adapter.is_file() or not PREVIOUS_PATCH.is_file():
        return None
    before = adapter.read_bytes()
    if hashlib.sha256(before).hexdigest() != _PREVIOUS_ADAPTER_SHA256:
        return None
    lines = before.decode("utf-8").splitlines()
    footer = [line for line in lines if line.startswith('        footer_items.append("执行节点: ')]
    subtitle = [line for line in lines if line.startswith('                "subtitle": {"tag": "plain_text", "content": ')]
    if len(footer) != 1 or len(subtitle) != 1:
        return None
    template = PREVIOUS_PATCH.read_text(encoding="utf-8")
    if template.count("__LEGACY_FOOTER_LINE__") != 1 or template.count("__LEGACY_SUBTITLE_LINE__") != 1:
        return None
    rendered = template.replace("__LEGACY_FOOTER_LINE__", footer[0]).replace("__LEGACY_SUBTITLE_LINE__", subtitle[0])
    with tempfile.TemporaryDirectory(prefix="superagent-card-preflight-") as directory:
        root = Path(directory)
        target = root / ADAPTER_REL
        target.parent.mkdir(parents=True)
        shutil.copy2(adapter, target)
        previous_patch = root / "previous.patch"
        previous_patch.write_text(rendered, encoding="utf-8")
        for arguments in (("-R", previous_patch), (None, PATCH)):
            cmd = ["git", "apply"]
            if arguments[0]:
                cmd.append(arguments[0])
            cmd.append(str(arguments[1]))
            if subprocess.run(cmd, cwd=root, capture_output=True).returncode:
                return None
        if subprocess.run(["git", "apply", "--check", "-R", str(PATCH)], cwd=root, capture_output=True).returncode:
            return None
        return target.read_bytes()


def _apply_migration() -> bool:
    adapter = HERMES_TREE / ADAPTER_REL
    before = adapter.read_bytes()
    updated = _migration_candidate()
    if updated is None or adapter.read_bytes() != before:
        return False
    digest = hashlib.sha256(before).hexdigest()[:16]
    backup = adapter.with_name(adapter.name + ".superagent-" + digest + ".bak")
    if backup.exists():
        if not backup.is_file() or backup.read_bytes() != before:
            return False
    else:
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, adapter.stat().st_mode & 0o777)
        with os.fdopen(fd, "wb") as stream:
            stream.write(before)
            stream.flush()
            os.fsync(stream.fileno())
    fd, name = tempfile.mkstemp(prefix=".superagent-card-", dir=adapter.parent)
    try:
        os.fchmod(fd, adapter.stat().st_mode & 0o777)
        with os.fdopen(fd, "wb") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        if adapter.read_bytes() != before:
            return False
        os.replace(name, adapter)
        return True
    finally:
        if os.path.exists(name):
            os.unlink(name)

def apply() -> int:
    if _in_place():
        print("[PASS] 补丁已在位，无需重放")
        return 0
    if not _forward_applies() and _migration_candidate() is not None:
        if _apply_migration() and _in_place():
            print("[PASS] 已从已知旧版本升级，原适配器已备份；需重启网关")
            return 0
        print("[FAIL] 旧版迁移失败或宿主同时发生变化")
        return 1
    if not _forward_applies():
        print("[FAIL] 补丁不能干净应用（上游 adapter.py 可能已变动）。"
              "请人工核对后再重放：\n  cd %s && git apply --3way %s" % (HERMES_TREE, PATCH))
        return 1
    res = _git("apply", str(PATCH))
    if res.returncode != 0:
        print(f"[FAIL] git apply 失败：{res.stderr.strip()}")
        return 1
    if not _in_place():
        print(f"[FAIL] 已应用但探针仍不全：{_probes_hit()} —— 补丁不完整，请人工核对")
        return 1
    print("[PASS] 补丁已重放；**需重启网关**后才生效")
    return 0


def _config_enabled(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def config_check() -> int:
    """用适配器自己的配置链验证 card_replies 的生效值（抓「写了配置但读不到」）。"""
    profile = _profile_dir()
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        yaml = None
    raw = None
    if yaml is not None:
        cfg_path = profile / "config.yaml"
        if cfg_path.is_file():
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if not raw:
        # 读不到**不能**返回 0：按本脚本自己的约定 0 = 在位，于是「解释器缺 PyYAML」
        # 或「HERMES_PROFILE_DIR 指错」会被上层读成「配置生效了」—— 假绿的经典来源（实测踩过）。
        if yaml is None:
            print(f"[FAIL] 当前解释器没有 PyYAML，读不到 {profile / 'config.yaml'}"
                  "（换带 PyYAML 的解释器，或设 HERMES_PROFILE_DIR 指向生效 profile）")
        else:
            print(f"[FAIL] 读不到 {profile / 'config.yaml'}（文件不存在或为空；"
                  "设 HERMES_PROFILE_DIR 指向实际生效的 profile）")
        return 2
    flagged = []
    for key in ("card_replies", "exec_approval_text"):
        value = ((raw.get("platforms") or {}).get("feishu") or {}).get(key)
        print(f"[INFO] profile 配置的平台级 {key} = {value!r}")
        if value is not None and value not in (True, "true", "True", 1, "1"):
            flagged.append(key)
    if _config_enabled(((raw.get("platforms") or {}).get("feishu") or {}).get("card_replies")):
        stream = raw.get("streaming") or {}
        mode = stream.get("mode")
        enabled = (_config_enabled(stream["enabled"]) if "enabled" in stream
                   else mode is not None and mode is not False and str(mode).strip().lower() != "off")
        transport = stream.get("transport")
        if transport is None:
            transport = mode
        display_platforms = (raw.get("display") or {}).get("platforms") or {}
        override = (display_platforms.get("feishu") or {}).get("streaming")
        if (not enabled or transport is False or str(transport).strip().lower() == "off"
                or (override is not None and not _config_enabled(override))):
            print("[FAIL] card_replies 已开启，但 streaming 未生效；检查 streaming.enabled、"
                  "streaming.transport 和 display.platforms.feishu.streaming（卡片依赖流式预览）")
            flagged.append("streaming")
    if not any((((raw.get("platforms") or {}).get("feishu") or {}).get(k)) is not None
               for k in ("card_replies", "exec_approval_text")):
        print("[WARN] 两项均未配置；功能在位但未启用（卡片默认关、审批仍走卡片按钮）")
        return 1
    return 0 if not flagged else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="消息平台流式卡片本地补丁的校验/重放")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="补丁是否在位")
    g.add_argument("--apply", action="store_true", help="幂等重放补丁")
    g.add_argument("--preflight", action="store_true", help="只读检查当前版本是否已安装或可干净应用")
    g.add_argument("--config", action="store_true", help="检查配置生效值")
    args = ap.parse_args()
    if args.check:
        return check()
    if args.apply:
        return apply()
    if args.preflight:
        if _in_place() or _forward_applies() or _migration_candidate() is not None:
            print("[PASS] 卡片补丁版本与宿主兼容")
            return 0
        print("[FAIL] 宿主卡片补丁版本不匹配或无法干净应用；未修改宿主")
        return 1
    return config_check()


if __name__ == "__main__":
    sys.exit(main())
