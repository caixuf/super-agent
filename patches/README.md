# patches/ — 宿主安装树本地补丁

## 为什么要有这一层

`feishu_card_replies.patch` 改的是**宿主 Hermes 安装树**里的
`plugins/platforms/feishu/adapter.py` —— 不是我们的仓。`hermes update` 会覆盖它，
所以补丁必须能**幂等重放**，并且要有一个**廉价的在位校验**：
哪天它悄悄丢了不会报错，只是「卡片的流式更新没了、回复退回纯文字」，
属于最难查的那种静默退化。

> `feishu` 是消息平台适配层的名字（第三方平台），不是业务内容；补丁本身是**平台适配层的通用件**。

## 文件

| 文件 | 用途 |
| :--- | :--- |
| `feishu_card_replies.patch` | 适配器本地增强：回复渲染成交互卡片（流式原地更新） + 审批以纯文字下发（不依赖卡片回调） |
| `apply_feishu_card_replies.py` | 幂等重放 + 在位校验 + 配置生效值检查 |
| `patch_card_finalize.py` | 卡片终态/同卡收尾 |
| `patch_card_markdown_native.py` | 原生表格 / Card 2.0 元素 |
| `patch_feishu_update_retry.py` | **卡片原地更新加入重试** — 见下方「为什么原地更新要重试」 |

### 为什么原地更新要重试

卡片一开，一条回复不再是一次发送，而是**一串 PATCH 帧**（每 `edit_interval` 一帧）。
发送路径 `_feishu_send_with_retry` 给新消息发送做了 3 次指数退避，但 `edit_message`
里卡片帧和文本帧原本都是**裸调用**（`adapter.py` 的 `message.patch` / `message.update`）。

于是同一类网络故障有两种结果。一次 DNS 抖动（`Temporary failure in name resolution`）
让某一帧失败后，代码的动作不是重试，而是**不可逆降级**：跳出卡片分支、把 message_id
从 `_card_message_ids` 摘掉，此后再也回不到卡片形态。用户看到的要么是「半截卡片 +
一条纯文本终稿」，要么是网关判定未投递成功而重发终稿
（`possible duplicate send`）——同一条回复出现两份形态不同的副本。

`patch_feishu_update_retry.py` 给这两处补上同构重试：3 次、指数退避，且**只重试可恢复
的故障**（网络异常 / 408 / 429 / 5xx）；4xx 语义错误立刻失败，重试只会拖长降级。
重试耗尽才回落原有降级路径 —— **先争取保住卡片，实在不行才降级**。

判别这一层是否在位：

```bash
python3 patches/patch_feishu_update_retry.py --check --agent-dir ~/.hermes/hermes-agent
```

`hermes update` 覆盖安装树后会回到裸调用，`--check` 会报 `[FAIL] 不在位`。

补丁携带两个开关（都在 profile `config.yaml` 的 `platforms.feishu` 下）：

- `card_replies`（默认 false）—— 卡片回复；任何发送/更新失败都回落到原来的 text/post 路径。
- `exec_approval_text`（默认 false）—— 审批以纯文本下发（把 `/approve` 指引写进消息体）。
  卡片回调需要平台应用订阅 `card.action.trigger` 并配回调地址；没配时按钮点了没反应。

回复卡片还依赖 `streaming.enabled: true`（配置顶层）。`streaming.transport: off`
或 `display.platforms.feishu.streaming: false` 也会关闭卡片所需的预览路径。
只开 `card_replies` 不会把普通非流式消息变成卡片；`--config` 会检查这些前置条件。
需要原生表格时，在网关进程环境设置 `HERMES_FEISHU_CARD_V2=1`，使用 Card JSON 2.0。
配置/环境修改后重启网关，确认新进程已连接消息平台；代码探针通过不等于实际投递验收。

## 用法

```bash
P=./patches/apply_feishu_card_replies.py

python3 "$P" --check     # 补丁在位吗？（0=在位 / 1=不在位）
python3 "$P" --apply     # 不在位就补上（幂等；本来在位则直接返回 0）
python3 "$P" --config    # 适配器解析出的生效值（抓「写了配置但读不到」）
```

脚本读环境变量定位目标（都有默认值）：

- `HERMES_AGENT_DIR` —— 宿主安装树，默认 `~/.hermes/hermes-agent`
- `HERMES_PROFILE_DIR` —— 用于 `--config`；未设时依次回落
  `$HERMES_HOME/active_profile`（宿主记录的活动画像）→ 名为 `default` 的画像

## 在位判定：探针优先，git 反向校验作旁证

`--check` 的主判据是**内容探针**（标记函数名 + 两个开关字段名都在适配器里），
而不是只看 `git apply -R` 能不能过。理由：`git apply -R` 是**字节级**判定，
补丁里任何一行注释措辞的漂移都会让它失败，从而把「在位」误报成「丢了」——
那正是这条校验最不该出错的地方。

脚本仍会把反向可应用性作为**旁证打印**出来，供人工判断：

```bash
cd "${HERMES_AGENT_DIR:-$HOME/.hermes/hermes-agent}" && git apply --check -R <patch>
```

> **已知后果**：本仓这份补丁把一行**新增注释**的措辞中性化了（把平台专名换成通名），
> 因此对「用**原始**补丁打过」的安装树，`git apply -R` 会失败（探针仍会正确报「在位」）。
> 想让字节级反向校验也成立，只能用原始补丁重打一遍；**不要**为此改动安装树。

## 幂等重放：什么时候必须重打

1. **`hermes update` 之后必打一次**（更新会覆盖安装树里的 adapter）。
2. 升级后 `--check` 报「不在位」而 `--apply` 报「补丁不能干净应用」，说明上游
   `adapter.py` 已变动，**不要**硬塞。人工核对 diff 后用三方合并：
   ```bash
   cd "${HERMES_AGENT_DIR:-$HOME/.hermes/hermes-agent}" && git apply --3way <patch>
   ```
3. **补丁打上后需重启网关才生效**（adapter 在网关进程内加载，不是每条消息重读）。

## 回退

```bash
cd "${HERMES_AGENT_DIR:-$HOME/.hermes/hermes-agent}" && git apply -R <patch>
```

只改这一个安装树文件，业务仓零改动。

## 合规

本目录两个文件都在仓级门禁的**射程内**（见 `docs/MIGRATION.md`）。补丁里除
`plugins/platforms/feishu/adapter.py` 这个第三方适配器路径外，不得出现任何业务专名；
新增注释也走同一口径。
