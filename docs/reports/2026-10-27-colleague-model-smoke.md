# 只读同事的模型冒烟检查

`tools/colleague_model_smoke.py` 用一段临时生成的无业务源码，检查模型适配器能否返回带引用的分析结果，以及结果能否绑定到测试同事并生成“主 Bot 转交”署名。它不会连接消息平台，也不会启动后台队列或真正的常驻同事。

## 如何运行

由管理员先准备并检查自己的模型适配器及其依赖。适配器需要是 Python 可导入的 `模块名:函数名`，函数接收 `messages, **options`，返回分析器要求的 JSON 对象或对应的 JSON 字符串：`conclusion`、`claims`（每项包括 `statement` 和包含 `source_id`、`line`、`quote` 的 `evidence`）以及 `limitations`。引用必须逐字来自提供的源码行。

在仓库目录运行，例如：

```bash
python3 tools/colleague_model_smoke.py --model-adapter your_adapter:call_model --timeout 120
```

模块名只是示例，需要由管理员替换。脚本没有内置供应商配置，也不接收命令行凭据；模型所需的认证、网络和超时配置由管理员在适配器的运行环境中提供。适配器是受信任代码，可能访问网络或自行写日志，运行前应自行审查。

## 结果怎么看

脚本只输出一组布尔值。`adapter_loaded` 表示适配器已加载；`model_called` 表示已进入调用，并不保证提供商实际完成请求；`report_validated` 表示引用能在临时源码快照中找到；`member_bound` 和 `signed_relay` 表示本地测试记录与署名一致；`timed_out` 表示超过总时限。全部通过时 `ok` 为真，退出码为 0；否则退出码为 1。

脚本将适配器进程的标准输出与错误输出丢弃，不打印回答、异常、凭据或临时路径。临时目录在结束时清理，但适配器自己创建的外部日志或文件由适配器负责。输出为真也不能证明适配器接入的是真实模型、结论正确，或消息平台已收到消息；需要管理员分别验证。

仓库中的 `tests/test_colleague_model_smoke.py` 使用假模型做离线测试，不会自行调用真实模型。

## 一次隔离实测

2026-10-27 12:12（北京时间）在远端一次性私有目录，使用管理员现有模型配置对合成源码运行检查。模型调用、带引用的结果校验、成员绑定和署名构造均返回通过，未超时。该次实测没有接入消息平台，也没有验证真实队列、后台进程或持续对话。远端临时目录已在检查后清理。

实测前后，工作树与远端副本的以下文件 SHA-256 一致：

| 文件 | SHA-256 |
| --- | --- |
| `agentcore/__init__.py` | `50888dd67e89d1b75afb80e35d424e251e24e5e09e5964aaa075b7dcf717e6df` |
| `agentcore/analysis_runner.py` | `35e926cbea02fcab2e4ecb2480b36883d5a72342bd807df073a1b70e1d4f2618` |
| `agentcore/colleague_registry.py` | `a9671cf9bfa033383cd682a7c47e255dfbdfd9ee08a9b24243ce37135951fcca` |
| `agentcore/colleague_dialogue.py` | `2b9005fa25bc418db08d59ffe7f47f3147a164f9c07440193674db7df9388ccd` |
| `agentcore/colleague_readonly_runtime.py` | `70505eb2ec7c149cb170e9319479200f329b76d4e3f445ca25985aa22109347a` |
| `agentcore/colleague_readonly_dialogue.py` | `b622b875dee4b989a30fd22ced14a641607b1846a77225f50ddb3c9d50eb124a` |
| `tools/colleague_model_smoke.py` | `ac74ac133b27e15b17acc8f2784d4661b9dce842927f1aef4c603da25acc79a6` |
