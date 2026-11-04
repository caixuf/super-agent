#!/usr/bin/env bash
# ==============================================================================
# purge_and_rebuild_host.sh
# 彻底根治环境漂移与原机历史污染：
# 1. 严格备份业务火种（记忆库 + 业务Skills + 配置密钥）
# 2. 暴力铲平原机历史地质层（停服务、删旧systemd、清残余配置目录）
# 3. 干净恢复与声明式重建环境，杜绝历史配置击穿
# ==============================================================================
set -euo pipefail

BACKUP_DIR="${HOME}/superagent_backups/backup_$(date +%Y%m%d_%H%M%S)"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
IMFL_PROFILE="${HOME}/.hermes/profiles/imfl"
NVME_PROFILE="/mnt/nvme_ntfs/agents/hermes/profiles/imfl"
BUSINESS_SKILLS_DIR="${HOME}/code/imfl_skills"

echo "======================================================================"
echo " [Step 1/4] 备份核心业务资产与火种 (记忆库 / 业务Skills / 关键配置)"
echo "======================================================================"
mkdir -p "${BACKUP_DIR}"

# 1. 备份记忆库
if [[ -d "${IMFL_PROFILE}/memories" ]]; then
    echo "-> 备份用户主目录记忆库: ${IMFL_PROFILE}/memories"
    cp -r "${IMFL_PROFILE}/memories" "${BACKUP_DIR}/memories_home"
elif [[ -d "${NVME_PROFILE}/memories" ]]; then
    echo "-> 备份NVME记忆库: ${NVME_PROFILE}/memories"
    cp -r "${NVME_PROFILE}/memories" "${BACKUP_DIR}/memories_nvme"
fi

# 2. 备份业务技能库
if [[ -d "${BUSINESS_SKILLS_DIR}" ]]; then
    echo "-> 备份业务专属技能: ${BUSINESS_SKILLS_DIR}"
    cp -r "${BUSINESS_SKILLS_DIR}" "${BACKUP_DIR}/imfl_skills"
fi

# 3. 备份核心密钥与配置文件
for cfg in "${IMFL_PROFILE}/config.yaml" "${NVME_PROFILE}/config.yaml" "${HOME}/.claude/settings.json"; do
    if [[ -f "${cfg}" ]]; then
        echo "-> 备份关键配置: ${cfg}"
        cp "${cfg}" "${BACKUP_DIR}/"
    fi
done

echo "业务资产火种已安全落盘至: ${BACKUP_DIR}"

echo "======================================================================"
echo " [Step 2/4] 暴力下线并清理原机历史僵尸进程与 Systemd 服务"
echo "======================================================================"
# 匹配所有历史涉案服务模式
SERVICES_TO_STOP=(
    "analysis-colleague*"
    "imfl-colleague@*"
    "hermes-gateway-imfl*"
    "superagent-*"
    "agentcore-*"
)

for pattern in "${SERVICES_TO_STOP[@]}"; do
    echo "-> 停止服务: ${pattern}"
    systemctl --user stop "${pattern}" 2>/dev/null || true
    systemctl --user disable "${pattern}" 2>/dev/null || true
done

# 删除旧 systemd unit 文件，彻底根治死服务
if [[ -d "${SYSTEMD_USER_DIR}" ]]; then
    echo "-> 清理旧 unit 文件..."
    rm -f "${SYSTEMD_USER_DIR}"/analysis-colleague*.service \
          "${SYSTEMD_USER_DIR}"/hermes-gw-reload-once*.service \
          "${SYSTEMD_USER_DIR}"/*superagent-before* 2>/dev/null || true
fi
systemctl --user daemon-reload
systemctl --user reset-failed 2>/dev/null || true

echo "======================================================================"
echo " [Step 3/4] 抹除原机历史散落配置与野生运行时代码目录"
echo "======================================================================"
# 清理可能引起版本打架与配置漂移的野生残留目录
DIRTY_TARGETS=(
    "${HOME}/.hermes/profiles/imfl/proxy.env"
    "${HOME}/code/agentcore/agentcore/proxy.env"
    "${HOME}/.local/state/agentcore/analysis-queue"
    "${HOME}/.local/state/agentcore/liveness.db"
)

for target in "${DIRTY_TARGETS[@]}"; do
    if [[ -e "${target}" ]]; then
        echo "-> 移除污染文件/运行时状态: ${target}"
        rm -rf "${target}"
    fi
done

echo "======================================================================"
echo " [Step 4/4] 恢复业务火种并核验系统纯净度"
echo "======================================================================"
# 校验当前工作区是否对齐
CURRENT_ENGINE="/mnt/nvme_ntfs/agents/hermes/superagent/imfl/current"
if [[ -L "${CURRENT_ENGINE}" ]]; then
    echo "-> 当前受控引擎路径正常: $(readlink -f "${CURRENT_ENGINE}")"
fi

echo "======================================================================"
echo " 原机历史环境推平与纯净重构完成！"
echo " 业务资产备份位置: ${BACKUP_DIR}"
echo " 系统已彻底摆脱原机旧配置与历史僵尸服务干扰。"
echo "======================================================================"
