# Super Agent × LobeChat 美化 Web 界面与 Bot 集成指南

本文档介绍如何在 **Super Agent** 层面无缝接入 **LobeChat**，告别原生 Dashboard 过于复古的像素风格与纯英文界面，获得全中文支持、现代化玻璃拟态 UI 以及一键使用 Super Agent 团队 Bot 的完整体验。

---

## 1. 架构协同原理

```
┌────────────────────────────────────────────────────────┐
│                   LobeChat Web 前端                    │
│   (现代高颜值 UI / 全中文 / 多 Bot 角色市场 / 会话历史)   │
└───────────────────────────┬────────────────────────────┘
                            │ OpenAI 兼容接口 (/v1)
                            │ SSE 流式返回工具执行过程
┌───────────────────────────▼────────────────────────────┐
│              Hermes Gateway (API Server)               │
│                  (默认监听端口: 8642)                   │
├────────────────────────────────────────────────────────┤
│                   Super Agent 认知层                   │
│   - 持久任务池 (agentcore.pool)                         │
│   - 突触长期记忆 (agentcore.memory)                     │
│   - 8 大官方研发同事 (DEV_ROLES)                         │
└────────────────────────────────────────────────────────┘
```

- **界面层**：LobeChat 负责交互呈现，拥有出色的深浅色主题、动效和多 Bot/Assistant 组织能力；
- **传输层**：Hermes 内置的 API Server（OpenAI 兼容协议），通过 Server-Sent Events (SSE) 向 LobeChat 实时流式传输 Agent 的思考、工具调用与结果；
- **智能体层**：Super Agent 作为底层大脑，承接 LobeChat 发起的任务，调用真实终端、文件读写、网络搜索、持久池与研发同事协同。

---

## 2. 快速部署步骤

### 第一步：在 Hermes 中开启 API Server

在 Hermes 环境配置（例如 `~/.hermes/.env` 或对应 profile 的 `.env`）中确保开启 API Server：

```bash
API_SERVER_ENABLED=true
API_SERVER_KEY=hermes-secret-key-123456
```

启动 Hermes Gateway：
```bash
hermes gateway run
# 或在后台常驻运行：
hermes gateway start
```
*API Server 默认监听在 `http://127.0.0.1:8642/v1`。*

---

### 第二步：一键配置并启动 LobeChat

Super Agent 提供了内置的 `lobechat` 安装部署命令：

```bash
# 查看部署计划与配置预览 (Dry-run)
./install/superagent.sh lobechat --port 3210

# 写入部署文件并准备启动
./install/superagent.sh lobechat --port 3210 --apply
```

执行后，将在 `~/.hermes/lobechat/` 目录下生成：
1. `docker-compose.yml`：已自动配置 `host.docker.internal:host-gateway`，容器内直连宿主机 Hermes API Server；
2. `start.sh` / `stop.sh`：快速启动与停止脚本。

启动 LobeChat：
```bash
cd ~/.hermes/lobechat
./start.sh
```

打开浏览器访问 `http://localhost:3210` 即可进入 LobeChat 全中文现代化界面。

---

## 3. 一键导入 Super Agent 团队 Bot 预设

为满足“**最好可以使用 bot**”的诉求，Super Agent 提供了官方 Bot 预设导出器：

```bash
# 一键导出 Super Agent 主中枢与 8 大官方研发同事 Bot 预设包
python3 tools/export_lobechat_agents.py -o ~/.hermes/lobechat/superagent_bots.json
```

导出的 9 个专属 Bot 包括：
- 🧠 **Super Agent 主智能体**：全局认知与复杂长任务总中枢
- 📐 **需求边界与系统架构师** (`architect`)：纯黑盒需求因果拆解与蓝图设计
- 🧪 **全场景用例开发大师** (`case_master`)：机制级测试规格与用例设计
- 🛡️ **红队挑刺与契约评审员** (`red_team`)：反模式审查、边界挖掘与缺陷阻断
- 🎮 **高并发仿真测试专家** (`simulation`)：仿真回灌与时序对账验证
- 🔍 **故障树与根因归因专家** (`attribution`)：因果分析与定性排查
- 💻 **核心领域服务后端** (`backend`)：服务接口与高内聚领域逻辑实现
- 🎨 **终端卡片与交互工程师** (`interaction`)：动态状态卡片与交互呈现
- 🛠️ **纯运维与巡检哨兵** (`ops`)：环境巡检、服务编排与自动化守护

### 在 LobeChat 中导入步骤：
1. 打开 LobeChat（`http://localhost:3210`）；
2. 点击左下角头像/设置图标 -> **数据管理 (Data Management)** -> **导入数据 (Import)**；
3. 选择刚才生成的 `~/.hermes/lobechat/superagent_bots.json`；
4. 导入完成后，左侧会话列表中即可直接看到所有 Super Agent 研发同事 Bot，点击即可直接分工对话！

---

## 4. 常见问题与网络排障 (FAQ)

### Q1: LobeChat 容器连接不上宿主机的 Hermes API Server 怎么办？
- Linux / WSL2 环境下，`docker-compose.yml` 中已默认配置 `extra_hosts: ["host.docker.internal:host-gateway"]`；
- 如果使用的是自定义 Docker 网络，请确保容器内解析 `host.docker.internal` 可以连通宿主机 IP 的 8642 端口；
- 检查 `curl http://127.0.0.1:8642/health` 或 `http://127.0.0.1:8642/v1/models` 是否返回正常响应。

### Q2: 如何增加 Web 端访问密码保护？
在运行 setup 命令时指定密码即可：
```bash
./install/superagent.sh lobechat --access-code "your-password" --apply
```

