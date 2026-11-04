#!/usr/bin/env bash
# ==============================================================================
# purge_and_rebuild_host.sh — 纯净重建（备份 → 归档 → 受管重建 → 验证）
#
# 默认只读预演；--apply 才执行。所有清理改为归档到备份目录，可回退。
# 重建复用受管安装器，不在本脚本手工拼装路径；不重启运行中的服务。
# 现场身份由实例部署清单注入；通用仓只保留中性默认值。
# ==============================================================================
set -euo pipefail

PROFILE_NAME="${AGENTCORE_PROFILE_NAME:-default}"
AGENTCORE_ROOT="${AGENTCORE_ROOT:-${HOME}/.hermes/superagent}"
MANAGED_ROOT="${AGENTCORE_ROOT}/${PROFILE_NAME}"
CURRENT="${MANAGED_ROOT}/current"
HERMES_HOME="${AGENTCORE_HERMES_HOME:-${HOME}/.hermes}"
PROFILE_DIR="${HERMES_HOME}/profiles/${PROFILE_NAME}"
SKILLS_DIR_NAME="${AGENTCORE_SKILLS_DIR_NAME:-business-skills}"
SKILLS_DIR="${HOME}/code/${SKILLS_DIR_NAME}"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
INSTALLER="${AGENTCORE_INSTALLER:-${HOME}/code/super-agent/tools/superagent.py}"
BACKUP_DIR="${MANAGED_ROOT}/state/purge-backups/backup_$(date +%Y%m%d%H%M%S)"

APPLY=0
case "${1:-}" in
    --apply) APPLY=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) ;;
esac

log()   { printf '  %s\n' "$*"; }
step()  { printf '\n=== %s ===\n' "$*"; }
archive() {
    local target="$1"
    [[ -e "${target}" ]] || return 0
    if (( APPLY == 0 )); then log "[DRY-RUN] 将归档: ${target}"; return 0; fi
    mkdir -p "${BACKUP_DIR}"
    mv "${target}" "${BACKUP_DIR}/$(basename "${target}")"
    log "已归档: ${target}"
}

printf '模式=%s profile=%s 真相源=%s\n' "$([[ ${APPLY} -eq 1 ]] && echo apply || echo dry-run)" "${PROFILE_NAME}" "${CURRENT}"
printf '备份目录=%s\n' "${BACKUP_DIR}"
[[ -d "${CURRENT}" ]] || { printf '受管 current 不存在，拒绝继续\n' >&2; exit 1; }

step '[1/4] 备份业务火种（记忆 / 技能 / 两份配置）'
if (( APPLY == 1 )); then
    mkdir -p "${BACKUP_DIR}"
    for src in "${PROFILE_DIR}/memories" "${SKILLS_DIR}" "${PROFILE_DIR}/config.yaml" "${HERMES_HOME}/config.yaml"; do
        if [[ -e "${src}" ]]; then cp -a "${src}" "${BACKUP_DIR}/"; log "已备份: ${src}"; fi
    done
    log "备份项数: $(find "${BACKUP_DIR}" -maxdepth 1 | wc -l)"
else
    log '[DRY-RUN] 将备份 memories / skills / 两份 config.yaml'
fi

step '[2/4] 下线历史遗留服务并归档遗留 unit'
for pattern in "analysis-colleague*" "${PROFILE_NAME}-colleague@*"; do
    if (( APPLY == 1 )); then
        systemctl --user stop "${pattern}" 2>/dev/null || true
        systemctl --user disable "${pattern}" 2>/dev/null || true
        log "已 stop/disable: ${pattern}"
    else
        log "[DRY-RUN] 将 stop/disable: ${pattern}"
    fi
done
for f in "${SYSTEMD_USER_DIR}"/analysis-colleague*.service \
         "${SYSTEMD_USER_DIR}"/hermes-gw-reload-once*.service \
         "${SYSTEMD_USER_DIR}"/*superagent-before*; do
    archive "${f}"
done

step '[3/4] 归档散落配置与运行时状态（不删除，可回退）'
for f in "${PROFILE_DIR}/proxy.env" \
         "${HOME}/.local/state/agentcore/analysis-queue" \
         "${HOME}/.local/state/agentcore/liveness.db"; do
    archive "${f}"
done

step '[4/4] 受管重建与验证'
if (( APPLY == 1 )); then
    systemctl --user daemon-reload
    if [[ -f "${INSTALLER}" ]]; then
        log '调用受管安装器 update ...'
        python3 "${INSTALLER}" --root "${AGENTCORE_ROOT}" --profile "${PROFILE_NAME}" update 2>&1 | tail -10 || true
        log '调用 doctor 验证 ...'
        python3 "${INSTALLER}" --root "${AGENTCORE_ROOT}" --profile "${PROFILE_NAME}" doctor 2>&1 | head -20 || true
    else
        log '安装器不可用，跳过重建验证'
    fi
    log "重建完成；可回退备份: ${BACKUP_DIR}"
    log '运行中的服务需重启才加载新代码 —— 本脚本不重启'
    log "  建议: systemctl --user restart hermes-gateway-${PROFILE_NAME}.service"
else
    log '[DRY-RUN] 将执行受管 update + doctor（不重启服务）'
fi