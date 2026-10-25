#!/usr/bin/env bash
# checks/feature_check.sh —— **特性**验证（宿主侧）
#
# 与 docker/ 的分工：
#   docker/  证明「在一个空机器上装得上、装完能用」（干净室，无 Hermes、无凭据）
#   本脚本   证明「装出来的东西**各项特性真的成立**」——身份注入 / 派工契约 / 示例技能 /
#            业务叠加钩子 / 记忆闭环 / 适配补丁 / 外部执行器 / 运行时骨架
#
# 安全边界：
#   * 一律用 `--hermes-home <临时目录>`，**绝不碰生产 profile**
#   * 不改任何全局配置；执行器只在自己给的隔离工作目录里读写
#   * 打印的回执里不含 prompt、工具参数与凭据
#
# 用法：bash checks/feature_check.sh
# 退出码：失败项数（0 = 全绿）

set -u
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/sa-feature.XXXXXX")"
HOME_DIR="$TMP/home"
FAILED=0

pass() { printf '  [PASS] %s\n' "$*"; }
bad()  { printf '  [FAIL] %s\n' "$*"; FAILED=$((FAILED + 1)); }
step() { printf '\n== %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*"; }
indent() { sed 's/^/     /'; }
trap 'rm -rf "$TMP"' EXIT

SOUL="$REPO_ROOT/identity/SOUL.generic.md"

# --------------------------------------------------------------------------- #
step "F1 身份骨架：纪律项齐备（八节）"
missing=""
for s in 工具分流分层 系统内部自足 结论先行 长任务后台化 记忆先查再动手 \
         交付与验收纪律 写保护与审批通道 业务叠加层的注入契约; do
  grep -q "$s" "$SOUL" || missing="$missing $s"
done
if [ -z "$missing" ]; then pass "八节纪律项全部在位"; else bad "缺纪律项：$missing"; fi

# --------------------------------------------------------------------------- #
step "F2 身份合成：注入点唯一 + 只替换那一处"
n_mark="$(grep -c '{{BUSINESS_OVERLAY}}' "$SOUL" || true)"
if [ "$n_mark" = "1" ]; then
  pass "骨架里注入点恰好 1 处（叠加层不可能被塞进散文说明）"
else
  bad "注入点出现 $n_mark 处（应恰好 1 处）—— 合成会把叠加层散落到正文里"
fi

printf '# 业务叠加层（特性验证）\n- 领域词表 / 业务工具面 / 知识库位置由业务仓提供。\n' > "$TMP/overlay.md"
if bash "$REPO_ROOT/install/install.sh" --hermes-home "$HOME_DIR" --compose-soul "$TMP/overlay.md" \
     > "$TMP/compose.log" 2>&1; then
  pass "合成退出码 0"
  grep '合成完成' "$TMP/compose.log" | tail -1 | indent
else
  tail -5 "$TMP/compose.log" | indent; bad "合成失败"
fi
COMPOSED="$HOME_DIR/SOUL.md"
if [ -f "$COMPOSED" ]; then
  if head -c 2000 "$SOUL" > "$TMP/a" && head -c 2000 "$COMPOSED" > "$TMP/b" && cmp -s "$TMP/a" "$TMP/b"; then
    pass "骨架部分原样保留（前 2000 字节一致）"
  else
    bad "骨架开头被改动 —— 叠加层注错了位置"
  fi
  if grep -q '领域词表 / 业务工具面' "$COMPOSED"; then pass "叠加层内容确实进来了"; else bad "叠加层内容没进去"; fi
  if grep -q '{{BUSINESS_OVERLAY}}' "$COMPOSED"; then bad "标记残留"; else pass "标记无残留"; fi
  s1="$(wc -c < "$SOUL")"; ov="$(wc -c < "$TMP/overlay.md")"; s2="$(wc -c < "$COMPOSED")"
  _marker='{{BUSINESS_OVERLAY}}'; mlen="${#_marker}"
  if [ "$s2" = "$((s1 + ov - mlen))" ]; then
    pass "体积等式成立（$s1 + $ov − $mlen = $s2）"
  else
    bad "体积等式不成立：实际 $s2，预期 $((s1 + ov - mlen))"
  fi
else
  bad "生效身份未生成"
fi

# --------------------------------------------------------------------------- #
step "F3 派工契约：角色文件齐备且字段完整"
n_role=0
for f in "$REPO_ROOT"/identity/agents/*.md; do
  base="$(basename "$f")"
  [ "$base" = "README.md" ] && continue
  n_role=$((n_role + 1))
  grep -q '回执字段' "$f" || bad "$base 缺「回执字段」节"
  grep -q '派工卡' "$f" || bad "$base 缺「派工卡」块"
done
if [ "$n_role" -ge 6 ]; then pass "角色契约 $n_role 个，均含「回执字段」与「派工卡」"; else bad "角色契约只有 $n_role 个"; fi

# --------------------------------------------------------------------------- #
step "F4 示例技能：规范可作模板"
n_sk=0
for sk in "$REPO_ROOT"/skills/examples/*/SKILL.md; do
  [ -f "$sk" ] || continue
  n_sk=$((n_sk + 1))
  dir="$(basename "$(dirname "$sk")")"
  if grep -q "^name: $dir\$" "$sk"; then pass "$dir：name 与目录名一致"; else bad "$dir：name 与目录名不一致"; fi
  if grep -qE '^description: (Use when|.{6,})' "$sk"; then pass "$dir：description 可被触发词匹配"; else bad "$dir：description 缺失或过短"; fi
done
[ "$n_sk" -ge 2 ] || bad "示例技能只有 $n_sk 个"

# --------------------------------------------------------------------------- #
step "F5 业务叠加钩子：默认关 / 显式开"
FAKE="$TMP/business-skills/demo-business-skill"
mkdir -p "$FAKE"
printf -- '---\nname: demo-business-skill\ndescription: Use when 演示业务叠加层能被装进技能目录。\nversion: 0.1.0\n---\n\n# demo business skill\n' > "$FAKE/SKILL.md"
bash "$REPO_ROOT/install/install.sh" --hermes-home "$HOME_DIR" --skip-patch > /dev/null 2>&1
if [ -e "$HOME_DIR/skills/demo-business-skill" ]; then
  bad "钩子关着也装了业务技能（默认应该不装）"
else
  pass "钩子关着：业务技能未装（默认全关）"
fi
INSTALL_BUSINESS_SKILLS=1 BUSINESS_SKILLS_SRC="$TMP/business-skills" \
  bash "$REPO_ROOT/install/install.sh" --hermes-home "$HOME_DIR" --skip-patch > /dev/null 2>&1
if [ -f "$HOME_DIR/skills/demo-business-skill/SKILL.md" ]; then
  pass "钩子打开：外部业务技能已装入技能目录"
else
  bad "钩子打开却没装进来 —— 迁移时业务技能会静默丢失"
fi

# --------------------------------------------------------------------------- #
step "F6 记忆引擎闭环（写入 → 汇合 → 召回 → 代际隔离 → 空结果语义）"
if out="$(cd "$TMP" && PYTHONPATH="$REPO_ROOT" python3 "$REPO_ROOT/docker/memory_features.py" 2>&1)"; then
  printf '%s\n' "$out" | grep -E '\[PASS\]|\[FAIL\]|参考' | indent
  pass "记忆特性：$(printf '%s\n' "$out" | grep -c '\[PASS\]') 项通过"
else
  printf '%s\n' "$out" | tail -20 | indent
  bad "记忆特性验证有失败项"
fi

# --------------------------------------------------------------------------- #
step "F7 平台适配补丁（消息平台：流式卡片 + 纯文字审批）"
APPLY="$REPO_ROOT/patches/apply_feishu_card_replies.py"
AGENT_DIR="${HERMES_AGENT_DIR:-$HOME/.hermes/hermes-agent}"
# 生效 profile 目录：显式变量 → $HERMES_HOME → ~/.hermes/active_profile（不写死任何画像名）
PROFILE_DIR="${HERMES_PROFILE_DIR:-}"
if [ -z "$PROFILE_DIR" ] && [ -n "${HERMES_HOME:-}" ]; then PROFILE_DIR="$HERMES_HOME"; fi
if [ -z "$PROFILE_DIR" ] && [ -f "$HOME/.hermes/active_profile" ]; then
  PROFILE_DIR="$HOME/.hermes/profiles/$(tr -d '\n' < "$HOME/.hermes/active_profile")"
fi
# --config 是**读**（检查生效值），不是写；它要 import 适配器链并解析 YAML，缺依赖的解释器会失败
# → 逐个试，**必须同时满足「退出码 0」且「输出里真的报出两个键」**才算过（只看退出码会吃假绿）
PYS="python3 /usr/bin/python3"
[ -x "$AGENT_DIR/.venv/bin/python" ] && PYS="$PYS $AGENT_DIR/.venv/bin/python"
okpy=""; last=""
if [ -d "$AGENT_DIR" ]; then
  for py in $PYS; do
    if out="$(HERMES_AGENT_DIR="$AGENT_DIR" "$py" "$APPLY" --check 2>&1)"; then okpy="$py"; break; else last="$out"; fi
  done
  if [ -n "$okpy" ]; then
    pass "--check 用 $okpy 判定补丁整块在位（幂等可重放）"
  else
    printf '%s\n' "$last" | tail -3 | indent
    bad "--check 报补丁未在位（试过解释器：$PYS）"
  fi
  cfg_ok=""; cfg_out=""
  if [ -n "$PROFILE_DIR" ] && [ -f "$PROFILE_DIR/config.yaml" ]; then
    for py in $PYS; do
      if cfg_out="$(HERMES_PROFILE_DIR="$PROFILE_DIR" "$py" "$APPLY" --config 2>&1)" \
         && printf '%s' "$cfg_out" | grep -q 'card_replies' \
         && printf '%s' "$cfg_out" | grep -q 'exec_approval_text'; then
        cfg_ok="$py"; break
      fi
    done
    if [ -n "$cfg_ok" ]; then
      pass "--config（只读检查）用 $cfg_ok 读到生效值"
      printf '%s\n' "$cfg_out" | grep -E 'card_replies|exec_approval_text' | head -3 | indent
      for k in card_replies exec_approval_text; do
        if printf '%s' "$cfg_out" | grep -q "$k"; then pass "生效配置里报出 $k"; else bad "生效配置里没有 $k"; fi
      done
    else
      printf '%s\n' "$cfg_out" | tail -3 | indent
      warn "--config 读不到生效值（试过解释器：$PYS）—— 该项如实记为未验证"
    fi
  else
    warn "定位不到生效 profile 的 config.yaml（设 HERMES_PROFILE_DIR 后重跑）—— 该项如实记为未验证"
  fi
else
  warn "本机没有 Hermes 安装树（$AGENT_DIR），跳过补丁验证"
fi

# --------------------------------------------------------------------------- #
step "F8 外部执行器（长任务派出去跑，回执以事件流为准）"
if ! command -v node > /dev/null 2>&1; then
  warn "本机没有 node —— 执行器特性未验证（资产落位已由干净室覆盖）"
else
  mkdir -p "$TMP/pi/work"
  printf 'hello-from-fixture\n' > "$TMP/pi/work/fixture.txt"
  pi_ok=""; pi_out=""
  for py in $PYS; do
    if pi_out="$(cd "$REPO_ROOT" && HERMES_HOME="${PROFILE_DIR:-}" "$py" -m toolsets.pi_executor \
        --prompt "Read fixture.txt in the current directory, then create out.txt whose content is exactly the single line it contains." \
        --workdir "$TMP/pi/work" --agent-dir "$TMP/pi/home" --timeout 240 2>&1)"; then
      pi_ok="$py"; break
    fi
  done
  if [ -n "$pi_ok" ]; then
    printf '%s\n' "$pi_out" | indent
    pass "执行器（$pi_ok）返回 ok=1（事件流判定为成功）"
    if [ -f "$TMP/pi/work/out.txt" ] && grep -q 'hello-from-fixture' "$TMP/pi/work/out.txt"; then
      pass "产物回读一致：out.txt 内容与 fixture 相同"
    else
      bad "回执说成功，但产物不存在或内容不符（自报成功不算证据）"
    fi
  else
    printf '%s\n' "$pi_out" | tail -4 | indent
    warn "执行器未跑通 —— 前置条件：Node 侧 CLI + 可解析的模型凭据（如实记为未验证）"
  fi
fi

# --------------------------------------------------------------------------- #
step "F9 运行时骨架：不写死部署身份"
UNIT="$REPO_ROOT/runtime/hermes-agent.service"
if [ -f "$UNIT" ]; then
  if grep -qE 'profiles/[A-Za-z0-9_.-]+' "$UNIT"; then
    bad "服务单元里写死了具体 profile 目录（应占位符化）"
  else
    pass "服务单元不含写死的 profile 目录（占位符化）"
  fi
  grep -qE 'EnvironmentFile|ExecStart' "$UNIT" && pass "服务单元结构完整（EnvironmentFile / ExecStart 在位）" || bad "服务单元结构不完整"
else
  bad "缺 runtime/hermes-agent.service"
fi

# --------------------------------------------------------------------------- #
step "F10 明确**未**覆盖的东西（别把绿当全能）"
cat <<'EOF'
     * 网关能否启动、消息能否收发 —— 需要真凭据与平台回调，属部署后验收
     * 模型路由是否可用 —— 本脚本只在本机已有路由上跑执行器一步，不验证多路由
     * 业务叠加层的**内容**（业务工具面 / 知识库 / 业务身份段）—— 本仓不含业务内容
     * 跨机迁移后的账号级配置（systemd linger、profile 凭据）—— 见 docs/MIGRATION.md
EOF

step "结果"
if [ "$FAILED" -eq 0 ]; then
  printf '  特性验证全部通过 ✅\n'
  exit 0
fi
printf '  失败 %d 项 ❌\n' "$FAILED"
exit "$FAILED"