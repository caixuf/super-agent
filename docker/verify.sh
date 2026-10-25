#!/usr/bin/env bash
# docker/verify.sh —— 干净室验证「已发布的提交」能不能立起来
#
# 与直接 `docker build` 的区别：先把指定 git ref **导出成一个干净检出**（不含未提交改动、
# 不含被 gitignore 的生产物），再用它当构建上下文。这样绿的是**那个提交**，
# 而不是「我这台机器的工作区」——发布前要的就是前者。
#
# 用法：
#   bash docker/verify.sh                # 默认 HEAD
#   bash docker/verify.sh <commit|tag>   # 指定提交
#
# 退出码：0 = 全部通过；非 0 = 失败。

set -u
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REF="${1:-HEAD}"
cd "$REPO_ROOT"

SHA="$(git rev-parse --short "$REF")"
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  printf '[warn] 工作区有未提交改动；本次验证只针对提交 %s 里已入库的内容\n' "$SHA"
fi

CTX="$(mktemp -d "${TMPDIR:-/tmp}/super-agent-ctx.XXXXXX")"
trap 'rm -rf "$CTX"' EXIT

git archive --format=tar "$REF" | tar -x -C "$CTX"
printf '== 干净检出 %s → %s（%s 个文件）\n' "$REF" "$CTX" "$(find "$CTX" -type f | wc -l)"

# 业务字面量门禁由**仓内那个测试**负责：容器里的 `pytest tests` 会跑
# tests/test_repo_neutrality.py（词表的单一事实源就是那个文件）。
# 这里刻意**不复制**词表 —— 复制一份，本文件自己就带上了业务词，反而把门禁弄红（实测踩过）。

IMG="super-agent-check:$SHA"
printf '\n== 构建镜像 %s\n' "$IMG"
docker build -f "$CTX/docker/Dockerfile" -t "$IMG" "$CTX" || { printf '❌ 镜像构建失败\n'; exit 1; }

printf '\n== 运行干净室自检\n'
rc=0
docker run --rm "$IMG" || rc=$?

printf '\n== 退出码 %d %s\n' "$rc" "$([ "$rc" -eq 0 ] && echo '（干净室部署通过）' || echo '（有失败项）')"
exit "$rc"