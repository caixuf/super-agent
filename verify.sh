#!/usr/bin/env bash
# 单一自检入口 —— clone 之后先跑这个，再看别的。
#
# 为什么要有它：此前「clone 后怎么开始」要靠人拼三条命令（install --dry-run / docker verify /
# feature_check），漏跑一条就得出「装得上」的假结论。合成一条，退出码 = 失败项数之和。
#
#   bash verify.sh              # 快检：静态 + 单测 + 安装 dry-run（约 30 秒，无需 docker）
#   bash verify.sh --with-docker  # 加跑干净室部署验证（建镜像，几分钟）
#   bash verify.sh --with-checks  # 加跑宿主特性矩阵（需 Hermes 树 / Node，缺依赖的组降级 WARN）
#
#   模型上下文体检每次都会跑：有 profile 配置就探测端点，没有配置或端点不可达则 SKIP。
#
# 退出码约定：0 = 全过；非 0 = 失败项数（三块相加）。缺依赖导致的 SKIP 不算失败，
# 但会在末尾明说 —— 「环境缺失」不许伪装成「通过」。
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT" || exit 1

WITH_DOCKER=0
WITH_CHECKS=0
for arg in "$@"; do
  case "$arg" in
    --with-docker) WITH_DOCKER=1 ;;
    --with-checks) WITH_CHECKS=1 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg（--help 看用法）" >&2; exit 1 ;;
  esac
done

PY=""
rc_total=0
skipped=()

# 解释器要挑「身上真的带 pytest 的那个」，而不是盲信 PATH 上的 python3：
# 开发机上 PATH 常常先命中某个没有 pytest 的沙箱 venv，于是单元测试块假红，
# 人会去怀疑代码而不是环境。挑不到就显式失败并告诉人怎么指（PYTHON=…）。
pick_python() {
  local cand
  for cand in "${PYTHON:-}" python3 /usr/bin/python3; do
    [ -n "$cand" ] || continue
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import pytest' >/dev/null 2>&1; then
      printf '%s' "$cand"; return 0
    fi
  done
  return 1
}
if ! PY="$(pick_python)"; then
  echo "找不到带 pytest 的解释器（用 PYTHON=/path/to/python 指定）" >&2
  exit 1
fi

section() { printf '\n== %s\n' "$1"; }
fail() { rc_total=$((rc_total + 1)); printf '  [FAIL] %s\n' "$1"; }
pass() { printf '  [PASS] %s\n' "$1"; }

# ── ① 静态：所有 shell 脚本必须语法正确（零依赖，先跑）
section "① 静态检查（shell 语法 + JSON/YAML 可解析）"
n_shell=0
while IFS= read -r f; do
  n_shell=$((n_shell + 1))
  bash -n "$f" || fail "语法错误：$f"
done < <(find . -name '*.sh' -not -path './.git/*' | sort)
[ "$n_shell" -gt 0 ] && pass "$n_shell 个 shell 脚本语法正确"

# ── ② 单元测试（含业务无关门禁；这是"仓自检"的本地版）
section "② 单元测试（$PY -m pytest tests -q）"
if "$PY" -m pytest tests -q >/tmp/verify_unit.log 2>&1; then
  pass "$(tail -n 1 /tmp/verify_unit.log | tr -d '\n')"
else
  tail -n 20 /tmp/verify_unit.log | sed 's/^/    /'
  fail "单元测试未全过（完整输出：/tmp/verify_unit.log）"
fi

# ── ②.1 防退化靶场：Golden Evals 真实踩坑用例硬门禁
section "②.1 防退化靶场（$PY -m pytest tests/evals -q）"
if "$PY" -m pytest tests/evals -q >/tmp/verify_golden.log 2>&1; then
  pass "$(tail -n 1 /tmp/verify_golden.log | tr -d '\n')"
else
  tail -n 20 /tmp/verify_golden.log | sed 's/^/    /'
  fail "防退化靶场用例失败（完整输出：/tmp/verify_golden.log）"
fi

# ── ③ 安装器 dry-run：一个文件都不许建
section "③ 安装器 dry-run（零副作用）"
if [ -f install/install.sh ]; then
  if bash install/install.sh --dry-run >/tmp/verify_dryrun.log 2>&1; then
    pass "退出码 0"
  else
    tail -n 15 /tmp/verify_dryrun.log | sed 's/^/    /'
    fail "dry-run 非 0 退出"
  fi
else
  fail "缺 install/install.sh"
fi

# ── ④ 可选：干净室部署验证（Docker）
if [ "$WITH_DOCKER" = "1" ]; then
  section "④ 干净室部署验证（docker/verify.sh）"
  if command -v docker >/dev/null 2>&1; then
    bash docker/verify.sh && pass "干净室可部署（退出码 0）" || fail "干净室验证失败"
  else
    skipped+=("干净室（无 docker）")
  fi
fi

# ── ⑤ 可选：宿主特性矩阵（需 Hermes 树 / Node / 凭据）
if [ "$WITH_CHECKS" = "1" ]; then
  section "⑤ 宿主特性矩阵（checks/feature_check.sh）"
  bash checks/feature_check.sh && pass "特性矩阵通过（含降级 WARN）" || fail "特性矩阵有失败项"
fi

# ── ⑥ 真实通道交付：消息真的发到消息平台并在平台回读（需 app 凭据 + bot 专属测试群）
# 未配置凭据时记一笔跳过，不让「环境缺失」看起来像「通过」。
section "⑥ 真实通道交付（消息平台 open platform）"
if [ -n "${FEISHU_APP_ID:-}" ] && [ -n "${FEISHU_APP_SECRET:-}" ] && [ -n "${FEISHU_TEST_CHAT_ID:-}" ]; then
  if "$PY" -m pytest tests/test_feishu_transport_real.py tests/test_feishu_delivery_real.py -q \
      >/tmp/verify_feishu.log 2>&1; then
    pass "$(tail -n 1 /tmp/verify_feishu.log | tr -d '\n')"
  else
    tail -n 20 /tmp/verify_feishu.log | sed 's/^/    /'
    fail "真实消息平台交付未过（完整输出：/tmp/verify_feishu.log）"
  fi
else
  skipped+=("真实消息平台交付（未设置 FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_TEST_CHAT_ID）")
fi

# ── ⑦ 模型上下文体检：端点必须说清窗口，不能靠手写 pin
# 退出码 0 = 拿到窗口；1 = 端点可达但契约不满足；2 = 无配置/不可达，记为 SKIP。
section "⑦ 模型上下文体检（端点契约）"
if [ -f checks/model_context_check.sh ]; then
  bash checks/model_context_check.sh >/tmp/verify_model_context.log 2>&1
  rc=$?
  if [ "$rc" -eq 0 ]; then
    pass "$(awk '/^context:/{print; exit}' /tmp/verify_model_context.log | sed 's/^context:  //')"
  elif [ "$rc" -eq 2 ]; then
    skipped+=("模型上下文体检（无 profile 配置或端点不可达）")
  else
    tail -n 20 /tmp/verify_model_context.log | sed 's/^/    /'
    fail "模型上下文契约未满足（完整输出：/tmp/verify_model_context.log）"
  fi
else
  fail "缺 checks/model_context_check.sh"
fi

section "结果"
if [ "${#skipped[@]}" -gt 0 ]; then
  printf '  跳过（环境缺失，不算通过）：%s\n' "$(IFS='、'; echo "${skipped[*]}")"
fi
if [ "$rc_total" -eq 0 ]; then
  echo "  全部通过 [PASS]"
else
  echo "  失败 $rc_total 项 [FAIL]"
fi
exit "$rc_total"