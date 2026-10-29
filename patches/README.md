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

补丁携带两个开关（都在 profile `config.yaml` 的 `platforms.feishu` 下）：

- `card_replies`（默认 false）—— 卡片回复；任何发送/更新失败都回落到原来的 text/post 路径。
- `exec_approval_text`（默认 false）—— 审批以纯文本下发（把 `/approve` 指引写进消息体）。
  卡片回调需要平台应用订阅 `card.action.trigger` 并配回调地址；没配时按钮点了没反应。

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