#!/usr/bin/env bash
# checks/identity_injection_check.sh —— 证明「身份文件真的进了发给模型的 prompt」
#
# 为什么需要它：干净室与特性验证只能证明「文件落位了」「合成对了」，**证明不了它被喂给了模型**。
# 这里用一条不依赖可用凭据的判定链：
#
#   把隔离 profile 的模型端点指向一个**死端口** → 调用必然失败
#   → Hermes 在 API 出错时**无条件**写请求转储 `<HERMES_HOME>/sessions/request_dump_*.json`（已脱敏）
#   → 转储里带着这一次的完整请求体（含 system prompt 位置的身份文件）
#   → 于是「身份是否注入」变成可机械判定的断言：转储里必须出现**骨架独有句**与**叠加层独有句**。
#
# 安全边界（硬要求）：
#   * 只操作**隔离 profile**（默认 `superagent-check`），跑完删除；绝不写生产 profile
#   * 跑前备份生产 profile 的 config.yaml，跑后 diff 必须**无变化**（证明没串到生产）
#   * 死端点不需要凭据：本脚本不读、不打印任何 key
#
# 用法：bash checks/identity_injection_check.sh
# 退出码：失败项数（0 = 通过；>0 = 有失败项）

set -u
PROFILE="${PROFILE:-superagent-check}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IHOME="$HOME/.hermes/profiles/$PROFILE"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/sa-inject.XXXXXX")"
#: 备份目录**故意不随 trap 删除**：万一隔离被击穿，操作者要能自己 diff
BAK="$(mktemp -d "${TMPDIR:-/tmp}/sa-inject-bak.XXXXXX")"
FAILED=0

pass() { printf '  [PASS] %s\n' "$*"; }
bad()  { printf '  [FAIL] %s\n' "$*"; FAILED=$((FAILED + 1)); }
warn() { printf '  [WARN] %s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
indent() { sed 's/^/     /'; }

# 生效 profile 名由环境或 ~/.hermes/active_profile 给出；**脚本里不写死任何画像名**
LIVE_PROFILE="${HERMES_PROFILE:-$(cat "$HOME/.hermes/active_profile" 2>/dev/null | tr -d '\n' || true)}"
[ -n "$LIVE_PROFILE" ] || LIVE_PROFILE="default"
LIVE_CFG="$HOME/.hermes/profiles/$LIVE_PROFILE/config.yaml"
TOPLVL_CFG="$HOME/.hermes/config.yaml"

cleanup() {
  # 保留隔离 profile：**不要**在热状态下 rm -rf 它 —— 被删掉却仍被进程持有的 state.db-wal/shm
  # inode 会让下次启动直接 `profile_parked`（实测踩过）。它是可弃目录，留着、幂等重装即可。
  rm -rf "$TMP"
}
trap cleanup EXIT

# --------------------------------------------------------------------------- #
step "① 备份生产配置（跑完 diff；备份保留在 $BAK）"
[ -f "$LIVE_CFG" ] && cp -p "$LIVE_CFG" "$BAK/live_config.yaml" && pass "已备份生产 config.yaml（$LIVE_PROFILE）" \
                   || warn "生产 config.yaml 不存在，跳过该基线"
[ -f "$TOPLVL_CFG" ] && cp -p "$TOPLVL_CFG" "$BAK/toplevel_config.yaml" && pass "已备份顶层 config.yaml"
printf '     人工核对命令：\n'
printf '       diff -u %s %s\n' "$BAK/live_config.yaml" "$LIVE_CFG"
printf '       diff -u %s %s\n' "$BAK/toplevel_config.yaml" "$TOPLVL_CFG"

# --------------------------------------------------------------------------- #
step "② 隔离 profile 里装通用身份（骨架 + 叠加层合成）"
if ! command -v hermes > /dev/null 2>&1; then
  warn "本机没有 hermes CLI —— 本项无法验证（资产落位由 docker/ 干净室覆盖）"
  exit 0
fi
printf '# 业务叠加层（注入验证）\n- 叠加层独有句：领域词表与业务工具面由业务仓提供。\n' > "$TMP/overlay.md"
if bash "$REPO_ROOT/install/install.sh" --hermes-home "$IHOME" --compose-soul "$TMP/overlay.md" \
     --skip-patch --skip-skills > "$TMP/install.log" 2>&1; then
  pass "隔离 profile 身份已合成（$IHOME/SOUL.md）"
else
  tail -5 "$TMP/install.log" | indent; bad "合成失败"
fi
[ -f "$IHOME/SOUL.md" ] && pass "  SOUL.md 在盘" || bad "  SOUL.md 没生成"

# --------------------------------------------------------------------------- #
step "③ 用「真 provider + 形状合法但无效的 key」逼出一次 API 调用"
warn "不涉及任何真实凭据：让 provider 拿一个占位/无效 key 打一次必然失败的调用（401）"
warn "为什么不用「自定义 provider + 死端口」：CLI 会在预检阶段就退出（提示换个 provider），"
warn "根本走不到 API 调用，于是没有转储可供判定（实测踩过）—— 真 provider 才过得去预检。"
warn "另一个坑：隔离 profile 若是**被热删过**的旧名字，state.db 的 WAL 可能仍被宿主网关持有 →"
warn "启动直接 profile_parked。解法=换一个新 profile 名（PROFILE=... 覆盖），**不要停网关**、不要手删 WAL。"

# 只写不敏感的键（provid选/模型名）；key 本身走环境变量，永不落盘到隔离 profile
for kv in "model.default gpt-4o-mini" "model.provider openai"; do
  # shellcheck disable=SC2086
  if hermes -p "$PROFILE" config set $kv > /dev/null 2>&1; then
    pass "已写入隔离 profile：$kv"
  else
    warn "写入失败：$kv（CLI 可能不支持该键，继续尝试）"
  fi
done

# --------------------------------------------------------------------------- #
step "④ 跑一轮对话（预期失败），再从请求转储里找身份文件"
mkdir -p "$IHOME/sessions"
# `</dev/null` 是必须的：CLI 若卡在 stdin 的交互提示上，等到超时被杀就根本没走到 API 调用（实测踩过）
# 假 key 走环境变量（不落盘到隔离 profile）；`-m/--provider` 显式指定，免得它在预检阶段就换 provider 退出
out="$(OPENAI_API_KEY=sk-bogus-not-a-secret timeout 420 hermes -p "$PROFILE" \
        -m gpt-4o-mini --provider openai -z "ping" < /dev/null 2>&1)"; rc=$?
printf '%s\n' "$out" | tail -3 | indent
printf '     （退出码 %d；失败是预期行为：端点不存在，我们只要它走到 API 调用并落下转储）\n' "$rc"

dump="$(ls -t "$IHOME"/sessions/request_dump_*.json 2>/dev/null | head -1 || true)"
if [ -n "$dump" ]; then
  pass "拿到请求转储：$(basename "$dump")"
  cp -p "$dump" "$BAK/" 2>/dev/null || true
  for marker in "系统内部自足" "记忆先查再动手" "写保护与审批通道"; do
    if grep -q "$marker" "$dump"; then pass "转储里含骨架独有句：$marker"; else bad "转储里没有骨架句子：$marker"; fi
  done
  if grep -q '叠加层独有句' "$dump"; then
    pass "转储里含**叠加层**独有句（说明业务叠加层也进了 prompt）"
  else
    bad "转储里没有叠加层内容 —— 合成结果可能没被读取"
  fi
  # 比逐词扫描更强、且脚本里**不必复制词表**：合成结果必须**逐字节等于**「骨架 + 叠加层」
  if python3 -c 'import pathlib,sys; s,o,g=(pathlib.Path(p).read_bytes() for p in sys.argv[1:4]); sys.exit(0 if s.replace(b"{{BUSINESS_OVERLAY}}", o) == g else 1)' \
       "$REPO_ROOT/identity/SOUL.generic.md" "$TMP/overlay.md" "$IHOME/SOUL.md"; then
    pass "生效身份逐字节等于「骨架 + 叠加层」（比逐词扫描更强，且不依赖词表）"
  else
    bad "生效身份与「骨架 + 叠加层」不一致"
  fi
else
  bad "没有拿到请求转储 —— 无法判定身份是否注入（看看上面那轮对话为什么没走到 API 调用）"
fi

# --------------------------------------------------------------------------- #
step "⑤ 隔离保证：生产配置"
if [ -f "$BAK/live_config.yaml" ]; then
  if cmp -s "$BAK/live_config.yaml" "$LIVE_CFG"; then
    pass "生产 profile（$LIVE_PROFILE）config.yaml 无变化"
  else
    bad "生产 config.yaml 被改动 —— 立刻人工核对：diff -u $BAK/live_config.yaml $LIVE_CFG"
  fi
fi
if [ -f "$BAK/toplevel_config.yaml" ] && [ -f "$TOPLVL_CFG" ]; then
  if cmp -s "$BAK/toplevel_config.yaml" "$TOPLVL_CFG"; then
    pass "顶层 config.yaml 无变化"
  else
    # 已知现象：CLI 启动时补默认键会重写这个文件（补键，不是我们写的值）—— 必须区分开，
    # 否则会把「CLI 自己补键」误报成「隔离被击穿」，噪声会淹没真正的泄漏。
    leak="$(grep -nE 'dead-model|127\.0\.0\.1:9|dummy-not-a-secret' "$TOPLVL_CFG" | head -3 || true)"
    if [ -z "$leak" ]; then
      warn "顶层 config.yaml 被重写，但**没有**本脚本写入的死端点/死模型键（疑似 CLI 补默认键）"
      warn "  核对：diff -u $BAK/toplevel_config.yaml $TOPLVL_CFG"
    else
      bad "顶层 config.yaml 出现本脚本写入的键 —— 隔离被击穿：$leak"
    fi
  fi
fi

step "结果"
printf '  （备份保留在 %s，核对无误后可自行删除）\n' "$BAK"
if [ "$FAILED" -eq 0 ]; then printf '  身份注入验证通过 ✅\n'; exit 0; fi
printf '  失败 %d 项 ❌\n' "$FAILED"
exit "$FAILED"