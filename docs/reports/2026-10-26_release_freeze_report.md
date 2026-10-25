# 可迁移发布候选 · 冻结报告（2026-10-26）

**候选提交**：冻结 tag `v0.1.0-freeze-20261026` 指向的提交（技术仓 `super-agent`，master）
—— **代码面最后一次完整验证发生在 `be34e56`**：干净室两次退出码 0，且新增的单一自检入口
`bash verify.sh --with-docker` 在它上面也退出码 0；tag 提交相对 `be34e56` 只多文档改动。

**结论**：干净室部署验证 **退出码 0（全部通过）** —— 干净室在 `6333c92` 与 `57cc8c7` 两个提交上各复跑过一次（后者含派工契约，容器内 `派工契约 8/8 全部落位`）；本地全量测试 **48 passed / 0 failed**。
本报告替代此前两份已过期报告（`2026-10-25_clean_room_deploy_report.md`、`2026-10-26_identity_and_feature_verification_report.md`）
——它们的验证对象停在 `74b13e0` / `2f23720`，之后 8~9 个提交引入的两处红没有跟着复验，**这正是本次要修的问题本身**。

## 一、冻结前修掉的两处红（原样记录，不做修饰）

`bash docker/verify.sh` 在 `c65c0f9` 上实跑：**退出码 2，2 项失败**。

| # | 现象（原文） | 根因 | 修法 |
| :-- | :--- | :--- | :--- |
| ① | `FAILED tests/test_arch_doc.py::test_mermaid_fence_is_swapped_wholesale` … `2 failed, 44 passed, 2 skipped` | 镜像只装 `pytest/setuptools/wheel`，缺 `python-markdown`；`tools/arch_doc.py:213` 在**调用期** `sys.exit`，于是渲染链那组测试硬崩 | 两边同时修：干净室装上 `markdown`（否则这条能力在容器里**从未被真验证过**）；测试侧按 `find_spec` 判定，缺依赖**整组 skip** |
| ② | `[FAIL] 意外装成生产技能 1 个（examples/ 应该只当范本）` | 断言写死「`skills` 下 SKILL.md 必须为 0」——那是收录**第一个真通用技能**之前的旧口径；安装器把 `skills/<name>/` 装进生产是**正确行为** | 断言改为只盯 `examples/` 不得被装（`n_ex == 0`），真技能装了几个如实打印 |

> 纪律：**报告必须跟着 commit 走**。这次的教训不是「有个测试挂了」，而是「报告停在旧提交上，仓库自述与事实矛盾」。

## 二、本候选的验证证据（逐项可复现）

| 项 | 命令 | 结果 |
| :--- | :--- | :--- |
| 本地全量 | `/usr/bin/python3 -m pytest tests -q` | **48 passed** |
| 缺依赖行为 | 屏蔽 `import`+`find_spec` 模拟无 markdown | **1 skipped / 0 failed**（不是崩） |
| 脚本语法 | `bash -n docker/clean_room_check.sh docker/verify.sh` | OK |
| 干净室 | `bash docker/verify.sh 6333c92` | **退出码 0（干净室可部署）** |
| 干净室 · 引擎端到端 | 第 ⑦ 段 | `pending=0 hits=1 empty_reason=None` / `SMOKE_OK` |
| 干净室 · 合成身份 | 第 ⑥ 段 | 占位符全替换、注入点恰好 1 处、骨架原样保留、体积等式成立 |
| 零业务字面量棘轮 | `tests/test_repo_neutrality.py` | 通过（本轮真拦下过一次：把验证日志写进了仓里） |

| **单一自检入口** | `bash verify.sh --with-docker` | **退出码 0**（静态检查 + 单测 + 安装 dry-run + 干净室，四段全绿；报告见下条复现说明） |

## 三、本轮其余定点验证（与发布同一提交体系内）

| 事项 | 判据 | 结果 |
| :--- | :--- | :--- |
| 文档渲染链判据 | 4 夹具（真残留必 FAIL / 自引用不误报 / 图数不符必 FAIL / 正常 PASS） | 4/4 |
| 冗余预览条清理 | 直调被改方法 + 假适配器观测删除集 | 6/6（含「已抑制时绝不误删终稿」） |
| 记忆召回 | 真 MemoryManager 驱动 provider + CLI 真召回 | 召回块 1039 字符、命中门限事实；`prefetch_all` 是 `agent/turn_context.py:929` 的注入来源 |
| 记忆守护作业 | 首跑回执 | 93.9% → 69.4%，条目 36 → 24，应用 2 批 / 拒 4 批（拒因＝锚点失效） |

## 四、已知缺口（照实记录，不藏）

1. **无单一自检入口**：clone 后要人工跑 `install/install.sh --dry-run`、`docker/verify.sh`、`checks/feature_check.sh`；仓根无 `Makefile`/根 `verify.sh`，GitHub CI 未接。
2. **未覆盖**：网关启停与消息收发、模型路由、真实迁移彩排、记忆数据搬运 —— 干净室与 checks 都不在射程内。
3. **`checks/` 两个脚本本次只做静态检查**（`bash -n` + 读），未复跑：`feature_check.sh` 的 F7/F8 依赖真实 Hermes 树、Node 与可解析凭据；`identity_injection_check.sh` 需 `hermes` CLI。
4. **发布出口未打通**：推 GitHub 需要凭据（本机 token 待轮转），tag 已打但推送状态以命令输出为准。

## 五、复现

```bash
git clone <repo> && cd super-agent
bash docker/verify.sh <候选提交>      # 干净室：退出码 0 = 可部署
/usr/bin/python3 -m pytest tests -q   # 本地：48 passed
bash checks/feature_check.sh          # 宿主特性矩阵（缺依赖的组降级为 WARN）
```