#!/usr/bin/env bash
# ==============================================================================
# uninstall-legacy.sh — 配置漂移对账与纠偏（幂等）
#
# 单一真相源：受管 release 的 current 软链。服务的 PYTHONPATH / engine_root /
# launcher 一旦指向工作区源码或固定版本快照，就是漂移。
#
# 用法：
#   ./uninstall-legacy.sh                 只读对账（默认，不改任何东西）
#   ./uninstall-legacy.sh --apply        备份后纠偏；不重启服务
#   ./uninstall-legacy.sh --rollback     从最近备份恢复 unit 文件
#
# 现场身份由实例部署清单注入；通用仓只保留中性默认值。
# ==============================================================================
set -euo pipefail

PROFILE_NAME="${AGENTCORE_PROFILE_NAME:-default}"
AGENTCORE_ROOT="${AGENTCORE_ROOT:-${HOME}/.hermes/superagent}"
MANAGED_ROOT="${AGENTCORE_ROOT}/${PROFILE_NAME}"
CURRENT="${MANAGED_ROOT}/current"
WORKTREE_REPO="${AGENTCORE_WORKTREE_REPO:-${HOME}/code/super-agent}"
HERMES_HOME="${AGENTCORE_HERMES_HOME:-${HOME}/.hermes}"
SYSTEMD_DIR="${HOME}/.config/systemd/user"
BACKUP_ROOT="${MANAGED_ROOT}/state/drift-backups"
INSTALLER="${AGENTCORE_INSTALLER:-${WORKTREE_REPO}/tools/superagent.py}"

MODE="report"
case "${1:-}" in
    --apply) MODE="apply" ;;
    --rollback) MODE="rollback" ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) ;;
esac

BACKUP_DIR="${BACKUP_ROOT}/backup_$(date +%Y%m%d%H%M%S)"
DRIFT=0
note()  { printf '  %s\n' "$*"; }
drift() { DRIFT=$((DRIFT+1)); printf '  [DRIFT] %s\n' "$*"; }
step()  { printf '\n=== %s ===\n' "$*"; }
backup_unit() { mkdir -p "${BACKUP_DIR}"; cp -a "$1" "${BACKUP_DIR}/$(basename "$1")"; }

if [[ "${MODE}" == "rollback" ]]; then
    latest="$(ls -1dt "${BACKUP_ROOT}"/backup_* 2>/dev/null | head -1 || true)"
    if [[ -z "${latest}" || ! -d "${latest}" ]]; then
        printf '无可回退备份: %s\n' "${BACKUP_ROOT}" >&2; exit 1
    fi
    cp -a "${latest}"/. "${SYSTEMD_DIR}/"
    systemctl --user daemon-reload
    printf '已从 %s 恢复 unit 文件（未重启服务）\n' "${latest}"
    exit 0
fi

printf '模式=%s profile=%s 真相源=%s\n' "${MODE}" "${PROFILE_NAME}" "${CURRENT}"
[[ -d "${CURRENT}" ]] || { printf '受管 current 不存在，拒绝继续\n' >&2; exit 1; }

step "[1/5] 服务代码来源：工作区 vs 受管 current"
if [[ -d "${SYSTEMD_DIR}" ]]; then
    mapfile -t units < <(grep -rlE "PYTHONPATH=.*${WORKTREE_REPO}" "${SYSTEMD_DIR}" 2>/dev/null || true)
    if (( ${#units[@]} == 0 )); then note '无服务 PYTHONPATH 指向工作区'; else
        for u in "${units[@]}"; do
            drift "PYTHONPATH 指向工作区: ${u}"
            note "  应改为: ${CURRENT}"
            if [[ "${MODE}" == "apply" ]]; then
                backup_unit "${u}"
                sed -i "s|PYTHONPATH=${WORKTREE_REPO}|PYTHONPATH=${CURRENT}|g" "${u}"
                note "  已纠偏（备份 ${BACKUP_DIR}）"
            fi
        done
    fi
fi

step "[2/5] 遗留 unit 与历史服务"
legacy=()
for f in "${SYSTEMD_DIR}"/*.superagent-before "${SYSTEMD_DIR}"/hermes-gw-reload-once*.service; do
    if [[ -e "${f}" ]]; then legacy+=("${f}"); fi
done
if (( ${#legacy[@]} == 0 )); then note '无遗留 unit'; else
    for f in "${legacy[@]}"; do
        drift "遗留 unit: ${f}"
        if [[ "${MODE}" == "apply" ]]; then
            backup_unit "${f}"; mv "${f}" "${BACKUP_DIR}/"
            note '  已归档到备份目录'
        fi
    done
fi

step "[3/5] 配置来源一致性"
cfg_a="${HERMES_HOME}/config.yaml"
cfg_b="${HERMES_HOME}/profiles/${PROFILE_NAME}/config.yaml"
if [[ -f "${cfg_a}" && -f "${cfg_b}" ]]; then
    if cmp -s "${cfg_a}" "${cfg_b}"; then note '两份配置一致'; else
        drift '两份 config.yaml 内容不同：实例名不决定实际配置来源'
        note "  A=${cfg_a}"
        note "  B=${cfg_b}"
        note '  本脚本不自动合并（可能有意为之），请由部署清单指定唯一来源'
    fi
fi

step "[4/5] 散落覆盖配置 (proxy.env)"
found=0
while IFS= read -r f; do
    [[ -n "${f}" ]] || continue
    found=1
    drift "散落覆盖: ${f}"
    if [[ "${MODE}" == "apply" ]]; then
        backup_unit "${f}"
        mv "${f}" "${f}.bak-legacy-$(date +%Y%m%d%H%M%S)"
        note '  已归档'
    fi
done < <(find "${HERMES_HOME}/profiles" -maxdepth 4 -name proxy.env -type f 2>/dev/null || true)
if (( found == 0 )); then note '无散落 proxy.env'; fi

step "[5/5] 受管 doctor 验证"
if [[ -f "${INSTALLER}" ]]; then
    python3 "${INSTALLER}" --root "${AGENTCORE_ROOT}" --profile "${PROFILE_NAME}" doctor 2>&1 | head -25 || true
else
    note '安装器不可用，跳过 doctor'
fi

step '结果'
printf '  模式=%s 漂移项=%d\n' "${MODE}" "${DRIFT}"
if [[ "${MODE}" == "apply" ]]; then
    systemctl --user daemon-reload
    note "已 daemon-reload；unit 备份: ${BACKUP_DIR}"
    note '运行中的服务需重启才加载新代码 —— 本脚本不重启，避免打断会话'
    note '误改可用 --rollback 回退'
else
    note '只读模式。确认清单后加 --apply 执行；误改可用 --rollback 回退'
fi