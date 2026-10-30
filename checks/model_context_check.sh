#!/usr/bin/env bash
# checks/model_context_check.sh —— 证明「模型上下文窗口来自端点，而不是手写 pin」
#
# 背景：/v1/models 是标准目录，但 LiteLLM 之类的网关只在 /v1/model/info 里
# 暴露 max_input_tokens。只探 /v1/models 的客户端会回退到 256K，长会话被过早压缩。
# 本脚本读取 active profile 的 config.yaml，探测端点并报告：
#   * 生效的上下文窗口与来源（models / model/info）
#   * 手写 model.context_length 是否与目录不一致
#   * compression.context_length / threshold_tokens 是否与窗口一致
#
# 退出码：0 = 拿到窗口；1 = 端点可达但没有窗口（契约不满足）；2 = 端点不可达（环境问题）。
#
# 用法：
#   bash checks/model_context_check.sh
#   MODEL_CONTEXT_CONFIG=/path/to/config.yaml bash checks/model_context_check.sh
#   bash checks/model_context_check.sh --base-url http://127.0.0.1:4000/v1 --model main
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 2

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for cand in python3 /usr/bin/python3; do
    if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
  done
fi
if [ -z "$PY" ]; then
  echo "[SKIP] 找不到 python3" >&2
  exit 2
fi

CONFIG="${MODEL_CONTEXT_CONFIG:-}"
if [ -z "$CONFIG" ]; then
  PROFILE="${HERMES_PROFILE:-$(cat "$HOME/.hermes/active_profile" 2>/dev/null | tr -d '\n' || true)}"
  [ -n "$PROFILE" ] || PROFILE="default"
  CONFIG="$HOME/.hermes/profiles/$PROFILE/config.yaml"
fi
if [ ! -f "$CONFIG" ]; then
  echo "[SKIP] 没有找到 profile 配置：$CONFIG" >&2
  exit 2
fi

exec "$PY" -m agentcore.model_context --config "$CONFIG" "$@"
