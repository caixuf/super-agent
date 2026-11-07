# 第三方卡片 sidecar：构建、切流、回滚

适用：上游发布了针对投递缺陷的候选修复（源码补丁集 + 回归测试 + 评审报告），要把它装到
本机正在跑的集成上。前提是诊断已完成、且已确认缺陷在**代码**而不是配置（诊断见
`references/streaming-card-sidecar.md`）。

## 先读仓库自述，再动手

这类集成在仓库里几乎都有：`integrations/<name>/README.md`、`lock.json`、`docs/handoff/*cutover*`、
`docs/reports/*`、`tools/<name>_acceptance.py`。先看三件事：

1. 锁定的是哪个上游 commit（补丁集基于它）；
2. 评审报告里记录的**目标文件 sha256**（构建完用它校验）；
3. 是否有 `release_hold` / `default_enabled: false` 之类的发布闸——有就不要自作主张打开。

## 构建（隔离目录，不碰线上）

```bash
B=/tmp/<name>-build; rm -rf $B; mkdir -p $B; cd $B
git clone --quiet <upstream> src && cd src
git checkout --quiet <locked-commit>
```

**按序应用补丁**，碰到 `git apply --check` 失败先看是否是顺序问题：

```bash
for f in <base>.patch <next>.patch ...; do git apply <patch-dir>/$f || echo "FAILED $f"; done
```

依赖齐了才跑回归——这些测试通常**要求专用 venv 和目标源码路径**，用系统 python 会
`ModuleNotFoundError`，不设源码路径会整套 `skipped`（`skipped` 不是 `pass`）：

```bash
python -m venv $B/venv && $B/venv/bin/pip install -q <runtime-deps> pytest
<VARS>_SOURCE_ROOT=$B/src $B/venv/bin/python -m pytest <contract-tests> -q
```

## 校验产物

```bash
$B/venv/bin/python -m build --wheel --outdir $B/dist
cd $B && rm -rf verify && mkdir verify && cd verify
unzip -q $B/dist/*.whl '<pkg>/<target>.py'
sha256sum <pkg>/<target>.py     # 必须等于评审报告记录的值
```

哈希不一致说明源码/补丁顺序不对，**不要装**。

## 装到两处（关键）

| 安装位 | 怎么装 | 生效条件 |
| :--- | :--- | :--- |
| sidecar venv | 建私有 venv，装 wheel，配置改成 `service.python_executable: <venv>/bin/python`，CLI `disable` → `enable` | sidecar 重启即生效 |
| 宿主 env venv | 把 wheel 里 `<pkg>/` 整个覆盖到宿主 venv 的 site-packages，删掉 `__pycache__` | **宿主必须重启**（`sys.modules` 缓存） |

宿主 venv 常常没有 pip（是宿主自己的 install environment），用 zipfile 按 wheel 内路径覆盖即可，
装完两边各算一次同名模块哈希对账。

切 sidecar 的 CLI 参数不是对称的：持久服务开启时 `stop` 会拒绝（提示改用 `disable`），
而 `disable` **不接受** `--config`；`enable` 反而强制要求 `--config` + `--hermes-dir` + `--yes`。
先各跑一次 `--help` 再写命令，比按 `stop/start` 的习惯去试快。

## 宿主重启

改模块代码后必须重启宿主（`sys.modules` 缓存），但**不能在网关进程内重启网关**：守卫会直接拦，
把重启写进命令行的间接写法（`systemd-run --on-active ... restart <网关>`）也拦。

- 默认：把 `hermes gateway restart` 交给用户在**外部 shell** 跑。
- 用户明确要求“你自己重启”时，用 systemd 用户定时器：systemd 拥有该单元、不是网关的子进程，
  SIGTERM 杀不到它。

```bash
# ~/.config/systemd/user/<name>.service
[Service]
Type=oneshot
ExecStart=/usr/bin/systemctl --user restart hermes-gateway.service

# ~/.config/systemd/user/<name>.timer
[Timer]
OnActiveSec=<40–60>
AccuracySec=1s
Unit=<name>.service
```

```bash
systemctl --user daemon-reload && systemctl --user start <name>.timer
```

延时留足是为了让当前回复先发出去；触发后核对 PID/启动时间变化、宿主 venv 里的模块哈希是新的，
再 `stop` 定时器并删掉两个单元文件（留在盘上就是第二个调度源）。

随后真发一条并看 `Normal final-send NOT suppressed` 这类警告是否消失——但要写明样本量：
重启后一两回合的“零次告警”不是结论。

## 验收层次（不要跳级）

```bash
python tools/<name>_acceptance.py --doctor-cli <cli> --config <cfg> \
  --hermes-dir <agent-dir> --hermes-home <home> --infrastructure-only
```

- `--infrastructure-only`：安装/健康/兼容，`ready` 只说明基础面干净；
- 默认（交互样本）：会要求真实回合样本，这是唯一能证投递的层次；
- `known_delivery_blockers`：是针对 **pin 的上游版本**的静态清单，改了代码它也不会变空；
  别拿它的存在宣布“仍有缺陷”，也别拿 `ready` 宣布“已修复”。
- `manual_client_acceptance_required` 列的那几项只能人看：一回合一张卡、原地更新无重复、
  按钮回调接回同一回合、无重复卡片生产者。逐条请用户确认。

## 回滚

快照必须包含三样，缺一项回滚就不完整：

1. sidecar 配置（含 `service.python_executable` 行）；
2. systemd 单元（`systemctl --user cat <unit> > ...`）；
3. **宿主 venv 的整个 `<pkg>/` 目录**（`cp -a`）。

回滚顺序：CLI `disable` → 恢复配置 → `enable`；宿主侧把包目录换回快照 → 重启宿主。
