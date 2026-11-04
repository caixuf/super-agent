# 交互难在哪：流式卡片投递的开源方案调研

日期：2026-11-03。
性质：**外部方案调研，不是验收记录、不是架构决策**。本文引用的外部事实都标了证据等级：

| 标记 | 含义 |
| :--- | :--- |
| `[官方]` | 平台/协议官方文档原文 |
| `[源码]` | 开源实现代码或其自带文档 |
| `[博客]` | 第三方实践文章，未经官方确认 |
| `[未验证]` | 推断，需实测 |

---

## 0. 先说结论

1. **你丢的不是"交互效果"，是三种语义被揉成了一个东西**：`已接受` / `已渲染` / `已送达`。
   HFC 的三种丢法（你的 `2026-11-02-hfc-delivery-loss.md` 已用故障注入证实）本质都是同一件事——
   **把"HTTP 200"当成了"用户看见了"**。开源项目里没有任何一个把这两者混为一谈，都会显式分档。
2. **开源界的成熟解法不是"更好的卡片渲染器"，而是把"状态"从"一条可变消息"里拿出来**，
   变成 **append-only 的事件流 + 带 id 的分片 + 显式终态**。谁还在原地 PATCH 同一条消息，
   谁就必然踩配额、乱序、所有权三个坑。你现在的 `cardkit2_renderer` 仍是"原地 PATCH"模型。
3. **最省事的答案可能是不流式**。Slack 官方对长任务是明确劝退 streaming 的（见 §2.2）。
   你这个场景（agent 跑几分钟、结果是一篇文档）流式的收益远小于它带来的失败面。
   要留流式，就只留"阶段进度"，把最终答案走一次性的、可重试的、有账本的文本投递。
4. **上一版在这里把死代码当成了活链路，已更正。** `FeishuCardKitAdapter` 确实有四处不符合官方契约，
   但它**全仓无调用方**，线上卡片由 HFC 发，所以那些 bug 不在你丢内容的路径上。
   真正在跑的 HFC（本地打过补丁的 wheel）已经**通过全部 8 条投递契约**——问题当前状态是「已修复，但未做过真实回合验收」，不是「还没修」。见 §5.1 与 §5.3。

---

## 1. 为什么这件事天生就难：五个损失源

你在做的事，本质是：**把一个无损的 token 流，投影到一个有损、有配额、有并发、且你不独占写入权的渠道上。**
损失只可能来自这五个地方，所有方案都在防这五个：

| # | 损失源 | 表现 | 你会看到的症状 |
| :--- | :--- | :--- | :--- |
| 1 | **配额** | 平台对你的更新次数/频率设上限 | 输出到一半突然不动了，但 API 一直返回成功 |
| 2 | **时序** | 更新请求是异步的，先发的后到 | 卡片最后显示的是中间某一段，最终答案被旧帧覆盖 |
| 3 | **身份** | 重试时平台返回"原来那条"，但你更新的是"新开的那条" | 消息在，但永远是"正在处理…"，内容永远不出现 |
| 4 | **所有权** | 两个生产者同时写同一张卡 | 两段内容互相打架，或一方被判"已处理"而其实没发 |
| 5 | **终态** | 把"请求被接受"当成"内容已送达" | 后端日志说成功，用户那边什么都没有 |

第 1 条你之前没有记录，而它可能直接解释"有时候丢东西"。见下。

### 1.1 新增硬事实：消息平台单条消息只能编辑 20 次 `[官方]`

消息平台 `im/v1/messages/:message_id`（PUT，编辑消息）官方原文：

> "A message can be edited up to 20 times. Only messages sent by the current operator can be edited."
> "If you need to edit the card message, please use the update the message card sent by the application interface."

即：

- **单条消息编辑上限 20 次**，这是官方明文，不是坊间传闻；
- 超过之后的行为，多篇实践文章描述为**静默失败**：接口返回成功，但内容不再变化 `[博客]`；
- 官方明确区分了两条路：**编辑消息** vs **更新卡片**。卡片消息应该走卡片接口，不该走消息编辑。

如果你链路里任何一段（包括兜底的"文本 fallback"）是用"发一条文本 + 反复编辑"实现的，
那么长答案必然在 20 次之后死掉，而且是静默的。这条要单独去代码里查。

### 1.2 另一条容易忽视的：流式模式期间卡片更新被阻塞 `[官方]`

消息平台流式更新文档原文：

> "During the streaming update mode of the card, when the server receives the card's callback request during user interaction with the card, it will not be able to update the card immediately."

意思是：**用户在卡片上点按钮（审批、澄清、反馈）期间，你的流式更新会更新不上去。**
这解释了一类"点了按钮之后就卡住/丢内容"的现象——不是你的 bug，是模式冲突。
正确做法是交互前先关掉 `streaming_mode`，交互完再按需重开或改走全量更新。

---

## 2. 平台方自己怎么设计的（这部分最值得抄）

### 2.1 消息平台 CardKit：累积全文 + 严格递增序号 + 每次一个 uuid

官方流式更新文档的关键约定 `[官方]`：

| 约定 | 内容 | 为什么重要 |
| :--- | :--- | :--- |
| 内容是**累积全文** | 每次推完整文本，平台自己算增量做打字机 | 天然幂等：丢一帧不影响最终态，下一帧带上全部内容就自愈 |
| `sequence` **严格递增** | 同一张卡上必须比上一次大 | 平台侧有了定序依据，晚到的旧帧可被丢弃 |
| 每次写入独立 `uuid` | 用于重试去重 | 重试不会变成"再写一遍" |
| `streaming_mode: true` **不触发 QPS 限制** | 原文："without triggering the API frequency limit (QPS)" | **你在流式模式下不需要手写去抖去卡 10 次/秒** |
| 单卡片实体卡片/组件级 OpenAPI 限 **10 次/秒** | 非流式路径的限额 | 非流式路径才要节流 |
| 卡片实体**有效期 14 天** | 超期不可再操作 | 账本里存 card_id 要带过期判断 |
| 10 分钟后**自动关闭流式状态** | 未收尾的流会白占实体 `[源码]` | 必须"无论如何都收尾"，异常路径也要 |

> 关于"自动关闭 10 分钟"：消息平台概览页只写了 10 次/秒，10 分钟自动关闭来自第三方 SDK 文档与 HFC 自己的 wiki `[源码]`，
> 建议以真机实测确认后再写进契约。

这里有个反直觉的结论，值得单独点出来：
**你之前那套"单在途 + 合并 + 手写去抖"的复杂度，是在解决一个在 `streaming_mode` 下根本不存在的问题（QPS）。**
真正需要挡的是"重绘次数带来的客户端渲染抖动"，那是体验问题，不是配额问题。
把 QPS 当敌人，就会设计出 `CardStreamRenderer` 这种自己造节流器的结构；
而平台已经把这件事接管了。

### 2.2 Slack：append-only + 服务端收尾 + 显式的会话状态机

Slack 的流式是三个方法 `[官方]`：`chat.startStream` → `chat.appendStream` → `chat.stopStream`。

它的设计选择几乎每一条都在回应上面的损失源：

| Slack 的做法 | 对应解决 |
| :--- | :--- |
| **只有 append，没有"替换整条"** | 消灭时序覆盖（损失源 2）：只能往后加，旧帧不可能盖掉新帧 |
| `stream_ts` 是服务端发的唯一句柄 | 消灭身份错乱（损失源 3）：不存在"新开一条但没人看见" |
| **必须显式 `stopStream`**；不调就由服务端在 10 分钟后用"已流出的内容"finalize，并把未完成任务标 error | 消灭"永远转圈"：**平台替你兜底，且如实标注未完成** |
| 会话状态与消息内容**分开**：`agents.sessions.setStatus(processing / active / suspended)` | 消灭所有权混淆（损失源 4）：进度、内容、会话状态是三样东西 |
| 官方明确劝退：任务可能超过流的上限时，**改用 postMessage + plan block + chat.update** | 承认"流式不适合长任务"，给出降级路径 |

最后一条是全文最该被记住的一句。Slack 官方对 agent 的建议是：

> "A long-running task can outlast a stream, which Slack finalizes automatically 10 minutes after it starts."
> "For tasks that may run longer than a stream allows, post a message when the task starts and update it as work progresses."

**平台方自己都在说：长任务别硬上流式，改成"先发一条 + 更新进度块"。**

---

## 3. 协议与 SDK 层怎么设计的

### 3.1 AG-UI：事件是唯一真相，UI 只是投影

AG-UI 的定位是"agent ↔ 用户界面"的事件协议，走 SSE，每条事件是一行 JSON `[官方]`。

事件family（原文名）：

| 家族 | 事件 |
| :--- | :--- |
| 生命周期 | `RUN_STARTED` / `RUN_FINISHED` / `RUN_ERROR` / `STEP_STARTED` / `STEP_FINISHED` |
| 文本 | `TEXT_MESSAGE_START` / `TEXT_MESSAGE_CONTENT` / `TEXT_MESSAGE_END` / `TEXT_MESSAGE_CHUNK` |
| 工具 | `TOOL_CALL_START` / `TOOL_CALL_ARGS` / `TOOL_CALL_END` / `TOOL_CALL_RESULT` |
| 状态 | `STATE_SNAPSHOT` / `STATE_DELTA` / `MESSAGES_SNAPSHOT` |
| 推理 | `REASONING_START` / `REASONING_MESSAGE_*` / `REASONING_END` |
| 扩展 | `RAW` / `CUSTOM` / meta 事件 |

三条规则值得直接照搬到你的卡片链路上：

1. **一次 run 必须 `RUN_STARTED` 开头，`RUN_FINISHED` 或 `RUN_ERROR` 结尾。** 终态只有两种，且必达其一。
2. **snapshot + delta 分离**：状态用 `STATE_SNAPSHOT`（全量）定基，用 `STATE_DELTA`（RFC 6902 JSON Patch）增量。
   这跟消息平台"累积全文"是同一招的两种写法——**全量兜底 + 增量省带宽**。
3. 官方明确要求：**实现要能容忍乱序交付**（"Implementations should be resilient to out-of-order delivery"），
   同一 id 的事件属于同一逻辑流，靠 id 归并而不是靠到达顺序。

注意 AG-UI 自己**不提供投递保证**。SSE 断了就是断了，它把"送达"这件事留给客户端重连与业务层。
所以它不是投递问题的解药，它是"状态长什么样"的标准化。**别指望换个协议就能不丢内容。**

### 3.2 Vercel AI SDK：分片带 id，失败的分片可以显式作废

AI SDK 的 UI message stream 协议 `[官方]`：

- 消息级：`{"type":"start","messageId":"..."}` … `{"type":"finish"}`
- 文本分片：`text-start` / `text-delta` / `text-end`，**每个 text block 有自己的 `id`**
- 失败与作废：`{"type":"error",...}`、`{"type":"abort","reason":"user cancelled"}`
- **`reset-step`**：作废最近一次 `start-step` 以来的所有分片——
  原文用途："useful when a streamed step is retried and partial output from the failed attempt must be invalidated before replacement output is sent"
- 终止：`data: [DONE]`

这里最值得学的是 `reset-step`：
**它给了"这次尝试的输出作废"一个一等公民的表达**，而不是让新内容去覆盖旧内容、指望看起来没问题。
你的 `drain()` + `close()` 现在是在"保证旧帧不覆盖新帧"，而 AI SDK 的思路是"显式声明旧帧作废"。
前者的正确性依赖时序，后者不依赖。

### 3.3 可直接读的消息平台开源参考实现

| 项目 | 看点 |
| :--- | :--- |
| `feishu.streaming.cardkit`（OpenFeishu SDK） `[源码]` | `stream_card()` 把整个生命周期压成**四次请求**：建实体 → 发消息 → 写累积全文 → 收尾。写入带去抖（0.25s）、序号单调、每次 uuid、收尾必执行。**这就是"够用的最小实现"的样板** |
| LangBot `[源码]` | 消息平台适配器支持"incremental text updates"的流式回复；多平台适配层把消息链与平台格式解耦，值得看它怎么抽接口 |
| `synaptic` 的 `StreamingCardWriter` `[源码]` | 明确写出"为什么不用消息编辑"：消息编辑约 20–30 次隐性上限，卡片实体无此限制；`write()` 内部节流到 ~500ms |
| HFC 自己的 wiki `[源码]` | 记了一条很贵的经验：**投递 UUID 与目标共同绑定实体**。同一投递重试时要复用原实体，否则"IM UUID 返回原消息，你却更新了一张不可见的新实体" |

最后那条正是损失源 3 的教科书案例，而且它就发生在你自己的项目里。

---

## 4. 五个可复用的机制（这才是能带走的东西）

抛开平台差异，所有能用的方案都收敛到这五条。你现在缺的是 2、3、5。

### 机制 1：状态是事件流，UI 是可重建的投影
不要让"卡片内容"成为唯一真相。真相是事件（阶段、token、工具结果、终态），
卡片只是它对某个渠道的一次投影。投影失败可以重投，事件丢了才真丢了。
→ 你现在有 `trajectory_ledger` / `liveness`，方向是对的，但卡片内容没进账本。

### 机制 2：id + 单调序号，是幂等与定序的最小武器
- 内容 id / 投递 id / 事件 id 必须稳定且随重试不变；
- 序号必须单调，让接收方有办法丢弃迟到帧；
- 有了这两样，重试才是安全的，而不是"再发一遍"。
→ 消息平台 `sequence` + `uuid`、Slack `stream_ts`、AG-UI `messageId` 都是这一条的不同写法。

### 机制 3：终态必须显式且分档，"已接受"永不等于"已送达"
最小三档，缺一不可：

| 档 | 含义 | 允许的动作 |
| :--- | :--- | :--- |
| `delivered` | 有平台侧证据表明内容已落到目标消息/卡片上 | 可以抑制原生的兜底发送 |
| `not_sent` | 确定没送出去 | **必须**留一个持久的补发义务 |
| `unknown` | 请求发出但结果不明（超时/连接断） | 不许盲重发，进核对队列，保留现场 |

你的 HFC 报告里那三种丢法，全部是"把 `accepted` 写成了 `delivered`"。
OpenAI Responses 的 `status` 枚举里也有 `incomplete` 且带 `incomplete_details.reason` `[官方]`——
**"没做完"是一个合法终态，不是失败，也不是成功。** 这跟 Slack 超时后"标 error 而不是假装完成"是同一个立场。

### 机制 4：唯一写入者 + 单一出口
一张卡只能有一个生产者；一次用户回合只能产生一份最终回复。
Slack 用 `setStatus` 把"会话状态"从"消息内容"里剥出去，就是为了让这两件事各有各的所有者。
→ 你 FRONTDESK 计划里"一个对外发送出口"那条已经写对了，缺的是代码里的强制（而不是靠约定）。

### 机制 5：兜底路径必须独立于主路径
HFC 的修补方案里最正确的一条是：**sidecar 自己发文本兜底，不依赖 Hermes 再跑一个模型回合。**
因为"再跑一个回合"会把兜底也变成可能失败、且可能重复的路径。
兜底的判据只能是"内容是否已确认送达"，不能是"上一跳是否返回 ok"。

---

## 5. 对照：你的三个丢法分别对应什么

| 你的已证实失效（`2026-11-02-hfc-delivery-loss.md`） | 本质 | 开源里对应的机制 | 你现在的缺口 |
| :--- | :--- | :--- | :--- |
| `/events` 在最终 PATCH 之前就 ack；fence 在后台投递完成前就定稿 | 把 accepted 当 delivered | 机制 3：终态分档 + 有界等待；Slack 的显式 `stopStream` | ack 语义没有三态，`_should_await_card_update` 恒 false |
| 终局 PATCH 与补救建卡都失败，但适配器已返回 `success=True`，原生兜底从未被调用 | 失败被上游吞掉 | 机制 5：独立兜底 + 机制 3：`not_sent` 必留义务 | 无 `not_sent → 文本兜底` 的强制路径 |
| 新回合开始把旧展示标 failed；旧回合终态事件被 `terminal_already_handled` 直接 ack，答案从未投递 | 所有权与终态混淆 | 机制 3 + Slack 的 `superseded ≠ delivered`；判据应是"文本是否已确认" | 判据是"终态是否出现过"，不是"内容是否送达" |
| *（新增怀疑）* 长答案在编辑路径上被 20 次上限静默截断 | 配额 | 机制 1：改用卡片实体，别用消息编辑 | **在死代码里，见 §5.1** |

### 5.1 追查结果（已更正）：那四处 bug 在死代码里

**先更正上一版。** 位置找对了，定性错了：

- `FeishuCardKitAdapter`（`agentcore/feishu_transport.py:541`）除一个单测外**全仓无调用方**；
  `grep -rn 'FeishuCardKitAdapter'` 在 `agentcore/`、`plugins/`、`runtime/`、Hermes 树里都为空。
- 宿主 `plugins.enabled` 里是 `hermes-feishu-card` + `hfc-runtime-dependency`，**卡片由 HFC 发**，不经过这个类。
- 用 `card_emit` / `card_store` 的那条 `plugins.task_progress`，在宿主上是 `disabled`。
- `cardkit2_renderer.CardStreamRenderer` 同样零调用方。

所以那四条确实是真缺陷，但**它们不在你丢内容的路径上**。这一点必须写清楚——
否则就会变成"按错误的地方修了一遍，症状照旧"，然后开始怀疑人生。

保留价值只有一条：**将来谁把它接上去，会立刻复现同一种病**。所以处理方式是「标记为未接线 + 机械门禁」，
而不是花力气去修一个没人用的发射器。

四条原文如下，留作接线时的对照：

| # | 位置 | 不符点 | 官方要求 |
| :--- | :--- | :--- | :--- |
| ① | L591-600 | 发消息 `content` 传卡片 JSON 本身 | `{"type":"card","data":{"card_id":...}}`（参考实现 `feishu.streaming.cardkit`） |
| ② | L625-635 | `batch_update` 传 `{uuid,sequence,card,update_multi}` | 必填 `actions`；整卡替换应走 `PUT /cards/:id` 且 `card.data` 为字符串 |
| ③ | L648-658 | 回退到 `PATCH /im/v1/messages/:id` 编辑卡片消息 | 官方要求卡片消息走卡片接口；消息编辑**上限 20 次** |
| ④ | L629/L632 | 同一 dict 字面量写了两次 `sequence` | —— |

**结论：这不是当前问题的原因。要改的是"别再把它接上来"，不是"把它改对"。**

### 5.2 用一次请求确认①②（30 秒，只读不写生产）

不需要改代码。拿一张测试卡：

```bash
# 用你已建过的 card_id（或新建一张测试卡），打一次正确的 batch_update
curl -X POST "https://open.feishu.cn/open-apis/cardkit/v1/cards/<card_id>/batch_update" \
  -H "Authorization: Bearer <tenant_access_token>" -H "Content-Type: application/json" \
  -d '{"sequence":1,"actions":"[{\"action\":\"partial_update_element\",\"params\":{\"element_id\":\"markdown_1\",\"partial_element\":{\"content\":\"probe\"}}}]"}'
```

- 如果这次返回 `code: 0`，而你的代码里同一动作报错 → 坐实②；
- 再看客户端里那条消息有没有变 → 没变就坐实①（实体与消息没绑）。

> 说明：以上是**代码与官方文档的比对**，不是运行期观测。`actions` 是否必填、
> 缺字段会被判 `10002` 还是被宽容忽略，取决于服务端实现，必须靠上面这条 probe 或一次真实调用确认。
> ①②③ 的**格式不符**是确定的；**实际报错码**是待确认的。

---

### 5.3 真正在跑的链路：HFC 已经是唯一生产者，而且已经修好了

这一节是**实测**，不是推断。

**① 谁是生产者：HFC，没有第二个。**

```
plugins.enabled: hermes-feishu-card, hfc-runtime-dependency   ← 卡片能力来自 HFC
systemd:         hermes-feishu-card-sidecar.service  (active)
ExecStart:       ~/.hermes/tools/hfc-terminal-ack-20261102/venv/bin/python -I -m hermes_feishu_card.runner
```

**② 跑的是什么：本地修过的 wheel，不是上游 release。**

| 项 | 值 |
| :--- | :--- |
| 版本 | `5.0.13` |
| 安装来源 | `file:///tmp/hfc-build/dist/hermes_feishu_streaming_card-5.0.13-py3-none-any.whl` |
| wheel sha256 | `fcbd90431fb051afc6fd54d275d28f8841b469e9f0ef6bc5c7ba3f0a74ab0baa` |
| 补丁签名核对 | `terminal_delivery_state`、`outcome="not_sent"`、`delivery_uuid`、`superseded` 在 site-packages 里均可命中 |

这也解释了一个长期困惑：**验收门永远过不了，不是因为它坏，而是因为它钉的是上游 commit
`e072763…`，而跑的是本地 wheel（`direct_url.json` 里没有 VCS 信息）。**
门是对的，身份是错的——所以要么把修复推成上游 release，要么给这个 build 自己的身份。

**③ 投递契约：8 条全过。**

```bash
V=~/.hermes/tools/hfc-terminal-ack-20261102/venv/lib/python3.12/site-packages
HFC_SOURCE_ROOT=$V PYTHONPATH=$V python3 -m pytest -q tests/test_hfc_delivery_contract.py
# 8 passed in 0.71s
```

包含当初 3 条红的：终局 ack 先于平台投递、确定失败被报成成功、被顶掉后晚到答案被吞。

**④ 但这不等于"交互已经很好用了"。** 没验过的：

- 真实消息平台回合（私有测试 bot）、真机回读
- 该 build 上的进程重启 / 崩溃恢复
- 话题回复、澄清、审批、媒体回传
- 连续两轮长任务 + 排队追问

自动化契约能证明"代码不再说谎"，**不能证明"用户那边好使"**。这两件事必须分开记。

## 6. 建议：HFC 作为唯一交互方式

### 6.1 决定：只留一个生产者

**HFC 作为唯一交互方式，原生 `cardkit2_renderer` 与 `FeishuCardKitAdapter` 保持未接线。**
理由不是 HFC 更先进，而是**一个聊天只能有一个写入者**。你已经为「两个生产者」付过一次学费，再加一个渲染器只会把失败面翻倍。

在这个前提下，「进度」和「答案」仍然要在语义上分开（前者可以流、后者必须可靠），但**都由 HFC 一个进程负责**，不新增第二个出口。
理由：

- 你的结果是文档/报告，用户是**读**它，不是**看它长出来**。打字机效果对阅读体验是负收益。
- 流式把 1 次投递拆成 N 次投递，把失败面放大 N 倍，而你并不因此省时间。
- Slack 官方对长任务的建议就是这么降级的 `[官方]`；这不是妥协，是平台方自己的最佳实践。
- 你现在 `cardkit2_renderer` 已经写好了，沉没成本不该决定架构。它作为**进度展示器**仍然有用。

### 6.2 最小可行方案（不推翻现有代码）

```
用户消息
  │
  ├─ (A) 立刻回一张卡：只有阶段，无正文    ← 复用 cardkit2_renderer，streaming_mode=true
  │        阶段更新 = 累积全文 + 严格递增 sequence + 每次 uuid
  │        9 分钟软熔断：强制 close streaming_mode，改全量更新
  │
  ├─ (B) 后台跑任务（pool/worker，已有）
  │        产出 = 事件 + 最终正文（正文进账本，不进卡片）
  │
  └─ (C) 终态一次投递（唯一出口）
           ├─ 先 close streaming_mode（必做，异常路径也要）
           ├─ 用 delivery_id（稳定 uuid）投递最终正文
           ├─ 结果写 delivery ledger：delivered / not_sent / unknown
           ├─ not_sent  → 文本兜底，同一 delivery_id，一次；失败则告警 + 进核对
           └─ unknown   → 不重发，标记待核对，下次启动对账
```

关键改动只有四处，都是"减复杂度"：

1. **删掉自造的 QPS 节流**，用 `streaming_mode` 的官方豁免；只保留"重绘频率别让客户端抖"的体验节流。
2. **新增 `delivery_ledger`（三态 + delivery_id）**，ack 只代表 accepted。
3. **终态投递前必须 close streaming_mode**，且 `not_sent` 必须有兜底，兜底不依赖模型回合。
4. **"旧展示作废"不构成"旧答案已处理"**：判据改成"该 `delivery_id` 的内容是否已 `delivered`"。

### 6.3 如果你还想要"真流式正文"

那才需要考虑 AG-UI / AI SDK 那套（事件流 + 分片 id + reset-step）。
但请把它当成**第二阶段**，且前提是 6.2 的账本已经跑通并能在故障注入下证明不丢。
没有账本的流式，只是把丢内容变得更难定位。

---

## 7. 可机械判定的验收清单

不用"看起来好了"，用下面 8 条，每条都能自动跑：

| # | 断言 |
| :--- | :--- |
| 1 | 终局 PATCH 被挂起时，HTTP 响应**不**返回 `delivered` |
| 2 | 终局 PATCH 与补救建卡全部失败 → 落 `not_sent`，且文本兜底被调用恰好一次 |
| 3 | 兜底也失败 → 落 `not_sent` 且产生告警，**不**返回 `success=True` |
| 4 | 强制超时 → 落 `unknown`，**不**发生第二次发送 |
| 5 | 重复终态事件 → 只有一次投递，`delivery_id` 相同 |
| 6 | A 被 B 顶掉后 A 晚到 → A 的正文被投递（或明确标为未投递），**不**静默丢弃 |
| 7 | 连续 30 次更新后内容仍与最后一次写入一致（防 20 次编辑上限那一类静默截断） |
| 8 | 卡片在交互回调期间不丢更新：进入交互前已 close `streaming_mode` |
| 9 | 发出去的消息 `content` 含 `{"type":"card","data":{"card_id":...}}`，且更新实体后客户端**可见变化**（不是只返回 200） |
| 10 | 实体更新走官方字段（`batch_update` 带 `actions`，或 `PUT /cards/{id}` 的 `card.data` 为字符串），**不靠消息编辑** |

第 6、7、9、10 条是这次调研新增的，你现有清单里没有。第 9 条尤其关键——
它是"你以为在更新卡片，其实更新了一个没人看得见的实体"的唯一探测器。

---

## 8. 明确没验证的部分

- 消息平台"10 分钟后自动关闭流式状态"、"超 20 次编辑静默失败"的**真机行为**：前者来自第三方 SDK 文档，后者来自实践博客。
  官方只明文写了"最多 20 次"和"streaming_mode 不触发 QPS"。要写进契约必须先实测。
- §5.1 的四条是**静态审读**：格式不符是确定的，但"实际返回哪个错误码"、"线上是否真的每次都回退到消息编辑"
  需要用 §5.2 的 probe 或一次带日志的真实调用来确认。**别拿这四条直接当事故结论用。**
- AG-UI 与本链路的关系：AG-UI 是前端↔agent 的协议，不解决渠道投递；
  把它接进消息平台等于自己写一个"chat platform client"，官方提到的 Channels SDK / OpenTag 是闭源托管服务的一部分。
  **不建议**为了"标准化"去接 AG-UI，收益与成本不成比例。
- 所有引用的外部行为都以 2026-11-02/03 抓取到的文档为准，平台随时可能改。

---

## 附：来源

- 消息平台 编辑消息（"A message can be edited up to 20 times"）：https://open.feishu.cn/document/server-docs/im-v1/message/update
- 消息平台 卡片流式更新概览（streaming_mode / 10 次每秒 / 交互期间不可更新）：https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/streaming-updates-openapi-overview
- 消息平台 更新卡片实体配置（sequence 严格递增）：https://open.feishu.cn/document/cardkit-v1/card/settings
- Slack 发送与调度 / 流式三方法：https://api.slack.com/messaging/sending
- Slack 开发 agent（streaming 上限、plan block 降级、setStatus）：https://docs.slack.dev/ai/developing-agents
- AG-UI 概览：https://docs.ag-ui.com/introduction
- AG-UI 事件与实现注意事项：https://ag-ui.com/concepts/events
- AI SDK Stream Protocols（`text-start`/`text-delta`/`reset-step`/`[DONE]`）：https://ai-sdk.dev/docs/ai-sdk-ui/stream-protocol
- OpenFeishu `feishu.streaming.cardkit`：https://feishu.danling.org/streaming/cardkit
- HFC 自家 wiki（投递 UUID 绑定实体）：https://github.com/baileyh8/hermes-feishu-streaming-card/blob/HEAD/docs/wiki/cardkit-streaming.md

## 9. 本轮对仓库做的改动（2026-11-03）

| 文件 | 改动 | 为什么 |
| :--- | :--- | :--- |
| `integrations/hfc/lock.json` | 新增 `deployed_build`：wheel 哈希、安装来源、已生效补丁、8 条契约结果、**未验证项清单** | 让"跑的是什么"可机械核对，而不是靠版本号口头声明 |
| `tests/test_deployed_hfc_build.py` | 新增：核对记录与已装 artifact 一致（哈希/版本/补丁签名/证据覆盖），并强制 `not_verified` 非空 | 防止"记了一个自己都没装过的身份"；也防止把契约通过说成交互验收 |
| `tests/test_single_card_producer.py` | 新增：非测试代码不得实例化 `FeishuCardKitAdapter` / `CardStreamRenderer`；lock 必须声明单一生产者；docs 必须保留未接线标记 | 把"只有一个卡片生产者"从口头约定变成会红的门禁 |
| `README.md` | 交互与卡片架构段落改为单生产者 + 三态语义 | 原段落还停在"HFC 有已知风险、原生链路待迁移"，与现状不符 |
| `docs/README.md` | 未接线表补两行（`FeishuCardKitAdapter`、`CardStreamRenderer`） | 死代码必须有明确标记，否则下一个人会以为它是活的 |

**没做的事（需要你授权）**：

- 没有重启任何服务、没有改 `~/.hermes` 配置、没有发真实消息平台消息。
- 没有把 lock 里的 `known_delivery_blockers` 清空——那三个是**上游 pin** 的缺陷，
  清空就等于宣称上游没问题。运行中的 build 用 `deployed_build` 另行记账，两条账不混。
- 没有删除死代码。标记 + 门禁比删除安全：删除会牵连那个单测，而"未接线"标记本身就是仓库既有惯例。

## 10. 仍未闭环的事（按优先级）

1. **真实回合验收**：私有测试 bot，抓一次完整的 `收到 → 建卡 → 阶段 → 终态 → 客户端回读`。
   这是唯一能把"交互可用"从推断变成事实的动作。
2. **身份收口**：把修复推成上游 release 并重钉 lock（最干净），或接受"本地 wheel + 哈希"作为长期身份。
   现在这种"门钉上游、跑本地"的状态，会让任何体检都红，久了就没人看体检了。
3. **删或留**：`FeishuCardKitAdapter` 要么删，要么按官方契约重写后再接线。现状（有 bug 且未接线）
   是最差的一种，因为它看起来像是可用的。
---

> 用词说明：仓级门禁禁止业务中文名，本文按仓库既有惯例统一写作 `Feishu`。
