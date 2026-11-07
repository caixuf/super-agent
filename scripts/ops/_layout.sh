#!/usr/bin/env bash
# ==============================================================================
# _layout.sh —— 官方运维脚本共用的「布局 / 真相源」解析
#
# 由 uninstall-legacy.sh 与 purge_and_rebuild_host.sh source；本文件不单独执行。
#
# 真相源判定顺序与 agentcore/profile_spec.py:get_engine_root() 同源：
#   1. AGENTCORE_ENGINE_ROOT（显式注入）
#   2. ${AGENTCORE_ROOT}/<profile>/current   （受管布局）
#   3. ${HERMES_HOME}/agentcore               （官方 install/install.sh 单写者布局）
#   4. 都没有 → 拒绝继续
#
# 调用方在 source 之前需要先给出：AGENTCORE_ROOT / PROFILE_NAME / HERMES_HOME。
# ==============================================================================

# 解析结果（source 后由 resolve_layout 填充）
LAYOUT=""
TRUTH_SOURCE=""
PROFILE_HOME=""

# --- 内部小工具 --------------------------------------------------------------

_layout_die() {
    printf '%s\n' "$1" >&2
    shift
    local line
    for line in "$@"; do printf '  %s\n' "${line}" >&2; done
    return 1
}

# profile home：单写者布局下 profile 就是 home 本身；受管布局下是 home 的子目录
_layout_detect_profile_home() {
    if [[ -d "${HERMES_HOME}/profiles/${PROFILE_NAME}" ]]; then
        printf '%s\n' "${HERMES_HOME}/profiles/${PROFILE_NAME}"
    else
        printf '%s\n' "${HERMES_HOME}"
    fi
}

# --- 真相源解析 --------------------------------------------------------------

# resolve_layout → 成功时设置 LAYOUT / TRUTH_SOURCE / PROFILE_HOME；
#                真相源不存在时打印原因并返回 1（调用方必须自行处理，不要让它撞 set -e）。
resolve_layout() {
    local managed_root="${AGENTCORE_ROOT}/${PROFILE_NAME}"
    local managed_current="${managed_root}/current"

    if [[ -n "${AGENTCORE_ENGINE_ROOT:-}" ]]; then
        if [[ ! -d "${AGENTCORE_ENGINE_ROOT}" ]]; then
            _layout_die "显式注入的真相源不存在，拒绝继续" \
                "AGENTCORE_ENGINE_ROOT=${AGENTCORE_ENGINE_ROOT}"
            return 1
        fi
        LAYOUT="explicit"
        TRUTH_SOURCE="${AGENTCORE_ENGINE_ROOT}"
        PROFILE_HOME="$(_layout_detect_profile_home)"
        return 0
    fi

    if [[ -d "${managed_current}" ]]; then
        LAYOUT="managed"
        TRUTH_SOURCE="${managed_current}"
        PROFILE_HOME="$(_layout_detect_profile_home)"
        return 0
    fi

    if [[ -d "${HERMES_HOME}/agentcore" ]]; then
        LAYOUT="install.sh"
        TRUTH_SOURCE="${HERMES_HOME}/agentcore"
        PROFILE_HOME="${HERMES_HOME}"
        return 0
    fi

    _layout_die "真相源不存在，拒绝继续" \
        "受管候选: ${managed_current}" \
        "单写者候选: ${HERMES_HOME}/agentcore" \
        "（可用 AGENTCORE_ENGINE_ROOT 显式注入）"
    return 1
}

# 打印本次生效的布局与真相源（让脚本的输出自证它在对账哪棵树）
layout_report() {
    printf '布局=%s\n' "${LAYOUT}"
    printf '真相源=%s\n' "${TRUTH_SOURCE}"
    printf 'profile home=%s\n' "${PROFILE_HOME}"
}

# --- 在役 Hermes 安装树 ------------------------------------------------------

# 官方 install/install.sh 的 HERMES_AGENT_DIR 默认值是 ${HERMES_HOME}/hermes-agent，
# 而受管单根布局把树放在 ${AGENTCORE_ROOT}/<profile>/hermes-current 下。
# 解析顺序：显式注入 > 受管 hermes-current（取最新）> ${HERMES_HOME}/hermes-agent。
resolve_hermes_tree() {
    if [[ -n "${AGENTCORE_HERMES_TREE:-}" ]]; then
        printf '%s\n' "${AGENTCORE_HERMES_TREE}"
        return 0
    fi
    local candidate
    for candidate in "${AGENTCORE_ROOT}"/*/hermes-current; do
        [[ -d "${candidate}" ]] || continue
        printf '%s\n' "${candidate}"
        return 0
    done
    if [[ -d "${HERMES_HOME}/hermes-agent" ]]; then
        printf '%s\n' "${HERMES_HOME}/hermes-agent"
        return 0
    fi
    return 1
}

# --- 布局相关的辅助 ----------------------------------------------------------

# 第二份 config.yaml 的路径（受管布局才有「root 配置 vs profile 配置」这组二分）。
# 单写者布局下 profile 就是 root，不存在第二份 —— 返回空串。
layout_second_config() {
    if [[ "${LAYOUT}" == "managed" ]]; then
        printf '%s\n' "${HERMES_HOME}/config.yaml"
    fi
}

# proxy.env 扫描根（每行一个；调用方按「是否 profiles 根」决定 find 的 maxdepth）
layout_proxy_env_roots() {
    printf '%s\n' "${HERMES_HOME}/profiles"
    if [[ "${LAYOUT}" != "managed" ]]; then
        printf '%s\n' "${HERMES_HOME}"
    fi
}

# 布局对应的验证命令：受管布局用受管 doctor，单写者布局用官方就绪门禁。
# 输出三行：<说明> / <命令...>，由调用方 eval 执行。
layout_verification_command() {
    if [[ "${LAYOUT}" == "managed" ]]; then
        printf '受管 doctor\n'
        printf 'python3 "%s" --root "%s" --profile "%s" doctor\n' \
            "${INSTALLER}" "${AGENTCORE_ROOT}" "${PROFILE_NAME}"
    else
        printf '官方就绪门禁\n'
        printf '"%s" "%s" --profile-home "%s" --profile "%s"\n' \
            "${HERMES_HOME}/agentcore/.superagent-venv/bin/python" \
            "${HERMES_HOME}/agentcore/tools/deployment_readiness.py" \
            "${HERMES_HOME}" "${PROFILE_NAME}"
    fi
}

# 备份源清单（每行一个路径）。单写者布局下 profile 就是 home 本身，
# 记忆与技能都在 ${HERMES_HOME} 下，不在 profiles/<name> 下。
layout_backup_sources() {
    if [[ "${LAYOUT}" == "managed" ]]; then
        printf '%s\n' "${HERMES_HOME}/profiles/${PROFILE_NAME}/memories"
        [[ -n "${SKILLS_DIR:-}" ]] && printf '%s\n' "${SKILLS_DIR}"
        printf '%s\n' "${HERMES_HOME}/profiles/${PROFILE_NAME}/config.yaml" \
            "${HERMES_HOME}/config.yaml"
    else
        printf '%s\n' "${HERMES_HOME}/memories" "${HERMES_HOME}/skills" \
            "${HERMES_HOME}/config.yaml"
    fi
}

# 归档源清单（每行一个路径）：散落覆盖配置 + 运行时状态。
# 现场配置若指向某个候选路径，就不许归档它 —— 归档的是死状态，不是活状态。
_live_config_refs() {
    local target="$1"
    [[ -f "${HERMES_HOME}/config.yaml" ]] || return 1
    grep -qF -- "${target}" "${HERMES_HOME}/config.yaml"
}

layout_archive_sources() {
    if [[ "${LAYOUT}" == "managed" ]]; then
        printf '%s\n' "${HERMES_HOME}/profiles/${PROFILE_NAME}/proxy.env"
    else
        printf '%s\n' "${HERMES_HOME}/proxy.env"
    fi
    local candidate
    for candidate in "${HOME}/.local/state/agentcore/analysis-queue" \
                     "${HOME}/.local/state/agentcore/liveness.db"; do
        if _live_config_refs "${candidate}"; then
            continue
        fi
        printf '%s\n' "${candidate}"
    done
}
