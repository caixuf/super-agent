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

RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true \
    && apt-get update && apt-get install -y --no-install-recommends git chromium \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/super-agent
COPY . /opt/super-agent

# 与 CI 共用 test extra；不手写依赖副本，也不提前安装本仓来掩盖包安装检查。
# 核心仍零第三方依赖，文档/宿主策略测试依赖只在验证环境安装。
RUN python3 -c 'import subprocess, sys, tomllib; extras = tomllib.load(open("pyproject.toml", "rb"))["project"]["optional-dependencies"]; subprocess.check_call([sys.executable, "-m", "pip", "install", "setuptools", "wheel", *extras["test"], *extras["documents"]])'

# 安装器仍在空 HOME 创建自己的私有 venv，不使用 --skip-deps。仅在构建时
# 预取其真实依赖；运行阶段断网也能完整安装，并由系统 Chromium 作启动探测。
# 直接读取安装器的依赖声明，避免测试镜像维护第二份易漂移的列表。
RUN python3 -c 'import runpy, subprocess, sys; deps = runpy.run_path("install/bootstrap_dependencies.py")["REQUIREMENTS"]; subprocess.check_call([sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--dest", "/opt/superagent-wheels", *deps])'
ENV PIP_NO_INDEX=1 \
    PIP_FIND_LINKS=/opt/superagent-wheels

# 默认动作：干净室自检（仓自检 → 包安装 → 安装器 → 幂等 → 合成身份 → 引擎端到端）
CMD ["bash", "docker/clean_room_check.sh"]
