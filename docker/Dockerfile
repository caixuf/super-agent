# 干净室部署验证镜像 —— 只装「智能体本体」，不含任何业务内容。
#
# 用途：证明「一个空机器 + 本仓源码」就能立起一套可用的通用智能体资产，
#       而不是只能在本机（有历史配置、有 home 残留）跑通。
#
# 手动用法（在仓根）：
#   docker build -f docker/Dockerfile -t super-agent-check .
#   docker run --rm super-agent-check
#
# 想验证「远端已发布的那个提交能不能立起来」，用 docker/verify.sh（它先把该提交
# 导出成一个干净检出，再建镜像 —— 排除工作区残留带来的假绿）。

FROM python:3.12-slim

# pip 走镜像站，避免依赖外网直连；不写字节码，保持容器内文件乾淨
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/root

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/super-agent
COPY . /opt/super-agent

# 本仓运行时零第三方依赖；测试与打包只需要这些。
# markdown 必须装：它是文档渲染链（tools/arch_doc.py）的唯一可选依赖，缺它时那组测试只能整体
# skip —— 干净室就变成「没验证过渲染链却报全绿」，那是最坏的一种假绿。
RUN python3 -m pip install "pytest>=7.0" setuptools wheel "markdown>=3.4" "pyyaml>=6.0"

# 默认动作：干净室自检（仓自检 → 包安装 → 安装器 → 幂等 → 合成身份 → 引擎端到端）
CMD ["bash", "docker/clean_room_check.sh"]