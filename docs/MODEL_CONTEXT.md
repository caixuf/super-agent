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
```

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
