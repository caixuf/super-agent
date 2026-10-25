# 干净室部署验证

`docker/` 只干一件事：证明**本仓能从一个空机器立起来**，而不是只在这台开发机上能跑。

## 跑法

```bash
bash docker/verify.sh              # 默认 HEAD：先把该 ref 导出成干净检出，再建镜像跑自检
bash docker/verify.sh <commit|tag> # 发布前验证某个提交

# 或者手动（构建上下文 = 当前工作区）
docker build -f docker/Dockerfile -t super-agent-check .
docker run --rm super-agent-check
```

`verify.sh` 与手动 `docker build .` 的区别：前者**先把 git ref 导出成一个干净检出**
（不含未提交改动、不含被 gitignore 的产物），所以绿的是**那个提交**；
后者绿的是**你的工作区**。发布前跑前者。

## 文件

| 文件 | 作用 |
| :--- | :--- |
| `Dockerfile` | `python:3.12-slim` + pytest；只装本仓，不装任何业务内容 |
| `clean_room_check.sh` | 容器内 7 步 20 项断言（表见仓根 `README.md`） |
| `engine_smoke.py` | 引擎端到端最小闭环；同时是「宿主该怎么注入业务约定」的可运行范例 |
| `verify.sh` | 主机侧：干净检出 → 建镜像 → 跑自检 → 透传退出码 |

## 不覆盖什么（别把绿当全能）

- **不启停服务、不装 Hermes CLI、不碰凭据** —— 容器里没有网关、没有模型 key。
- 所以「网关能不能起、消息能不能收发、模型路由通不通」**不在**本目录的断言范围，
  那属于部署后的验收（`install/install.sh` ⑩ 段会打印要人工执行的步骤）。
- 外部编码执行器（Pi 之类）需要 Node 子进程，容器里也不装 —— 只验证资产落位与可导入性。
- 业务叠加层不在验证范围：容器里没有业务技能源/业务工具面，那是业务仓的事。

## 为什么值得留着

它抓过真缺陷：`install/install.sh` ⑩ 段的 heredoc 用了没兜底的 `$HERMES_HOME`，在 `set -u` 下
**空机器上会以 unbound variable 非 0 退出**（活全干完了却报失败），而开发机因为恰好导出了
那个变量一直没暴露。每次改安装器或资产清单，跑一遍这个，比去新机器上手工试快得多。

日志留在宿主的 scratch 目录即可，容器用完即弃（`docker run --rm`）；镜像可以留着复用
（`super-agent-check:<sha>`），分支切换后按 sha 区分，不会互相踩。