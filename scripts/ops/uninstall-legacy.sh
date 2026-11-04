#!/usr/bin/env bash
# ==============================================================================
# uninstall-legacy.sh
# 彻底清理历史单体服务、野路径软链和孤立配置的幂等对账清理脚本
# 适用范围: Emily (白泽节点) 与 本机 WSL2
# ==============================================================================
set -euo pipefail

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
    echo "[INFO] Running in DRY-RUN mode. No changes will be applied."
fi

SYSTEMD_DIR="${HOME}/.config/systemd/user"

# 1. 废弃的单体服务黑名单（已统一收敛至 release 引擎模板 imfl-colleague@ 或 superagent 矩阵）
LEGACY_SERVICES=(
    "analysis-colleague.service"
    "hermes-gw-reload-once.service"
    "agentcore-colleague.service"
    "hermes-gateway-imfl.service.superagent-before"
)

echo "=== [Step 1/4] 清理历史僵尸 / 废弃单体 systemd 服务 ==="
RELOAD_NEEDED=0
for svc in "${LEGACY_SERVICES[@]}"; do
    svc_file="${SYSTEMD_DIR}/${svc}"
    if [[ -f "${svc_file}" ]] || systemctl --user list-unit-files "${svc}" &>/dev/null; then
        echo "发现历史废弃服务: ${svc}"
        if [[ "${DRY_RUN}" -eq 0 ]]; then
            systemctl --user stop "${svc}" 2>/dev/null || true
            systemctl --user disable "${svc}" 2>/dev/null || true
            if [[ -f "${svc_file}" ]]; then
                rm -f "${svc_file}"
                echo "  已删除 unit 文件: ${svc_file}"
            fi
            RELOAD_NEEDED=1
        else
            echo "  [DRY-RUN] 将执行: systemctl stop/disable 并删除 ${svc_file}"
        fi
    fi
done

# 2. 检查现存 systemd 服务中是否含有指向野生 ~/code/agentcore 的工作路径或 PYTHONPATH
echo "=== [Step 2/4] 检查并纠偏现存服务的硬编码野生路径 ==="
if [[ -d "${SYSTEMD_DIR}" ]]; then
    DIRTY_UNITS=$(grep -rnE '(code/agentcore)' "${SYSTEMD_DIR}" 2>/dev/null | cut -d: -f1 | sort -u || true)
    if [[ -n "${DIRTY_UNITS}" ]]; then
        echo "[WARNING] 发现以下服务文件仍含有野生 code/agentcore 路径:"
        echo "${DIRTY_UNITS}"
        if [[ "${DRY_RUN}" -eq 0 ]]; then
            for unit in ${DIRTY_UNITS}; do
                echo "  正在将 ${unit} 中的 code/agentcore 纠偏为 current 规范路径..."
                sed -i 's|/home/emily/code/agentcore|/mnt/nvme_ntfs/agents/hermes/superagent/imfl/current|g' "${unit}"
                RELOAD_NEEDED=1
            done
        else
            echo "  [DRY-RUN] 将自动替换为 /mnt/nvme_ntfs/agents/hermes/superagent/imfl/current"
        fi
    else
        echo "现存所有 systemd 服务路径规范，无野生 code/agentcore 引用。"
    fi
fi

if [[ "${RELOAD_NEEDED}" -eq 1 ]]; then
    echo "重新加载 systemd daemon..."
    systemctl --user daemon-reload
fi

# 3. 检查并清理损坏/孤立的插件死符号链接
echo "=== [Step 3/4] 校验 profile 插件与运行时软链健康度 ==="
PROFILE_PLUGINS_DIR="/mnt/nvme_ntfs/agents/hermes/profiles/imfl/plugins"
if [[ -d "${PROFILE_PLUGINS_DIR}" ]]; then
    find "${PROFILE_PLUGINS_DIR}" -xtype l | while read -r deadlink; do
        echo "发现断裂软链: ${deadlink}"
        if [[ "${DRY_RUN}" -eq 0 ]]; then
            rm -f "${deadlink}"
            echo "  已删除死链: ${deadlink}"
        else
            echo "  [DRY-RUN] 将删除死链: ${deadlink}"
        fi
    done
fi

# 4. 检查全局配置与 Key 隔离（防止多重 .env 互相覆盖）
echo "=== [Step 4/4] 检查散落的环境变量与代理配置 ==="
SCATTERED_ENVS=(
    "${HOME}/.hermes/profiles/imfl/proxy.env"
    "${HOME}/code/agentcore/agentcore/proxy.env"
)

for envfile in "${SCATTERED_ENVS[@]}"; do
    if [[ -f "${envfile}" ]]; then
        echo "发现散落的旧覆盖配置: ${envfile}"
        if [[ "${DRY_RUN}" -eq 0 ]]; then
            mv "${envfile}" "${envfile}.bak-legacy-$(date +%Y%m%d%H%M%S)"
            echo "  已备份并移出生效区: ${envfile}"
        else
            echo "  [DRY-RUN] 将归档废弃配置: ${envfile}"
        fi
    fi
done

echo "======================================================================"
echo "清理与对账完成！系统运行时已全部锁定为 Single Source of Truth。"
echo "======================================================================"
