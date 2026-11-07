#!/usr/bin/env bash
# docker/clean_room_check.sh —— 干净室部署自检（在容器内跑，由 docker/verify.sh 调起）
#
# 校验「一个空 HOME + 本仓源码」能不能立起一套可用的智能体本体资产：
#   ① 仓自检          业务无关门禁 + 引擎单测
#   ② 包安装          离开仓目录仍可 import；随包 .so 在包内（打包元数据正确）
#   ③ dry-run         零副作用：一个文件都不许建
#   ④ 真实安装        资产落位；绝不覆盖现场身份；示例技能不得装成生产技能；
#                     没有 Hermes 时须明确选择 --skip-patch，不能伪报完整部署
#   ⑤ 幂等            重跑退出码 0、识别出「已一致」、文件集合不变
#   ⑥ 合成生效身份    骨架 + 业务叠加层；占位符必须被替换干净
#   ⑦ 引擎端到端      写入 → 汇合 → 召回（离线确定性嵌入，不依赖网络/模型）
#
# 明确不做：不启停服务、不装 Hermes CLI、不碰任何凭据、不写仓内文件。
# 退出码：0 = 全部通过；非 0 = 失败项数。

set -u

# 破坏性清理（rm -rf "$HOME/.hermes"）只能在容器里跑。
if [ ! -f /.dockerenv ]; then
  echo "clean_room_check.sh 只能在容器内运行（拒绝在宿主执行）" >&2
  exit 2
fi

REPO="${REPO:-/opt/super-agent}"
PROFILE=dockercheck
P="$HOME/.hermes/profiles/$PROFILE"
LOG=/tmp/install.log
FAILED=0

pass() { printf '  [PASS] %s\n' "$*"; }
bad()  { printf '  [FAIL] %s\n' "$*"; FAILED=$((FAILED + 1)); }
step() { printf '\n== %s\n' "$*"; }
indent() { sed 's/^/     /'; }

# --------------------------------------------------------------------------- #
step "① 仓自检（业务无关门禁 + 引擎单测）"
if out="$(cd "$REPO" && python3 -m pytest tests -q --tb=short 2>&1)"; then
  printf '%s\n' "$out" | tail -2 | indent
  n="$(printf '%s\n' "$out" | grep -oE '[0-9]+ passed' | head -1)"
  if [ -n "$n" ]; then pass "单测通过（$n）"; else bad "没解析到 passed 计数"; fi
else
  printf '%s\n' "$out" | indent
  bad "仓自检失败"
fi

# --------------------------------------------------------------------------- #
step "② 包安装（离开仓目录仍可 import；随包 .so 在包里）"
if out="$(python3 -m pip install --no-build-isolation "$REPO" -q 2>&1)"; then
  pass "pip install 成功（打包元数据可用）"
else
  printf '%s\n' "$out" | tail -12 | indent
  bad "pip install 失败"
fi
if out="$(cd /tmp && python3 -c '
import pathlib, agentcore.memory as m
so = pathlib.Path(m.__file__).parent / "vendor" / "vec0.so"
print("import_from=%s" % m.__file__)
print("vendor_so=%s" % so.exists())
' 2>&1)"; then
  printf '%s\n' "$out" | indent
  case "$out" in *"site-packages"*) pass "从 site-packages 导入（不是仓内源码）" ;;
                    *) bad "仍在从仓内源码导入，安装没生效" ;; esac
  case "$out" in *"vendor_so=True"*) pass "随包 .so 已打进包内" ;;
                 *) bad "随包 .so 缺失（召回会降级到逐条计算）" ;; esac
else
  printf '%s\n' "$out" | indent
  bad "离开仓目录 import 失败"
fi

# --------------------------------------------------------------------------- #
step "③ --dry-run 必须零副作用"
rm -rf "$HOME/.hermes"
if bash "$REPO/install/install.sh" --dry-run --profile "$PROFILE" --skip-patch > "$LOG" 2>&1; then
  pass "--dry-run 退出码 0"
else
  tail -15 "$LOG" | indent
  bad "--dry-run 退出码非 0"
fi
if [ -e "$HOME/.hermes" ]; then
  bad "dry-run 产生了副作用：$HOME/.hermes 被创建"
  find "$HOME/.hermes" | head -5 | indent
else
  pass "dry-run 未创建任何文件（--dry-run 名副其实）"
fi

# --------------------------------------------------------------------------- #
step "④ 真实安装（模拟「没有 Hermes CLI 的新机器」）"
if bash "$REPO/install/install.sh" --profile "$PROFILE" --skip-patch > "$LOG" 2>&1; then
  pass "安装器退出码 0"
else
  tail -20 "$LOG" | indent
  bad "安装器失败"
fi

if [ -f "$P/agentcore/identity/SOUL.generic.md" ] && \
   cmp -s "$REPO/identity/SOUL.generic.md" "$P/agentcore/identity/SOUL.generic.md"; then
  pass "身份骨架字节一致"
else
  bad "身份骨架缺失或与源不一致"
fi

for f in toolsets/pi_executor/__init__.py runtime/hermes-agent.service \
         patches/feishu_card_replies.patch patches/apply_feishu_card_replies.py \
         patches/patch_card_finalize.py tools/arch_doc.py tools/doc_browser.py \
         tools/assets/mermaid.min.js; do
  if [ -f "$P/agentcore/$f" ]; then pass "落位 agentcore/$f"; else bad "缺失 agentcore/$f"; fi
done

for name in capability-alert-outbox colleague-catalog colleague-team durable-dispatch \
            illustrated-delivery lan-cluster local-model-grounding \
            localvec longtask-guard markdown-delivery memory-bridge task-progress \
            task-status write-guard; do
  if [ -d "$P/plugins/$name" ]; then pass "插件目录 $name"; else bad "插件目录缺失 $name"; fi
done
for name in capability-alert-outbox colleague-catalog colleague-team durable-dispatch \
            illustrated-delivery lan-cluster local-model-grounding \
            longtask-guard markdown-delivery memory-bridge task-progress \
            task-status write-guard; do
  if [ -f "$P/plugins/$name/plugin.yaml" ]; then pass "插件清单 $name"; else bad "插件清单缺失 $name"; fi
  if grep -qE "^[[:space:]]*- $name$" "$P/config.yaml" 2>/dev/null; then
    pass "插件已启用 $name"
  else
    bad "插件未写入 plugins.enabled：$name"
  fi
done
if [ -f "$P/plugins/localvec/__init__.py" ]; then pass "localvec provider 落位"; else bad "localvec provider 缺失"; fi

if [ -f "$P/skills/illustrated-doc-rendering/SKILL.md" ]; then
  pass "T4 具名技能 illustrated-doc-rendering 落位"
else
  bad "T4 具名技能缺失"
fi

n_src="$(find "$REPO/identity/agents" -name '*.md' | wc -l)"
n_dst="$(find "$P/agentcore/agents" -name '*.md' 2>/dev/null | wc -l)"
if [ "$n_src" -gt 0 ] && [ "$n_src" = "$n_dst" ]; then
  pass "派工契约 $n_dst/$n_src 全部落位"
else
  bad "派工契约数量不符：目标 $n_dst / 源 $n_src"
fi

if [ -e "$P/SOUL.md" ]; then
  bad "安装器动了现场身份（$P/SOUL.md 不该被创建）"
else
  pass "未动现场身份（默认不写 SOUL.md）"
fi

# 判定要盯的是「examples/ 只当范本」，不是「一个技能都不许装」。
# 本仓已收录真通用技能（skills/<name>/），安装器把它们装进生产是**正确行为**；
# 旧断言写死 0，会把正确行为判成失败（曾让干净室在 HEAD 报红）。
n_ex="$(find "$P/skills" -path '*/examples/*' -name SKILL.md 2>/dev/null | wc -l)"
n_sk="$(find "$P/skills" -name SKILL.md 2>/dev/null | wc -l)"
if [ "$n_ex" = "0" ]; then
  pass "examples/ 只当范本、没被装成生产技能（已装通用技能 $n_sk 个）"
else
  bad "examples/ 被当成生产技能装了 $n_ex 个"
fi

if bash "$REPO/install/install.sh" --profile "$PROFILE" > "$LOG.missing" 2>&1; then
  bad "缺少 Hermes 补丁目标却返回成功"
elif grep -qE 'Hermes 安装树不存在|未找到 hermes CLI' "$LOG.missing"; then
  pass "缺少 Hermes CLI 或补丁目标时明确失败"
else
  tail -20 "$LOG.missing" | indent
  bad "缺少 Hermes 时没有明确的补丁失败"
fi

# ⑩ 段（人工步骤）必须把变量都展开：这里是那个 unbound 缺陷的回归哨兵
if grep -q '\${HERMES_HOME' "$LOG"; then
  bad "⑩ 段变量没展开（仍有字面量 \${VAR}）"
else
  pass "⑩ 段变量已全部展开（unbound 修复有效）"
fi
if grep -q 'channels.env' "$LOG"; then
  pass "⑩ 段给出了凭据文件落位路径"
else
  bad "⑩ 段缺少凭据文件路径"
fi
printf '  --- ⑩ 段节选（本脚本只打印、不执行）\n'
sed -n '/需要人工执行的步骤/,$p' "$LOG" | head -12 | indent

# --------------------------------------------------------------------------- #
step "④b 安装后的私有运行时真实渲染（离线 Chromium）"
if out="$(cd /tmp && "$P/agentcore/.superagent-venv/bin/python" - "$P/agentcore" <<'PY'
import json
from pathlib import Path
import sys
import tempfile

from PIL import Image
import yaml, markdown, pygments, websocket

engine = Path(sys.argv[1])
assert Path(sys.prefix) == engine / '.superagent-venv', 'private interpreter was lost'
sys.path.insert(0, str(engine / 'tools'))
import arch_doc

with tempfile.TemporaryDirectory(prefix='installed-document-') as directory:
    html, png = (Path(directory) / name for name in ('check.html', 'check.png'))
    body, toc = arch_doc.md_to_html_with_toc(
        '# Deployment check\n\n## Pipeline\n\n```mermaid\nflowchart LR\n A[Submit] --> B[Worker]\n```\n')
    arch_doc.render(html, 'Deployment check', body, toc=toc, footer='Offline rendering check')
    manifest = arch_doc.screenshot(html, png, 900, 900)
    assert manifest['pages'] and png.stat().st_size > 5000
    with Image.open(png) as image:
        image.verify()
    print(json.dumps({'private_runtime': True, 'png_bytes': png.stat().st_size,
                      'pages': len(manifest['pages']), 'rendered': True}))
PY
)"; then
  printf '%s\n' "$out" | indent
  pass "私有 Python 保持虚拟环境身份，真实浏览器生成含图 PNG"
else
  printf '%s\n' "$out" | tail -20 | indent
  bad "安装后的文档渲染失败"
fi

# --------------------------------------------------------------------------- #
step "⑤ 幂等：同一目标重跑一遍"
before="$(find "$P" -type f | sort | md5sum | cut -d' ' -f1)"
if bash "$REPO/install/install.sh" --profile "$PROFILE" --skip-patch > "$LOG" 2>&1; then
  pass "重跑退出码 0"
else
  tail -20 "$LOG" | indent
  bad "重跑失败"
fi
k="$(grep -c '已一致' "$LOG" || true)"
if [ "${k:-0}" -gt 0 ]; then
  pass "识别出 $k 处「已一致」（没有重复劳动）"
else
  bad "重跑没识别出任何已一致文件（幂等判定失效）"
fi
after="$(find "$P" -type f | sort | md5sum | cut -d' ' -f1)"
if [ "$before" = "$after" ]; then
  pass "重跑后文件集合未变"
else
  bad "重跑改动了文件集合"
fi

# --------------------------------------------------------------------------- #
step "⑥ 合成生效身份（骨架 + 业务叠加层）"
printf '# 业务叠加层（示例）\n- 业务技能源 / 业务 MCP 工具 / 业务领域词表由业务仓提供。\n' > /tmp/overlay.md
if bash "$REPO/install/install.sh" --profile "$PROFILE" --skip-patch --compose-soul /tmp/overlay.md > "$LOG" 2>&1; then
  pass "合成退出码 0"
else
  tail -20 "$LOG" | indent
  bad "合成失败"
fi
if [ -f "$P/SOUL.md" ]; then
  if grep -q '业务叠加层（示例）' "$P/SOUL.md"; then pass "叠加层内容已写入生效身份"; else bad "叠加层内容没进去"; fi
  if grep -q '{{BUSINESS_OVERLAY}}' "$P/SOUL.md"; then bad "占位符未被替换干净"; else pass "占位符已全部替换"; fi
  s1="$(wc -c < "$REPO/identity/SOUL.generic.md")"
  s2="$(wc -c < "$P/SOUL.md")"
  # 语义断言：注入点必须唯一、骨架必须原样在前、体积必须等于「骨架 + 叠加层 - 标记」
  n_mark="$(grep -c '{{BUSINESS_OVERLAY}}' "$REPO/identity/SOUL.generic.md" || true)"
  if [ "$n_mark" = "1" ]; then
    pass "骨架里注入点恰好 1 处（叠加层不可能被塞进散文说明）"
  else
    bad "骨架注入点出现 $n_mark 处（应恰好 1 处）"
  fi
  if head -c 2000 "$REPO/identity/SOUL.generic.md" > /tmp/skel_head && \
     head -c 2000 "$P/SOUL.md" > /tmp/composed_head && cmp -s /tmp/skel_head /tmp/composed_head; then
    pass "骨架部分原样保留（前 2000 字节一致）"
  else
    bad "骨架开头被改动 —— 叠加层可能注错了位置"
  fi
  ov="$(wc -c < /tmp/overlay.md)"
  mk='{{BUSINESS_OVERLAY}}'
  mlen="${#mk}"
  expect=$((s1 + ov - mlen))
  if [ "$s2" = "$expect" ]; then
    pass "体积符合预期（骨架 $s1 + 叠加层 $ov − 标记 $mlen = $s2）"
  else
    bad "体积异常：实际 $s2，预期 $expect（若差标记长度的整数倍，说明标记被替换了多处）"
  fi
else
  bad "生效身份未生成"
fi

# --------------------------------------------------------------------------- #
step "⑦ 引擎端到端（写入 → 汇合 → 召回，离线嵌入）"
if out="$(cd /tmp && python3 "$REPO/docker/engine_smoke.py" 2>&1)"; then
  printf '%s\n' "$out" | indent
  case "$out" in *SMOKE_OK*) pass "写入后能召回，且后台向量线程已汇合" ;;
                 *) bad "端到端结果不符合预期" ;; esac
else
  printf '%s\n' "$out" | indent
  bad "引擎端到端失败"
fi

# --------------------------------------------------------------------------- #
step "⑧ 高手绑定（caixuf）与同事团队 / 局域网集群 / 受管单根安装端到端实测"
if out="$(cd /tmp && "$P/agentcore/.superagent-venv/bin/python" - "$P" "$REPO" <<'PY'
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import yaml

profile_dir = Path(sys.argv[1])
repo_dir = Path(sys.argv[2])
engine_dir = profile_dir / "agentcore"
sys.path.insert(0, str(engine_dir))

cfg = yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8"))
entries = cfg["plugins"]["entries"]
for req in ("colleague-catalog", "colleague-team", "illustrated-delivery", "lan-cluster",
            "markdown-delivery", "task-progress", "task-status"):
    assert req in entries, f"missing plugin entry {req}"
    assert entries[req]["settings"].get("enabled") is True, f"{req} not enabled"
    if "engine_root" in entries[req]["settings"]:
        assert entries[req]["settings"]["engine_root"] == str(engine_dir), f"{req} engine_root mismatch"

class DummyCtx:
    def __init__(self, settings, profile_name="dockercheck"):
        self.plugin_config = settings
        self.profile_name = profile_name
        self.tools = {}
        self.hooks = {}
        self.prompts = {}
        self.unloads = []
    def get_config(self, key, default=None):
        cur = self.plugin_config
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur
    def register_tool(self, **kw):
        self.tools[kw["name"]] = kw["handler"]
    def register_hook(self, name, fn):
        self.hooks.setdefault(name, []).append(fn)
    def register_system_prompt_section(self, *args, **kw):
        self.prompts[kw.get("id") or (args[0] if args else "default")] = (args, kw)
    def on_unload(self, fn):
        self.unloads.append(fn)

def load_plugin(plugin_folder, mod_name):
    init_py = profile_dir / "plugins" / plugin_folder / "__init__.py"
    spec = importlib.util.spec_from_file_location(mod_name, init_py)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod

# 1. Colleague Catalog
cat_ctx = DummyCtx(entries["colleague-catalog"]["settings"])
load_plugin("colleague-catalog", "_clean_colleague_catalog").register(cat_ctx)
cat_res = json.loads(cat_ctx.tools["colleague_catalog"]({}, session_id="sess-clean-1"))
assert cat_res["ok"] is True and len(cat_res["colleagues"]) >= 1, f"catalog failed: {cat_res}"

# 2. Colleague Team & Expert caixuf binding
team_settings = dict(entries["colleague-team"]["settings"])
team_ctx = DummyCtx(team_settings, profile_name=team_settings["primary_id"])
load_plugin("colleague-team", "_clean_colleague_team").register(team_ctx)

for role_id, label, duty in (
    ("caixuf_expert", "首席架构与全栈主程高手", "负责复杂架构收敛、全栈开发与多同事协同攻坚"),
    ("architect", "架构师", "负责系统架构设计与模块边界审阅"),
    ("backend", "后端主程", "负责核心引擎与插件实现"),
    ("reviewer", "代码审阅", "负责代码质量与门禁把关"),
):
    def_res = json.loads(team_ctx.tools["colleague_define_role"]({
        "role_id": role_id,
        "label": label,
        "duty": duty,
        "boundaries": ["遵守零硬编码中立红线"],
        "request_id": f"req-def-{role_id}",
    }, session_id="sess-clean-1"))
    assert def_res["ok"] is True, f"define_role {role_id} failed: {def_res}"

hire_caixuf = json.loads(team_ctx.tools["colleague_hire"]({
    "member_id": "caixuf",
    "role_id": "caixuf_expert",
    "display_name": "caixuf（首席高手）",
    "request_id": "req-hire-caixuf",
}, session_id="sess-clean-1"))
assert hire_caixuf["ok"] is True and hire_caixuf["member_id"] == "caixuf", f"hire caixuf failed: {hire_caixuf}"

for cid, rid, dname in (
    ("architect", "architect", "架构师"),
    ("backend", "backend", "后端主程"),
    ("reviewer", "reviewer", "代码审阅"),
):
    r = json.loads(team_ctx.tools["colleague_hire"]({
        "member_id": cid,
        "role_id": rid,
        "display_name": dname,
        "request_id": f"req-hire-{cid}",
    }, session_id="sess-clean-1"))
    assert r["ok"] is True, f"hire {cid} failed: {r}"

grant_res = json.loads(team_ctx.tools["colleague_grant"]({
    "member_id": "caixuf",
    "enabled": True,
    "request_id": "req-grant-caixuf",
}, session_id="sess-clean-1"))
assert grant_res["ok"] is True, f"grant failed: {grant_res}"

list_res = json.loads(team_ctx.tools["colleague_list"]({}, session_id="sess-clean-1"))
assert list_res["ok"] is True, f"list failed: {list_res}"
roster_ids = {c["member_id"] for c in list_res["members"]}
assert {"caixuf", "architect", "backend", "reviewer"}.issubset(roster_ids), f"missing roster ids: {roster_ids}"
assert Path(team_settings["registry_path"]).is_file(), "registry.db not created"

# 3. LAN Cluster RPC & Session Affinity with caixuf expert node
lan_settings = dict(entries["lan-cluster"]["settings"])
lan_settings["nodes"] = {
    "caixuf": {
        "display_name": "caixuf-expert-node",
        "role": "expert-dev",
        "description": "Local loopback expert dev node",
        "capabilities": ["coding", "architecture", "review"],
        "agent_rpc_cmd": [
            sys.executable, "-c",
            "import json, sys; req = json.loads(sys.stdin.read()); "
            "print(json.dumps({'ok': True, 'protocol_version': 2, 'node': 'caixuf', "
            "'session_id': req.get('session_id') or 'sess-caixuf-1', "
            "'summary': 'executed:' + req.get('prompt', '')}))"
        ],
    }
}
lan_ctx = DummyCtx(lan_settings)
load_plugin("lan-cluster", "_clean_lan_cluster").register(lan_ctx)

nodes_res = json.loads(lan_ctx.tools["lan_node_list"]({}, session_id="sess-clean-1"))
assert nodes_res["ok"] is True and any(n["node_id"] == "caixuf" for n in nodes_res["nodes"]), f"lan_node_list failed: {nodes_res}"

rpc_res = json.loads(lan_ctx.tools["lan_node_rpc"]({
    "node_id": "caixuf",
    "request_id": "req-rpc-caixuf-1",
    "operation": "submit",
    "prompt": "verify-clean-room-caixuf-binding",
    "session_key": "clean-room-affinity",
}, session_id="sess-clean-1"))
assert rpc_res["ok"] is True and rpc_res["remote"].get("summary") == "executed:verify-clean-room-caixuf-binding", f"lan_node_rpc failed: {rpc_res}"
assert (Path(lan_settings["state_dir"]) / "lan_sessions.json").is_file(), "lan_sessions.json not persisted"

# 4. Single-root superagent.py install + bound colleague profile convergence
with tempfile.TemporaryDirectory(prefix="superagent-single-root-") as tmp:
    tmp_root = Path(tmp)
    managed_root = tmp_root / "super_agent" / "caixuf"
    profiles_root = tmp_root / ".hermes" / "profiles"
    caixuf_home = profiles_root / "caixuf"
    arch_home = profiles_root / "architect"
    hermes_src = tmp_root / "hermes-src"
    for d in (caixuf_home, arch_home / "plugins" / "localvec", hermes_src / "plugins" / "platforms" / "feishu"):
        d.mkdir(parents=True, exist_ok=True)
    (hermes_src / "plugins" / "platforms" / "feishu" / "adapter.py").write_text("# fake adapter\n", encoding="utf-8")
    (caixuf_home / ".env").write_text("TOKEN=local-test-only\n", encoding="utf-8")
    # Minimal hermes tree markers + config with legacy paths and bound colleague
    (caixuf_home / "config.yaml").write_text(yaml.safe_dump({
        "model": {"default": "gpt-5.4"},
        "memory": {"provider": "localvec", "localvec": {
            "engine_root": "/home/caixuf/.hermes/superagent/caixuf/current",
            "combined_entity_db": "/home/caixuf/.hermes/superagent/caixuf/state/entity-rules/combined_rules.db",
        }},
        "plugins": {
            "enabled": ["colleague-team", "durable-dispatch"],
            "disabled": [],
            "entries": {
                "colleague-team": {"settings": {
                    "enabled": True,
                    "engine_root": "/home/caixuf/.hermes/superagent/caixuf/current",
                    "registry_path": "/home/caixuf/.hermes/superagent/caixuf/state/colleague-team/registry.sqlite",
                    "gateway_dispatch": {
                        "enabled": True,
                        "tool_backend": {"members": [{"profile": "architect", "home": str(arch_home)}]},
                    },
                }},
                "durable-dispatch": {"settings": {
                    "enabled": True,
                    "engine_root": "/home/caixuf/.hermes/superagent/caixuf/current",
                    "queue_root": "/home/caixuf/.hermes/superagent/caixuf/state/analysis-queue",
                    "allowed_roots": [str(repo_dir)],
                }},
            },
        },
    }), encoding="utf-8")
    (arch_home / "config.yaml").write_text(yaml.safe_dump({
        "memory": {"provider": "localvec", "localvec": {
            "engine_root": "/home/caixuf/.hermes/superagent/caixuf/current",
        }},
    }), encoding="utf-8")
    proc = subprocess.run([
        sys.executable, str(repo_dir / "tools" / "superagent.py"),
        "--profile", "caixuf",
        "--root", str(tmp_root / "super_agent"),
        "--profile-home", str(caixuf_home),
        "--hermes-root", str(hermes_src),
        "install",
    ], capture_output=True, text=True, check=True)
    install_out = json.loads(proc.stdout)
    assert install_out["status"] == "installed"
    assert (managed_root / "current").is_symlink()
    migrated_main = yaml.safe_load((caixuf_home / "config.yaml").read_text(encoding="utf-8"))
    migrated_arch = yaml.safe_load((arch_home / "config.yaml").read_text(encoding="utf-8"))
    assert migrated_main["memory"]["localvec"]["engine_root"] == str(managed_root / "current")
    assert migrated_main["memory"]["localvec"]["combined_entity_db"] == str(managed_root / "state" / "entity-rules" / "combined_rules.db")
    assert migrated_arch["memory"]["localvec"]["engine_root"] == str(managed_root / "current")
    assert (arch_home / "plugins" / "localvec").resolve() == (managed_root / "current" / "plugins" / "localvec").resolve()

print(json.dumps({
    "caixuf_bound": True,
    "roster_count": len(roster_ids),
    "lan_rpc_ok": True,
    "single_root_converged": True,
}))
PY
)"; then
  printf '%s\n' "$out" | indent
  pass "caixuf 高手绑定、同事团队协同、LAN 集群协议与单根收敛全部验证通过"
else
  printf '%s\n' "$out" | tail -25 | indent
  bad "高手绑定或单根收敛实测失败"
fi

# --------------------------------------------------------------------------- #
step "结果"
if [ "$FAILED" -eq 0 ]; then
  printf '  全部通过 ✅（干净室可部署）\n'
  exit 0
fi
printf '  失败 %d 项 ❌\n' "$FAILED"
exit "$FAILED"
