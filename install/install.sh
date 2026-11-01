#!/usr/bin/env bash
# install/install.sh — 把「智能体本体」（通用骨架）装到一台机器的 Hermes 上。
#
# 设计原则
#   1. **幂等**：重复跑不报错、不重复劳动（逐文件 cmp 判定，已一致就跳过）。
#   2. **--dry-run 只打印计划**：不改系统、不建目录、不写文件。
#   3. **不启停任何服务**：本脚本只落文件；systemd 单元的安装命令**打印**给人工执行。
#   4. **不碰凭据**：只拷骨架文件；env 键名清单与真实 .env 都由人工自建。
#   5. **不覆盖现场业务身份**：本仓只有通用骨架，生效身份 = 骨架 + 业务叠加层。
#      默认**不动** $HERMES_HOME/SOUL.md（那上面有业务叠加层），只在显式开关下合成。
#   6. **不动任何源**：只从本仓读、往部署位写；业务仓只读引用。
#   7. **启用插件只写启用记录**：优先官方 `hermes plugins enable`；目标还不能被
#      官方命令寻址时（全新 profile）改用 install/enable_plugins.py 的最小文本合并。
#      回退合并只增删 `plugins.enabled` / `plugins.disabled` 两个列表的行，不整文件重写
#      config.yaml（注释/顺序/密钥/entries 逐字节保留）。
#      `plugins.entries.*.settings`（engine_root / queue_root / allowed_roots）仍属部署配置，
#      本脚本不猜值。官方命令可能请求正在运行的网关重载插件（不重启进程）；要严格只落文件
#      设 PLUGIN_ENABLE_MODE=merge。
#
# 用法
#   bash install/install.sh --help
#   bash install/install.sh --dry-run                        # 看计划
#   bash install/install.sh --profile <profile>              # 真装（通用 + 示例）
#   bash install/install.sh --profile <profile> --skip-plugins
#   bash install/install.sh --profile <profile> --compose-soul /path/to/overlay.md
#   INSTALL_BUSINESS_SKILLS=1 BUSINESS_SKILLS_SRC=<dir> bash install/install.sh
#   PLUGIN_ENABLE_MODE=merge bash install/install.sh --profile <profile>   # 只改文件
#
# 退出码：0 = 成功（含全部已就位）；1 = 前置缺失或某步失败。

set -e -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---------------------------------------------------------------------------- #
# 默认值与开关
# ---------------------------------------------------------------------------- #
DRY_RUN=0
PROFILE=""
HERMES_HOME_ARG=""
DEPLOY_DIR_ARG=""
SOUL_COMPOSE_ARG=""
DO_SOUL_LIVE=0
DO_PLUGINS=1
DO_SKILLS=1
DO_PATCH=1
DO_BASELINE=0
BUSINESS_SKILLS_SRC="${BUSINESS_SKILLS_SRC:-}"
INSTALL_BUSINESS_SKILLS="${INSTALL_BUSINESS_SKILLS:-0}"
# auto = 官方入口可用就用，否则最小合并；cli = 只用官方；merge = 只改文件（不碰运行中的网关）
PLUGIN_ENABLE_MODE="${PLUGIN_ENABLE_MODE:-auto}"

log()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
ok()   { printf '  [OK] %s\n' "$*"; }
skip() { printf '  [--] %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*"; }
fail() { printf '  [FAIL] %s\n' "$*" >&2; }

usage() {
  cat <<'EOF'
install.sh — 智能体本体安装器（幂等 / 支持 --dry-run）

用法:
  bash install/install.sh [选项]

开关:
  --dry-run                 只打印将执行的计划，不改系统
  --profile NAME            目标 Hermes profile（默认：由 $HERMES_HOME 推断，否则 default）
  --hermes-home PATH        Hermes home 全路径（覆盖 --profile 推断）
  --deploy-dir PATH         agentcore 托管资产的部署根（默认 <HERMES_HOME>/agentcore）
  --compose-soul PATH       把 identity/SOUL.generic.md 里的 {{BUSINESS_OVERLAY}} 替换为
                            PATH 的内容，合成生效身份文件写到 <HERMES_HOME>/SOUL.md
                            （旧文件先备份）
  --deploy-soul             只装通用骨架到 <HERMES_HOME>/SOUL.md（**会丢掉业务叠加层**，
                            仅用于全新机器；已有现场身份时不要用）
  --business-skills-src D   业务技能源目录；等价于 BUSINESS_SKILLS_SRC=D
  --skip-plugins            不装按 profile 的插件副本，也不写 plugins.enabled / plugins.disabled
  --skip-skills             不装技能
  --skip-patch              不打消息平台适配补丁
  --apply-baseline          写入**通用能力基线**（轮次/派工上限/无人值守档）。默认**不动**配置；
                            只走官方 `hermes config set`，逐条打印改动与复原口径。
                            写保护开关（protected_instruction_files）不在此步 —— 另行人工决定并留痕
  -h, --help                显示本帮助

装什么（全部业务无关）:
  1. identity/SOUL.generic.md -> <DEPLOY_DIR>/identity/SOUL.generic.md
     （生效身份另见 --compose-soul / --deploy-soul）
  2. identity/agents/*.md     -> <DEPLOY_DIR>/agents/            （派工契约参考位）
  3. toolsets/*/              -> <DEPLOY_DIR>/toolsets/
  4. runtime/*                -> <DEPLOY_DIR>/runtime/
  5. patches/*                -> <DEPLOY_DIR>/patches/
  6. plugins/*/               -> <HERMES_HOME>/plugins/<连字符名>/（按 profile 的插件副本；
                                 目录名里的下划线转连字符，跳过 __pycache__ / *.pyc）
     落位后把带 plugin.yaml 的插件名写进该 profile config 的 plugins.enabled，
     并把同名项从 plugins.disabled 移出（幂等；不动 plugins.entries / 其它键）。
     plugins/localvec 没有 manifest，属 memory.provider，不在此列表。
  7. skills/**/SKILL.md       -> <HERMES_HOME>/skills/<name>/    （通用技能；examples/ 是范本，不安装）
  8. 对 Hermes 安装树打补丁（可 --skip-patch）：
       patches/apply_feishu_card_replies.py --apply（卡片流式 + 审批纯文字）
       patches/patch_card_finalize.py --apply（卡片终态 / 整轮同一张卡）

业务叠加层（默认不装）:
  业务技能：INSTALL_BUSINESS_SKILLS=1 + BUSINESS_SKILLS_SRC=<dir>（或 --business-skills-src）
  业务 MCP / 业务 provider / 业务 SOUL 叠加：由业务层按 runtime/README.md 与
  identity/README.md 的占位符契约补齐。**本脚本不生成任何业务内容。**

环境变量:
  PLUGIN_ENABLE_MODE=auto|cli|merge
                            插件启用入口。auto（默认）：目标 profile 已能被官方命令寻址
                            （已有配置/身份标记且数据根有 Hermes 安装树）时用
                            `hermes plugins enable`，否则用最小文本合并。cli = 只用官方；
                            merge = 只改文件（不请求运行中的网关重载插件）。

不做什么:
  * 不启动/停止/重启任何服务（systemd 命令只打印）
  * 不读写任何 .env / 凭据
  * 不修改部署源（只读）
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)             DRY_RUN=1 ;;
    --profile)             PROFILE="${2:?--profile 需要参数}"; shift ;;
    --hermes-home)         HERMES_HOME_ARG="${2:?--hermes-home 需要参数}"; shift ;;
    --deploy-dir)          DEPLOY_DIR_ARG="${2:?--deploy-dir 需要参数}"; shift ;;
    --compose-soul)        SOUL_COMPOSE_ARG="${2:?--compose-soul 需要参数}"; shift ;;
    --deploy-soul)         DO_SOUL_LIVE=1 ;;
    --business-skills-src) BUSINESS_SKILLS_SRC="${2:?--business-skills-src 需要参数}"; shift ;;
    --skip-plugins)        DO_PLUGINS=0 ;;
    --skip-skills)         DO_SKILLS=0 ;;
    --skip-patch)          DO_PATCH=0 ;;
    --apply-baseline)      DO_BASELINE=1 ;;
    -h|--help)             usage; exit 0 ;;
    *) fail "未知参数：$1"; usage; exit 1 ;;
  esac
  shift
done

case "$PLUGIN_ENABLE_MODE" in
  auto|cli|merge) ;;
  *) warn "未知 PLUGIN_ENABLE_MODE=$PLUGIN_ENABLE_MODE（auto|cli|merge），按 auto 处理"
     PLUGIN_ENABLE_MODE=auto ;;
esac

# ---------------------------------------------------------------------------- #
run() {  # dry-run 时只打印
  if [ "$DRY_RUN" = "1" ]; then
    printf '    [plan] %s\n' "$*"
  else
    "$@"
  fi
}

install_file() {  # 幂等拷单文件
  local src="$1" dest="$2"
  if [ -f "$dest" ] && cmp -s "$src" "$dest"; then
    skip "已一致 $dest"
    return 0
  fi
  run mkdir -p "$(dirname "$dest")"
  run cp -p "$src" "$dest"
  ok "部署 $dest"
}

install_tree() {  # 幂等拷目录（排除 __pycache__ / *.pyc）
  local src="${1%/}" dest="$2" f rel
  [ -d "$src" ] || { fail "源目录不存在：$src"; return 1; }
  run mkdir -p "$dest"
  while IFS= read -r -d '' f; do
    rel="${f#"$src"/}"
    case "$rel" in *__pycache__*|*.pyc) continue ;; esac
    install_file "$f" "$dest/$rel"
  done < <(find "$src" -type f -print0 | sort -z)
}

install_plugin_tree() {  # 幂等装各插件目录：<root>/<snake_case>/ -> <dest>/<kebab-case>/
  local src_root="${1%/}" dest_root="$2" d name dest
  [ -d "$src_root" ] || { warn "插件源目录不存在：$src_root"; return 0; }
  for d in "$src_root"/*/; do
    [ -d "$d" ] || continue
    name="$(basename "$d")"
    name="${name//_/-}"
    dest="$dest_root/$name"
    # 现场有把插件目录做成指向开发仓的符号链接。直接 cp 会**写穿到链接目标**，
    # 等于安装器改了别的仓；这里先摘掉链接，再落实体副本。
    if [ -L "$dest" ]; then
      warn "目标原是符号链接（$dest -> $(readlink "$dest")），改为实体副本"
      run rm -f "$dest"
    fi
    install_tree "$d" "$dest"
  done
}

install_skill_tree() {  # 幂等装一棵技能树（递归找 SKILL.md，按目录名安装）
  local src="$1" label="$2" sk name dest n=0 skip_examples="${3:-0}"
  [ -d "$src" ] || { warn "$label 源目录不存在：$src"; return 0; }
  while IFS= read -r -d '' sk; do
    name="$(basename "$(dirname "$sk")")"
    if [ "$skip_examples" = "1" ]; then
      case "$sk" in */examples/*) skip "$label 跳过范本技能 $name"; continue ;; esac
    fi
    dest="$SKILLS_DEST/$name"
    if [ -d "$dest" ] && diff -rq "$(dirname "$sk")" "$dest" >/dev/null 2>&1; then
      skip "$label 技能已一致 $name"; n=$((n + 1)); continue
    fi
    run mkdir -p "$SKILLS_DEST"
    run rm -rf "$dest"
    run cp -r "$(dirname "$sk")" "$dest"
    ok "$label 安装技能 $name"; n=$((n + 1))
  done < <(find "$src" -name SKILL.md -print0 | sort -z)
  log "  $label 处理 $n 个技能"
}

# ---------------------------------------------------------------------------- #
# 插件启用：优先官方入口，否则最小幂等合并（只动 plugins.enabled / disabled）
# ---------------------------------------------------------------------------- #
ENABLER="$REPO_ROOT/install/enable_plugins.py"
PLUGIN_ENABLE_NAMES=()
PLUGIN_ENABLE_ROOT=""
PLUGIN_ENABLE_PROFILE=""
PLUGIN_ENABLE_BIN=""

plugin_manifest_name() {  # plugin.yaml 里的 name:（去引号/空白）；空表示没有
  local f="$1" line name=""
  [ -f "$f" ] || return 0
  while IFS= read -r line; do
    line="${line%$'\r'}"
    case "$line" in name:*) name="${line#name:}"; break ;; esac
  done < "$f"
  name="${name#"${name%%[![:space:]]*}"}"
  name="${name%"${name##*[![:space:]]}"}"
  name="${name%\"}"; name="${name#\"}"
  name="${name%\'}"; name="${name#\'}"
  printf '%s' "$name"
}

plugin_enable_names() {  # 带 manifest 的插件名（localvec 没有 manifest，不在此列）
  local f name
  for f in "$REPO_ROOT"/plugins/*/plugin.yaml; do
    [ -f "$f" ] || continue
    name="$(plugin_manifest_name "$f")"
    [ -n "$name" ] && printf '%s\n' "$name"
  done
}

plugin_settings_gated_names() {  # 以 settings.enabled 为总开关的插件：只在 plugins.enabled 里不算生效
  local d name
  for d in "$REPO_ROOT"/plugins/*/; do
    [ -f "$d/__init__.py" ] || continue
    grep -qE "get_config\(['\"]enabled['\"]" "$d/__init__.py" 2>/dev/null || continue
    name="$(plugin_manifest_name "$d/plugin.yaml")"
    [ -n "$name" ] || name="$(basename "$d")"
    printf '%s\n' "$name"
  done
}

plugin_enable_official_target() {  # 官方 `hermes plugins enable` 能否精确寻址这个 profile
  PLUGIN_ENABLE_ROOT=""
  PLUGIN_ENABLE_PROFILE=""
  [ "$PLUGIN_ENABLE_MODE" != "merge" ] || return 1
  PLUGIN_ENABLE_BIN="$(command -v hermes || true)"
  [ -n "$PLUGIN_ENABLE_BIN" ] || return 1
  # `hermes --profile` 拒绝没有身份标记的目录；全新 profile 走最小合并，不猜官方路径。
  local t="$TARGET_HOME" marker root has_identity=0
  for marker in config.yaml .env SOUL.md profile.yaml auth.json state.db; do
    if [ -f "$t/$marker" ] || [ -L "$t/$marker" ]; then has_identity=1; break; fi
  done
  [ "$has_identity" = "1" ] || return 1
  if [ -d "$t/hermes-agent" ]; then
    PLUGIN_ENABLE_ROOT="$t"
    PLUGIN_ENABLE_PROFILE="default"
    return 0
  fi
  root="$(dirname "$(dirname "$t")")"
  if [ "$(basename "$(dirname "$t")")" = "profiles" ] \
     && { [ -d "$root/hermes-agent" ] || [ -d "$root/installs" ]; }; then
    PLUGIN_ENABLE_ROOT="$root"
    PLUGIN_ENABLE_PROFILE="$(basename "$t")"
    return 0
  fi
  return 1
}

official_plugin_enable() {  # 只对缺的项调用官方入口；失败返回 1（由调用方回退）
  local name out
  for name in "$@"; do
    if out="$(HERMES_HOME="$PLUGIN_ENABLE_ROOT" "$PLUGIN_ENABLE_BIN" \
              --profile "$PLUGIN_ENABLE_PROFILE" plugins enable "$name" 2>&1)"; then
      ok "启用 $name（官方入口：hermes --profile $PLUGIN_ENABLE_PROFILE plugins enable）"
    else
      warn "官方启用失败：$name —— 回退到最小文本合并"
      printf '%s\n' "$out" | while IFS= read -r line; do
        [ -n "$line" ] && log "      $line"
      done
      return 1
    fi
  done
  return 0
}

merge_plugin_enable() {  # 最小幂等合并；无变更时不写盘
  local -a args=(--config "$TARGET_HOME/config.yaml")
  local name status payload out rc=0
  for name in "${PLUGIN_ENABLE_NAMES[@]}"; do args+=(--name "$name"); done
  [ "$DRY_RUN" = "1" ] && args+=(--dry-run)
  out="$(python3 "$ENABLER" "${args[@]}")" || rc=$?
  if [ "$rc" = "2" ]; then
    warn "配置结构超出最小合并的安全范围（未改动）：$TARGET_HOME/config.yaml"
    warn "  人工执行：hermes${PLUGIN_ENABLE_PROFILE:+ --profile $PLUGIN_ENABLE_PROFILE} plugins enable <name>"
    return 0
  elif [ "$rc" != "0" ]; then
    fail "写插件启用记录失败（enable_plugins.py 退出码 $rc）"
    return 1
  fi
  while IFS=$'\t' read -r status payload; do
    case "$status" in
      ADD)             ok "启用 $payload" ;;
      ALREADY)         skip "已启用 $payload" ;;
      UNDISABLED)      ok "从 plugins.disabled 移除 $payload" ;;
      WOULD-ADD)       log "    [plan] 启用 $payload" ;;
      WOULD-UNDISABLE) log "    [plan] 从 plugins.disabled 移除 $payload" ;;
      WOULD-CONFIG)
        case "$payload" in
          create) log "    [plan] 新建 $TARGET_HOME/config.yaml（只含 plugins.enabled）" ;;
          update) log "    [plan] 更新 $TARGET_HOME/config.yaml 的 plugins.enabled" ;;
        esac ;;
      CONFIG)
        case "$payload" in
          created)   ok "新建配置（仅插件启用段）：$TARGET_HOME/config.yaml" ;;
          updated)   ok "更新插件启用段：$TARGET_HOME/config.yaml" ;;
          unchanged) skip "插件启用段已一致：$TARGET_HOME/config.yaml" ;;
        esac ;;
      STRUCTURE) : ;;
      *) [ -n "$status" ] && log "  $status${payload:+	$payload}" ;;
    esac
  done <<< "$out"
  return 0
}

enable_profile_plugins() {  # 先只读检查：全在就不再触发 CLI / 写盘
  local -a cargs=(--config "$TARGET_HOME/config.yaml" --check) missing=()
  local -a still=()
  local name status payload out rc=0
  for name in "${PLUGIN_ENABLE_NAMES[@]}"; do cargs+=(--name "$name"); done
  out="$(python3 "$ENABLER" "${cargs[@]}")" || rc=$?
  if [ "$rc" = "2" ]; then
    merge_plugin_enable
    return $?
  fi
  while IFS=$'\t' read -r status payload; do
    case "$status" in
      ABSENT|DISABLED) missing+=("$payload") ;;
    esac
  done <<< "$out"
  if [ "${#missing[@]}" -eq 0 ]; then
    for name in "${PLUGIN_ENABLE_NAMES[@]}"; do skip "已启用 $name"; done
    return 0
  fi
  if [ "$DRY_RUN" = "1" ]; then
    if plugin_enable_official_target; then
      for name in "${missing[@]}"; do
        log "    [plan] HERMES_HOME=$PLUGIN_ENABLE_ROOT hermes --profile $PLUGIN_ENABLE_PROFILE plugins enable $name"
      done
    elif [ "$PLUGIN_ENABLE_MODE" = "cli" ]; then
      warn "PLUGIN_ENABLE_MODE=cli 但官方入口不可用；dry-run 不写启用记录"
    else
      merge_plugin_enable || return $?
    fi
    return 0
  fi
  if plugin_enable_official_target && official_plugin_enable "${missing[@]}"; then
    # 官方命令写的是不是这个 profile，要复核；仍有缺口才回退合并。
    out="$(python3 "$ENABLER" "${cargs[@]}")" || rc=$?
    still=()
    while IFS=$'\t' read -r status payload; do
      case "$status" in
        ABSENT|DISABLED) still+=("$payload") ;;
      esac
    done <<< "$out"
    if [ "${#still[@]}" -gt 0 ]; then
      warn "官方入口后仍有未启用项：${still[*]} —— 回退最小合并"
      merge_plugin_enable || return $?
    fi
    return 0
  fi
  if [ "$PLUGIN_ENABLE_MODE" = "cli" ]; then
    warn "PLUGIN_ENABLE_MODE=cli：官方入口不可用，未写启用记录"
    warn "  人工执行：hermes --profile <profile> plugins enable <name>"
    return 0
  fi
  merge_plugin_enable
}

# ---------------------------------------------------------------------------- #
step "① 解析目标 Hermes"
if [ -n "$HERMES_HOME_ARG" ]; then
  TARGET_HOME="$HERMES_HOME_ARG"
elif [ -n "$PROFILE" ]; then
  TARGET_HOME="$HOME/.hermes/profiles/$PROFILE"
# 注意：解析结果存进 TARGET_HOME，**不要**写回 $HERMES_HOME。
# 调用方常常已经导出了 HERMES_HOME；同名赋值会继承 export 属性并传染给子进程 ——
# 而 `hermes --version` 这类子进程会据此在目标位置 bootstrap 出一整个 home 目录，
# 于是 --dry-run 就有了副作用（实测踩过，已修）。
elif [ -n "${HERMES_HOME:-}" ]; then
  TARGET_HOME="${HERMES_HOME:-}"
else
  TARGET_HOME="$HOME/.hermes"
fi

DEPLOY_DIR="${DEPLOY_DIR_ARG:-$TARGET_HOME/agentcore}"
SOUL_DEST="$TARGET_HOME/SOUL.md"
SKILLS_DEST="$TARGET_HOME/skills"
HERMES_AGENT_DIR="${HERMES_AGENT_DIR:-$HOME/.hermes/hermes-agent}"

log "  目标 home       = $TARGET_HOME"
log "  DEPLOY_DIR      = $DEPLOY_DIR"
log "  技能目标        = $SKILLS_DEST"
log "  Hermes 安装树   = $HERMES_AGENT_DIR"
log "  模式            = $([ "$DRY_RUN" = 1 ] && echo 'DRY-RUN（只打印）' || echo '真实安装')"

if command -v hermes >/dev/null 2>&1; then
  ok "Hermes CLI：$(command -v hermes)  $(hermes --version 2>/dev/null | head -1 || true)"
else
  warn "未找到 hermes CLI —— 资产可先落位，但补丁与技能要等 Hermes 装好再跑"
fi
if [ -d "$TARGET_HOME" ]; then
  ok "目标 home 存在"
else
  warn "目标 home 不存在，将创建：$TARGET_HOME"
  run mkdir -p "$TARGET_HOME"
fi
if [ -d "$HOME/.hermes/profiles" ]; then
  log "  已有 profile：$(ls "$HOME/.hermes/profiles" 2>/dev/null | tr '\n' ' ')"
fi

step "② 检查本仓源资产"
for need in identity/SOUL.generic.md identity/agents toolsets patches plugins \
            install/enable_plugins.py \
            patches/feishu_card_replies.patch patches/apply_feishu_card_replies.py \
            patches/patch_card_finalize.py skills; do
  if [ -e "$REPO_ROOT/$need" ]; then ok "$need"
  else fail "缺少源资产：$REPO_ROOT/$need"; exit 1; fi
done

# ---------------------------------------------------------------------------- #
step "③ 身份：通用骨架"
install_file "$REPO_ROOT/identity/SOUL.generic.md" "$DEPLOY_DIR/identity/SOUL.generic.md"

if [ -n "$SOUL_COMPOSE_ARG" ]; then
  if [ ! -f "$SOUL_COMPOSE_ARG" ]; then
    fail "--compose-soul 指向的文件不存在：$SOUL_COMPOSE_ARG"; exit 1
  fi
  log "  合成生效身份：骨架 + 业务叠加层（$SOUL_COMPOSE_ARG）"
  if [ -f "$SOUL_DEST" ]; then
    run cp -p "$SOUL_DEST" "$SOUL_DEST.bak.$(date +%Y%m%d-%H%M%S)"
    warn "现场身份已备份为 $SOUL_DEST.bak.<ts>"
  fi
  run mkdir -p "$(dirname "$SOUL_DEST")"
  # {{BUSINESS_OVERLAY}} 占位符 → 叠加层内容（追加到骨架之后则由叠加层自带标题）
  if [ "$DRY_RUN" = "1" ]; then
    log "    [plan] sed 替换 {{BUSINESS_OVERLAY}} 后写入 $SOUL_DEST"
  else
    python3 - "$REPO_ROOT/identity/SOUL.generic.md" "$SOUL_COMPOSE_ARG" "$SOUL_DEST" <<'PY'
import sys, pathlib
skel, overlay, dest = (pathlib.Path(p) for p in sys.argv[1:4])
text = skel.read_text(encoding="utf-8")
placeholder = "{{BUSINESS_OVERLAY}}"
occ = text.count(placeholder)
if occ != 1:
    sys.exit("骨架里 %s 应当恰好出现 1 次（实际 %d 次）—— 拒绝合成：多于一处会把叠加层塞进散文说明里"
             % (placeholder, occ))
body = overlay.read_text(encoding="utf-8")
dest.write_text(text.replace(placeholder, body), encoding="utf-8")
print("  合成完成：%s（骨架 %d + 叠加层 %d → %d 字节）"
      % (dest, len(text.encode()), len(body.encode()), dest.stat().st_size))
PY
  fi
  ok "生效身份已写：$SOUL_DEST"
elif [ "$DO_SOUL_LIVE" = "1" ]; then
  warn "--deploy-soul：只装通用骨架到 $SOUL_DEST —— **会丢掉业务叠加层**，仅全新机器可用"
  if [ -f "$SOUL_DEST" ]; then
    run cp -p "$SOUL_DEST" "$SOUL_DEST.bak.$(date +%Y%m%d-%H%M%S)"
  fi
  install_file "$REPO_ROOT/identity/SOUL.generic.md" "$SOUL_DEST"
else
  skip "未动现场 $SOUL_DEST（它上面有业务叠加层）"
  log "      要合成生效身份：--compose-soul <叠加层文件>；全新机器：--deploy-soul"
fi

# ---------------------------------------------------------------------------- #
step "④ 派工契约 -> $DEPLOY_DIR/agents"
install_tree "$REPO_ROOT/identity/agents" "$DEPLOY_DIR/agents"

step "⑤ 工具面 -> $DEPLOY_DIR/toolsets"
for d in "$REPO_ROOT"/toolsets/*/; do
  [ -d "$d" ] || continue
  install_tree "$d" "$DEPLOY_DIR/toolsets/$(basename "$d")"
done

step "⑤b 文档渲染引擎 -> $DEPLOY_DIR/tools"
if [ -d "$REPO_ROOT/tools" ]; then
  install_tree "$REPO_ROOT/tools" "$DEPLOY_DIR/tools"
else
  warn "tools/ 不存在：T4 渲染技能将需要 --engine-root 指向本体检出"
fi

step "⑥ 运行时基线 -> $DEPLOY_DIR/runtime"
if compgen -G "$REPO_ROOT/runtime/*" > /dev/null; then
  # 有什么拷什么：本目录的**文件清单由 runtime/README.md 的口径决定**（可能是「只有服务骨架」，
  # 也可能另有占位符化的键名骨架）。安装器不硬编码清单，免得清单在仓里漂移后安装器还按老清单走。
  install_tree "$REPO_ROOT/runtime" "$DEPLOY_DIR/runtime"
else
  warn "runtime/ 里没有文件：本仓不随仓提供整份 config/env 模板（模板由部署方按 runtime/README.md 自建）"
fi
warn "**不要**整文件覆盖现场 config.yaml（会冲掉内联的密钥）；按 runtime/README.md 逐键补齐。"

step "⑥b 通用技能 -> $TARGET_HOME/skills（示例技能不装）"
if [ "$DO_SKILLS" = "1" ]; then
  # --hermes-home puts the profile directly under that path; otherwise <home>/profiles/<profile>.
  if [ -n "$HERMES_HOME_ARG" ] && [ "$TARGET_HOME" = "$HERMES_HOME_ARG" ]; then
    SKILLS_TARGET=(--home "$TARGET_HOME")
  else
    SKILLS_TARGET=(--profile "$(basename "$TARGET_HOME")")
  fi
  if [ "$DRY_RUN" = "1" ]; then
    python3 "$REPO_ROOT/tools/install_skills.py" "${SKILLS_TARGET[@]}" --dry-run | while IFS= read -r line; do log "    [plan] $line"; done
  else
    python3 "$REPO_ROOT/tools/install_skills.py" "${SKILLS_TARGET[@]}" | while IFS= read -r line; do log "  $line"; done
  fi
else
  skip "通用技能（--skip-skills）"
fi

step "⑦ 补丁文件 -> $DEPLOY_DIR/patches"
install_tree "$REPO_ROOT/patches" "$DEPLOY_DIR/patches"

# ---------------------------------------------------------------------------- #
if [ "$DO_PLUGINS" = "1" ]; then
  step "⑧ 插件（按 profile）-> $TARGET_HOME/plugins"
  install_plugin_tree "$REPO_ROOT/plugins" "$TARGET_HOME/plugins"
  mapfile -t PLUGIN_ENABLE_NAMES < <(plugin_enable_names)
  if [ "${#PLUGIN_ENABLE_NAMES[@]}" -eq 0 ]; then
    warn "没有带 plugin.yaml 的插件可启用（localvec 没有 manifest，属 memory.provider，另行配置）"
  else
    enable_profile_plugins
  fi
  mapfile -t PLUGIN_SETTINGS_GATED < <(plugin_settings_gated_names)
  if [ "${#PLUGIN_SETTINGS_GATED[@]}" -gt 0 ]; then
    log "  以下插件以 settings.enabled 为总开关：只在 plugins.enabled 里**不算生效**，还要"
    log "  写 plugins.entries.<name>.settings.enabled: true（并按模板补 engine_root/queue_root/roles 等）："
    for name in "${PLUGIN_SETTINGS_GATED[@]}"; do
      log "    - $name    → plugins.entries.$name.settings   （模板见 plugins/*/README.md）"
    done
    log "  验收口径：**工具面里出现该工具**才算生效；配置文件里那一行不是证据："
    log "    hermes ${PROFILE:+-p $PROFILE }tools list   |   hermes ${PROFILE:+-p $PROFILE }plugins list"
  fi
else
  step "⑧ 跳过插件（--skip-plugins）：不落插件副本，也不写 plugins.enabled"
fi

# ---------------------------------------------------------------------------- #
if [ "$DO_SKILLS" = "1" ]; then
  step "⑨ 技能（通用）-> $SKILLS_DEST"
  install_skill_tree "$REPO_ROOT/skills" "通用" 1
  skip "skills/examples/ 是写法范本，不作为生产技能安装"

  if [ "$INSTALL_BUSINESS_SKILLS" = "1" ] && [ -n "$BUSINESS_SKILLS_SRC" ]; then
    log "  业务叠加层：INSTALL_BUSINESS_SKILLS=1，源=$BUSINESS_SKILLS_SRC"
    install_skill_tree "$BUSINESS_SKILLS_SRC" "业务"
  else
    skip "业务技能未安装（默认关闭）"
    log "      要装业务技能：INSTALL_BUSINESS_SKILLS=1 BUSINESS_SKILLS_SRC=<业务技能源目录>"
  fi
else
  step "⑨ 跳过技能（--skip-skills）"
fi

# ---------------------------------------------------------------------------- #
if [ "$DO_PATCH" = "1" ]; then
  step "⑩ 消息平台适配补丁（卡片流式 + 审批纯文字 + 卡片终态/同卡 + 原生表格/Card2.0）"
  APPLY="$REPO_ROOT/patches/apply_feishu_card_replies.py"
  FINALIZE="$REPO_ROOT/patches/patch_card_finalize.py"
  NATIVE="$REPO_ROOT/patches/patch_card_markdown_native.py"
  if [ "$DRY_RUN" = "1" ]; then
    log "    [plan] HERMES_AGENT_DIR=$HERMES_AGENT_DIR python3 $APPLY --check"
    log "    [plan] HERMES_AGENT_DIR=$HERMES_AGENT_DIR python3 $APPLY --apply"
    log "    [plan] HERMES_PROFILE_DIR=$TARGET_HOME python3 $APPLY --config"
    log "    [plan] python3 $FINALIZE --check --agent-dir $HERMES_AGENT_DIR"
    log "    [plan] python3 $FINALIZE --apply --agent-dir $HERMES_AGENT_DIR   （仅当 --check 不在位）"
    log "    [plan] python3 $NATIVE --check --agent-dir $HERMES_AGENT_DIR"
    log "    [plan] python3 $NATIVE --apply --agent-dir $HERMES_AGENT_DIR   （仅当 --check 不在位）"
  elif [ ! -d "$HERMES_AGENT_DIR" ]; then
    warn "Hermes 安装树不存在（$HERMES_AGENT_DIR），跳过补丁。装好 Hermes 后重跑本步。"
  else
    if HERMES_AGENT_DIR="$HERMES_AGENT_DIR" python3 "$APPLY" --check; then
      skip "卡片流式补丁已整块在位"
    elif HERMES_AGENT_DIR="$HERMES_AGENT_DIR" python3 "$APPLY" --apply; then
      ok "卡片流式补丁已重放（**需重启网关**才生效）"
    else
      fail "卡片流式补丁重放失败（上游 adapter.py 可能已变动）；人工核对后用 git apply --3way"
    fi
    if python3 "$FINALIZE" --check --agent-dir "$HERMES_AGENT_DIR"; then
      skip "卡片终态/同卡补丁已整块在位"
    elif python3 "$FINALIZE" --apply --agent-dir "$HERMES_AGENT_DIR"; then
      ok "卡片终态/同卡补丁已重放（**需重启网关**才生效）"
    else
      fail "卡片终态/同卡补丁重放失败（上游锚点可能已漂移，退出码 2）；人工核对后重跑"
    fi
    if python3 "$NATIVE" --check --agent-dir "$HERMES_AGENT_DIR"; then
      skip "卡片原生表格补丁已整块在位"
    elif python3 "$NATIVE" --apply --agent-dir "$HERMES_AGENT_DIR"; then
      ok "卡片原生表格补丁已重放（**需重启网关**才生效）"
    else
      fail "卡片原生表格补丁重放失败（上游锚点可能已漂移，退出码 2）；人工核对后重跑"
    fi
  fi
else
  step "⑩ 跳过补丁（--skip-patch）"
fi

# ---------------------------------------------------------------------------- #
if [ "$DO_BASELINE" = "1" ]; then
  step "⑪ 能力基线（显式开关；只走官方设置命令）"
  if ! command -v hermes >/dev/null 2>&1; then
    warn "没有 hermes CLI，跳过基线写入。装好 Hermes 后重跑：bash install/install.sh --apply-baseline"
  else
    HCMD=(hermes)
    [ -n "$PROFILE" ] && HCMD+=(--profile "$PROFILE")
    for kv in "agent.max_turns 1000" \
              "delegation.max_iterations 250" \
              "delegation.max_concurrent_children 8" \
              "delegation.max_spawn_depth 3" \
              "approvals.mode off" \
              "approvals.timeout 900" \
              "agent.restart_drain_timeout 300"; do
      k="${kv%% *}"; v="${kv#* }"
      cur="$("${HCMD[@]}" config get "$k" 2>/dev/null | tr -d '"' | head -1 || true)"
      if [ "$cur" = "$v" ]; then
        skip "$k 已在基线（$v）"
      elif [ -n "$cur" ] && [[ "$cur" =~ ^[0-9]+$ ]] && [[ "$v" =~ ^[0-9]+$ ]] && [ "$cur" -ge "$v" ]; then
        skip "$k 已是 $cur（≥ 地板 $v）—— 地板**只抬不降**，绝不把现场已有的高值改小"
      elif [ "$DRY_RUN" = "1" ]; then
        printf '    [plan] hermes%s config set %s %s   （当前：%s）\n' \
               "${PROFILE:+ -p $PROFILE}" "$k" "$v" "${cur:-<未设>}"
      elif "${HCMD[@]}" config set "$k" "$v" >/dev/null 2>&1; then
        ok "$k: ${cur:-<未设>} -> $v"
      else
        warn "$k 写入失败 —— 手工执行：hermes${PROFILE:+ -p $PROFILE} config set $k $v"
      fi
    done
    log "  复原口径：逐条 hermes${PROFILE:+ -p $PROFILE} config set <key> <原值>（写进部署记录）"
  fi
  warn "写保护开关（security.protected_instruction_files）**不**在本步改动："
  warn "  它是提示注入的持久化防线，关闭属**安全降级** —— 人工决定 + 留痕，详见 runtime/BASELINE.md"
else
  step "⑪ 跳过能力基线（未给 --apply-baseline；默认不动现场配置）"
fi

# ---------------------------------------------------------------------------- #
step "⑫ 需要人工执行的步骤（本脚本不启停任何服务）"
cat <<EOF

  当前 profile      : ${PROFILE:-default（未指定 --profile）}
  Hermes home       : $TARGET_HOME

  # 1) 凭据（不要提交、不要 echo；chmod 600）
  #    由部署方按 runtime/README.md 的口径自建（键名清单，值留空）：
  #      ${HERMES_HOME:-$TARGET_HOME}/<mcp-env-name>.env   （业务 MCP 子进程环境；业务键名由业务叠加层给出）
  #      ${HERMES_HOME:-$TARGET_HOME}/channels.env         （网关的消息平台凭据）
  #    业务 provider / 业务 MCP / 业务技能目录按 runtime/README.md 逐键补齐。

  # 2) 网关常驻（user 级 —— 系统级无 DBUS/keyring，需凭据的 CLI 会 authenticated:false）
  hermes ${PROFILE:+-p $PROFILE }gateway install --start-on-login
  loginctl enable-linger $(id -un)        # SSH 退出后服务仍常驻（关键）
  systemctl --user daemon-reload
  systemctl --user enable --now hermes-gateway-${PROFILE:-default}.service
  systemctl --user status  hermes-gateway-${PROFILE:-default}.service

  # 3) 不要 cp runtime/hermes-agent.service 当常驻单元（那是骨架；现场用上面 hermes gateway install）

  # 4) 生效时机
  #    - 身份文件：**每条消息重新读取**，合成后即生效，无需重启
  #    - 适配器补丁：需重启网关
  #    - 插件启用：运行时基线；⑧ 已写 plugins.enabled，需新会话/网关重载才加载

  # 5) 插件验收与部署侧设置（本脚本不猜业务路径）
  #    - 验收口径：**工具面里出现该工具**才算生效；配置文件里那一行不算证据
  #    - 官方改法：hermes ${PROFILE:+-p $PROFILE }plugins enable <name>
  #    - plugins.entries.<name>.settings（engine_root / queue_root / allowed_roots 等）
  #      仍是部署配置，按 plugins/<name>/README.md 的模板补齐；本脚本只写 enabled/disabled
  #    - localvec 不是 plugins.enabled 项：配 memory.provider: localvec 及其 recall_cmd

  # 6) 升级后首查：后台完成回执有没有真的回到会话（只读，逐个 durable 队列根跑）
  #    PYTHONPATH=<agentcore checkout> python3 -m agentcore.completion_backfill \
  #      --root <queue_root> --profile ${PROFILE:-default}
  #    - 队列根 = plugins.entries.durable-dispatch.settings.queue_root，以及每个 colleague 的 queue_root
  #    - deliverable=false 的条目 = **永远不会自动投递的存量无主回执**（历史遗留 / 别的 profile）
  #      → 按 docs/COMPLETION_DELIVERY.md 的 §2 走带证明的管理性修复
  #    - 投递健康：积压连续 60s 未清会在共享 ops-alert outbox 入一条 fault 事件；
  #      宿主需跑 agentcore.ops_alert_daemon/发送器才会真正送达（否则只堆在库里 + 一条 WARNING）
EOF

step "完成"
if [ "$DRY_RUN" = "1" ]; then
  log "  DRY-RUN 结束：以上 [plan] 行是唯一会发生的动作（其实没发生）。"
else
  log "  通用资产已落位。上面 ⑫ 的人工步骤仍需执行。"
fi