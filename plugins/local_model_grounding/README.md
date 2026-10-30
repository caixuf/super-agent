# local-model-grounding

为运行中的助手提供两类本地模型接地（grounding），两个模型**都不做生成**：

| 模型 | 作用 | 接口 |
| :--- | :--- | :--- |
| decider-4b | 把最近一次失败日志归到固定候选集，给出校准过的置信度 | `POST /decide` |
| bge-m3 | 召回 `docs/handover` 里的带日期先例 | `POST /encode` |

模型由另一台机器（直连网线）上的 `scripts/ops/local_model_gateway.py` 提供；本插件只做 HTTP 客户端与注入。

## 行为（每个回合 `pre_llm_call`）

1. 从本轮消息历史里找出**最近一条失败文本**（命中 ERROR/FATAL/Traceback/FAIL/… 才触发）。
   没有失败文本就不调用模型，不做任何注入。
2. decider-4b 单条判定，候选集固定 7 类，置信度门槛默认 0.6。
   - 判定为 环境问题 / 数据缺失 / 依赖服务异常 **且** 置信度 ≥ 门槛：注入确定性指令
     「不要为了变绿去改断言、删 check 或凑动作；先核对缺失项并报告」。
   - 置信度不足：**不注入任何结论**，只写审计。低置信度不得伪装成"就是环境问题"。
3. bge-m3 按「用户消息 + 失败摘要」召回 top-k 先例，带 `[YYYY-MM-DD]` 与文件路径，
   并明确标注「仓内历史结论，非当前真相」。

相同内容在同一会话内只注入一次；索引在后台线程构建，不在回合内阻塞。

## 配置（profile `config.yaml`）

```yaml
plugins:
  enabled:
    - local-model-grounding
  entries:
    local-model-grounding:
      settings:                  # 与既有插件一致：配置放在 settings 下
        enabled: true
        gateway_url: "http://<gateway-host>:8920"
        token_file: "~/.config/local-model/token"   # 0600，勿写进仓
        docs_roots:
          - "~/<platform-root>/docs/handover"
        index_dir: "~/.local/state/local-model-grounding/index"
        audit_path: "~/.local/state/local-model-grounding/audit.jsonl"
        min_confidence: 0.6      # 只能调高，低于 0.6 会被抬回
        top_k: 5
        max_context_chars: 4000
        rebuild_min_interval_s: 600   # 文档变更后最多每 10 分钟重建一次索引（0 = 不节流）
        timeout: 10
```

## 安装

```bash
ln -s ~/code/super-agent-t6/plugins/local_model_grounding \
      ~/.hermes/profiles/<profile>/plugins/local-model-grounding
hermes --profile <profile> plugins enable local-model-grounding
# 然后按上文补 plugins.entries，再重启 hermes-gateway-<profile>.service
```

首次启用后索引会在后台构建；`index_dir` 下出现 `meta.json` + `vectors.f32` 即完成。

索引重建策略：文档一旦变更（另一个 agent 提交 handover 也算），后台线程会重建，
但受 `rebuild_min_interval_s`（默认 600s）节流，且**永不在回合内同步重建** ——
重建要重新 embedding 全量文档（实测 219s），放在回合里会直接毁掉响应时间。
索引过期时该回合只是没有先例注入，不影响判定与回合本身。

## 审计

`audit_path` 为追加式 JSONL，每行一条：

| kind | 关键字段 |
| :--- | :--- |
| `decider` | `source, options, choice, confidence, threshold, decisive, skip_llm, elapsed_ms, session_id` |
| `precedent` | `query_chars, hits[{path,date,score}]` |
| `index` | `built, path` / `error` |

载荷正文不写日志，只有元数据。

## 关闭

- 临时：`LOCAL_MODEL_GROUNDING_OFF=1`（网关进程环境）。
- 永久：`plugins.entries.local-model-grounding.enabled: false` 或 `hermes plugins disable`。

## 已知边界

- decider 的准确率只在小样本上验证过；它是「省一轮推理」的初筛，不是结论来源。
- 网关不可达时插件完全静默（返回 `None`），不影响回合。
- 内网直连地址必须绕过 HTTP 代理：客户端使用显式 no-proxy opener。
