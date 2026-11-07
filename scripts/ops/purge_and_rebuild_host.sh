#!/usr/bin/env bash
# ==============================================================================
# purge_and_rebuild_host.sh — 纯净重建（备份 → 归档 → 重建 → 验证）
#
# 默认只读预演；--apply 才执行。所有清理改为归档到备份目录，可回退。
# 重建复用官方安装器，不在本脚本手工拼装路径；不重启运行中的服务。
#
# 布局判定规则与 agentcore/profile_spec.py 同源（见 scripts/ops/_layout.sh）：
# 显式 AGENTCORE_ENGINE_ROOT > 受管 current > ${HERMES_HOME}/agentcore。
# 受管布局用受管安装器重建；官方 install/install.sh 单写者布局用官方 install.sh 重建。
#
# 现场身份由实例部署清单注入；通用仓只保留中性默认值。
# ==============================================================================
set -euo pipefail

PROFILE_NAME="${AGENTCORE_PROFILE_NAME:-default}"
AGENTCORE_ROOT="${AGENTCORE_ROOT:-${HOME}/super_agent}"
MANAGED_ROOT="${AGENTCORE_ROOT}/${PROFILE_NAME}"
CURRENT="${MANAGED_ROOT}/current"
HERMES_HOME="${AGENTCORE_HERMES_HOME:-${HOME}/.hermes}"
PROFILE_DIR="${HERMES_HOME}/profiles/${PROFILE_NAME}"
SKILLS_DIR_NAME="${AGENTCORE_SKILLS_DIR_NAME:-business-skills}"
SKILLS_DIR="${HOME}/code/${SKILLS_DIR_NAME}"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
INSTALLER="${AGENTCORE_INSTALLER:-${HOME}/code/super-agent/tools/superagent.py}"
WORKTREE_REPO="${AGENTCORE_WORKTREE_REPO:-${HOME}/code/super-agent}"
BACKUP_DIR="${MANAGED_ROOT}/state/purge-backups/backup_$(date +%Y%m%d%H%M%S)"

# 共用的「布局 / 真相源」解析
# shellcheck source=scripts/ops/_layout.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_layout.sh"

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

printf '模式=%s profile=%s\n' "$([[ ${APPLY} -eq 1 ]] && echo apply || echo dry-run)" "${PROFILE_NAME}"
printf '备份目录=%s\n' "${BACKUP_DIR}"
if ! resolve_layout; then
    exit 1
fi
layout_report

step '[1/4] 备份业务火种（记忆 / 技能 / 配置）'
if (( APPLY == 1 )); then
    mkdir -p "${BACKUP_DIR}"
    while IFS= read -r src; do
        [[ -n "${src}" ]] || continue
        if [[ -e "${src}" ]]; then cp -a "${src}" "${BACKUP_DIR}/"; log "已备份: ${src}"; fi
    done < <(layout_backup_sources)
    log "备份项数: $(find "${BACKUP_DIR}" -maxdepth 1 | wc -l)"
else
    log '[DRY-RUN] 将备份记忆 / 技能 / 配置'
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
while IFS= read -r f; do
    [[ -n "${f}" ]] || continue
    archive "${f}"
done < <(layout_archive_sources)

step '[4/4] 重建与验证'
if (( APPLY == 1 )); then
    systemctl --user daemon-reload
    if [[ "${LAYOUT}" == "managed" ]]; then
        if [[ -f "${INSTALLER}" ]]; then
            log '调用受管安装器 update ...'
            python3 "${INSTALLER}" --root "${AGENTCORE_ROOT}" --profile "${PROFILE_NAME}" update 2>&1 | tail -10 || true
            log '调用 doctor 验证 ...'
            python3 "${INSTALLER}" --root "${AGENTCORE_ROOT}" --profile "${PROFILE_NAME}" doctor 2>&1 | head -20 || true
        else
            log '安装器不可用，跳过重建验证'
        fi
    else
        # 单写者布局：重建交回官方安装器（单一写者），本脚本不手工拼装引擎目录
        if [[ -f "${WORKTREE_REPO}/install/install.sh" ]]; then
            if hermes_tree="$(resolve_hermes_tree)"; then
                log "调用官方 install.sh 重建（HERMES_AGENT_DIR=${hermes_tree}）..."
                HERMES_AGENT_DIR="${hermes_tree}" bash "${WORKTREE_REPO}/install/install.sh" \
                    --hermes-home "${HERMES_HOME}" 2>&1 | tail -12 || true
            else
                log '找不到在役 Hermes 安装树，跳过重建（可用 AGENTCORE_HERMES_TREE 显式指定）'
            fi
            log '调用官方就绪门禁验证 ...'
            verify_cmd="$(layout_verification_command | sed -n '2p')"
            eval "${verify_cmd}" 2>&1 | head -20 || log '就绪门禁未全绿（见上方逐项状态）'
        else
            log "找不到官方安装器 ${WORKTREE_REPO}/install/install.sh，跳过重建"
        fi
    fi
    log "重建完成；可回退备份: ${BACKUP_DIR}"
    log '运行中的服务需重启才加载新代码 —— 本脚本不重启'
    log "  建议: systemctl --user restart hermes-gateway-${PROFILE_NAME}.service"
else
    if [[ "${LAYOUT}" == "managed" ]]; then
        log '[DRY-RUN] 将执行受管 update + doctor（不重启服务）'
    else
        log '[DRY-RUN] 将执行官方 install.sh 重建 + 官方就绪门禁（不重启服务）'
    fi
fi