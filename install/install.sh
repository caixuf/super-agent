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
#
# 用法
#   bash install/install.sh --help
#   bash install/install.sh --dry-run                        # 看计划
#   bash install/install.sh --profile <profile>              # 真装（通用 + 示例）
#   bash install/install.sh --profile <profile> --skip-plugins
#   bash install/install.sh --profile <profile> --compose-soul /path/to/overlay.md
#   INSTALL_BUSINESS_SKILLS=1 BUSINESS_SKILLS_SRC=<dir> bash install/install.sh
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
  --skip-plugins            不装按 profile 的插件副本
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
  7. skills/**/SKILL.md       -> <HERMES_HOME>/skills/<name>/    （通用技能；examples/ 是范本，不安装）
  8. 对 Hermes 安装树打补丁（可 --skip-patch）：
       patches/apply_feishu_card_replies.py --apply（卡片流式 + 审批纯文字）
       patches/patch_card_finalize.py --apply（卡片终态 / 整轮同一张卡）

业务叠加层（默认不装）:
  业务技能：INSTALL_BUSINESS_SKILLS=1 + BUSINESS_SKILLS_SRC=<dir>（或 --business-skills-src）
  业务 MCP / 业务 provider / 业务 SOUL 叠加：由业务层按 runtime/README.md 与
  identity/README.md 的占位符契约补齐。**本脚本不生成任何业务内容。**

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

step "⑥ 运行时基线 -> $DEPLOY_DIR/runtime"
if compgen -G "$REPO_ROOT/runtime/*" > /dev/null; then
  # 有什么拷什么：本目录的**文件清单由 runtime/README.md 的口径决定**（可能是「只有服务骨架」，
  # 也可能另有占位符化的键名骨架）。安装器不硬编码清单，免得清单在仓里漂移后安装器还按老清单走。
  install_tree "$REPO_ROOT/runtime" "$DEPLOY_DIR/runtime"
else
  warn "runtime/ 里没有文件：本仓不随仓提供整份 config/env 模板（模板由部署方按 runtime/README.md 自建）"
fi
warn "**不要**整文件覆盖现场 config.yaml（会冲掉内联的密钥）；按 runtime/README.md 逐键补齐。"

step "⑦ 补丁文件 -> $DEPLOY_DIR/patches"
install_tree "$REPO_ROOT/patches" "$DEPLOY_DIR/patches"

# ---------------------------------------------------------------------------- #
if [ "$DO_PLUGINS" = "1" ]; then
  step "⑧ 插件（按 profile）-> $TARGET_HOME/plugins"
  install_plugin_tree "$REPO_ROOT/plugins" "$TARGET_HOME/plugins"
else
  step "⑧ 跳过插件（--skip-plugins）"
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
  step "⑩ 消息平台适配补丁（卡片流式 + 审批纯文字 + 卡片终态/同卡）"
  APPLY="$REPO_ROOT/patches/apply_feishu_card_replies.py"
  FINALIZE="$REPO_ROOT/patches/patch_card_finalize.py"
  if [ "$DRY_RUN" = "1" ]; then
    log "    [plan] HERMES_AGENT_DIR=$HERMES_AGENT_DIR python3 $APPLY --check"
    log "    [plan] HERMES_AGENT_DIR=$HERMES_AGENT_DIR python3 $APPLY --apply"
    log "    [plan] HERMES_PROFILE_DIR=$TARGET_HOME python3 $APPLY --config"
    log "    [plan] python3 $FINALIZE --check --agent-dir $HERMES_AGENT_DIR"
    log "    [plan] python3 $FINALIZE --apply --agent-dir $HERMES_AGENT_DIR   （仅当 --check 不在位）"
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
              "approvals.timeout 900"; do
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
EOF

step "完成"
if [ "$DRY_RUN" = "1" ]; then
  log "  DRY-RUN 结束：以上 [plan] 行是唯一会发生的动作（其实没发生）。"
else
  log "  通用资产已落位。上面 ⑫ 的人工步骤仍需执行。"
fi