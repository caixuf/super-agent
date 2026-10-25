#!/usr/bin/env bash
# docker/clean_room_check.sh —— 干净室部署自检（在容器内跑，由 docker/verify.sh 调起）
#
# 校验「一个空 HOME + 本仓源码」能不能立起一套可用的智能体本体资产：
#   ① 仓自检          业务无关门禁 + 引擎单测
#   ② 包安装          离开仓目录仍可 import；随包 .so 在包内（打包元数据正确）
#   ③ dry-run         零副作用：一个文件都不许建
#   ④ 真实安装        资产落位；绝不覆盖现场身份；示例技能不得装成生产技能；
#                     没有 Hermes 时补丁步骤必须优雅跳过而不是报错
#   ⑤ 幂等            重跑退出码 0、识别出「已一致」、文件集合不变
#   ⑥ 合成生效身份    骨架 + 业务叠加层；占位符必须被替换干净
#   ⑦ 引擎端到端      写入 → 汇合 → 召回（离线确定性嵌入，不依赖网络/模型）
#
# 明确不做：不启停服务、不装 Hermes CLI、不碰任何凭据、不写仓内文件。
# 退出码：0 = 全部通过；非 0 = 失败项数。

set -u
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
if out="$(cd "$REPO" && python3 -m pytest tests -q 2>&1)"; then
  printf '%s\n' "$out" | tail -2 | indent
  n="$(printf '%s\n' "$out" | grep -oE '[0-9]+ passed' | head -1)"
  if [ -n "$n" ]; then pass "单测通过（$n）"; else bad "没解析到 passed 计数"; fi
else
  printf '%s\n' "$out" | tail -20 | indent
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
if bash "$REPO/install/install.sh" --dry-run --profile "$PROFILE" > "$LOG" 2>&1; then
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
if bash "$REPO/install/install.sh" --profile "$PROFILE" > "$LOG" 2>&1; then
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
         patches/feishu_card_replies.patch patches/apply_feishu_card_replies.py; do
  if [ -f "$P/agentcore/$f" ]; then pass "落位 agentcore/$f"; else bad "缺失 agentcore/$f"; fi
done

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

n_sk="$(find "$P/skills" -name SKILL.md 2>/dev/null | wc -l)"
if [ "$n_sk" = "0" ]; then
  pass "示例技能没有被装成生产技能（0 个）"
else
  bad "意外装成生产技能 $n_sk 个（examples/ 应该只当范本）"
fi

if grep -qE 'Hermes 安装树不存在|跳过补丁' "$LOG"; then
  pass "没有 Hermes 时补丁步骤优雅跳过（不是失败）"
else
  bad "补丁步骤没有优雅跳过"
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
step "⑤ 幂等：同一目标重跑一遍"
before="$(find "$P" -type f | sort | md5sum | cut -d' ' -f1)"
if bash "$REPO/install/install.sh" --profile "$PROFILE" > "$LOG" 2>&1; then
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
if bash "$REPO/install/install.sh" --profile "$PROFILE" --compose-soul /tmp/overlay.md > "$LOG" 2>&1; then
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
  if [ "$s2" -ge "$s1" ]; then
    pass "合成文件不小于骨架（$s1 -> $s2 字节）"
  else
    bad "合成文件比骨架还小（$s1 -> $s2），疑似截断"
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
step "结果"
if [ "$FAILED" -eq 0 ]; then
  printf '  全部通过 ✅（干净室可部署）\n'
  exit 0
fi
printf '  失败 %d 项 ❌\n' "$FAILED"
exit "$FAILED"