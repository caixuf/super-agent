# 模型上下文窗口：让端点说明，而不是手写 pin

## 1. 为什么需要它

客户端的模型元数据探测通常只读 OpenAI 标准的 `/v1/models`。很多网关
（包括 LiteLLM）在这个标准列表里只返回 `id / object / created / owned_by`，
上下文窗口放在管理端点 `/v1/model/info` 的
`litellm_params.model_info.max_input_tokens` 里。

于是出现一种很难发现的故障：模型实际支持 1,000,000 级上下文，客户端却因为
探测不到而回退到 256,000。压缩阈值随之提前触发，长会话被过早总结，表现像
「模型变笨了」而不是「配置错了」。

本仓的能力是：**把这个契约变成可执行的体检**，而不是靠人记住某个数字。

## 2. 契约

一个可用的 OpenAI 兼容路由应当满足：

```json
GET /v1/models
{
  "data": [
    {"id": "main", "max_input_tokens": 1048000, "max_output_tokens": 328000}
  ]
}
```

如果网关只在 `/v1/model/info` 暴露窗口，则应由网关侧（例如反向代理）把它
合并进 `/v1/models`。LiteLLM 的原始位置是：

```json
GET /v1/model/info
{
  "data": [
    {"model_name": "main",
     "litellm_params": {"model_info": {"max_input_tokens": 1048000}}}
  ]
}
```

同一个模型组有多个 deployment 时，广告**最小值**：最大值会制造一个
「看起来安全、实际会溢出」的上限。

## 3. 体检

```bash
# 读取 active profile 的 config.yaml，自动找到 model/provider/base_url
bash checks/model_context_check.sh

# 也可以直接指定
bash checks/model_context_check.sh \
  --base-url http://127.0.0.1:4000/v1 --model main

# 机器可读
python3 -m agentcore.model_context --config <profile>/config.yaml --json

# 只看它会挑哪个配置，不探端点
python3 -m agentcore.model_context --resolve-config --home ~ --profile default
```

### 配置定位顺序

脚本**不认死画像配置**：画像配置常常只有插件启用清单（安装器就是这么写的），
本身没有 `model.base_url` / `model.provider → providers.<name>.base_url`，
拿它当唯一来源会让体检永远退化成 SKIP，端点契约从未被真正验证过。
定位顺序是：

| 顺序 | 来源 | 说明 |
| --- | --- | --- |
| 1 | `MODEL_CONTEXT_CONFIG` | 显式指定，不做回退 |
| 2 | `~/.hermes/profiles/<画像>/config.yaml` | **仅当它能解析出 base_url** |
| 3 | `~/.hermes/config.yaml` | 顶层主配置；模型设置实际在这里 |
| 4 | 画像配置 | 兜底：两者都没有时保持原路径，由体检报「没有配置」→ SKIP |

「能解析出 base_url」用的是 `agentcore.model_context.resolve_base_url()`，
和体检本身同一套优先级，不在脚本里复刻第二份语义。

输出示例：

```
base_url: http://127.0.0.1:4000/v1
model:    main
context:  1048000 (source=model/info)
output:   328000
warning:  manual model.context_length=922000 differs from catalog 1048000; remove the pin ...
```

退出码：

| 码 | 含义 | 处理 |
| --- | --- | --- |
| 0 | 拿到窗口 | 正常 |
| 1 | 端点可达但没有窗口 | 契约不满足：修网关的 `/v1/models` |
| 2 | 端点不可达 / 没有配置 | 环境问题，不是契约失败 |

## 4. 配置纪律

- **默认不写 `model.context_length`。** 让端点成为唯一真相源。
- 只有在端点确实无法提供窗口时，才把它作为最后手段的 pin，并在旁边写清原因与移除条件。
- `auxiliary.compression.context_length` 和 `compression.threshold_tokens` 应与探测到的窗口一致；
  体检会报出不一致。
- 手写 pin 与目录不一致时，pin 可能覆盖目录；体检会把它当作警告，而不是静默接受。

## 5. 常见症状对照

| 症状 | 最可能的原因 |
| --- | --- |
| 长会话在同一位置被压缩，/usage 显示 256K | `/v1/models` 没有窗口，客户端回退 256K |
| 配置写了 922000，实际仍按 256K 压缩 | pin 没有落到运行时路由（provider/base_url 不匹配） |
| `/v1/model/info` 有值，`/v1/models` 没有 | 网关没有合并；在网关侧修，或让体检报红 |
| 同一模型组切换 deployment 后窗口变小 | 应按组内最小值广告；体检会以最小值报 |

## 6. 2026-10-29 的现场

- 网关 `/v1/model/info`：`main` / `pro` / `flash` 均为 `max_input_tokens: 1048000`。
- 网关 `/v1/models`：原本没有窗口，客户端回退 256,000；压缩在约 220,000 tokens 触发。
- 网关侧把 `/v1/model/info` 合并进 `/v1/models` 后，`agentcore.model_context` 读到 1,048,000。
- 手写 `model.context_length: 922000` 已删除；它既不等于目录值，也不是运行时路由的 pin。

## 7. 2026-11-06 的现场

- 画像配置 `~/.hermes/profiles/default/config.yaml` 只有插件启用清单，**没有** 模型设置；
  模型设置在顶层 `~/.hermes/config.yaml`（`model.provider: custom:ccp` →
  `providers.ccp.base_url`）。体检此前只看画像配置，因此在本机一律记 SKIP ——
  「窗口来自端点」这条契约实际从未被执行过。
- 定位规则改为「画像配置解析不出 base_url 就退回顶层主配置」后，体检实际探到：
  `context: 1441792 (source=models)`、`output: 393216`。目录值已不是 1,048,000，
  本节第 6 节是 2026-10-29 的历史读数，不能外推到当前端点。
- 现场配置里仍留着手写 `model.context_length: 1048000`，与目录值 1441792 不一致，
  体检按设计报 warning（不阻断）。要消除这条告警应删掉该 pin —— 那是
  `~/.hermes/config.yaml` 的运行时改动，不在本仓验证范围内，需单独授权后处理。
