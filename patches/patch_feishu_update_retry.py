"""卡片原地更新加入重试 —— 宿主 Hermes 安装树补丁（幂等重放 + 在位校验）。

## 为什么需要这个补丁

`feishu_card_replies.patch` 让回复渲染成交互卡片、流式预览原地更新。卡片一开，
一条回复不再是一次发送，而是**一串 PATCH 调用**：每 0.8 秒一帧，一帧一次
`PATCH /open-apis/im/v1/messages/:id`。

对照发消息那条路径：`_feishu_send_with_retry` 给**新消息发送**做了 3 次指数退避
（1s / 2s），所以一次网络抖动扛得过去。但 `edit_message` 里这两处是**裸调用**：

- 卡片帧：`await self._run_blocking(self._client.im.v1.message.patch, request)`
- 文本帧：`response = await self._run_blocking(self._client.im.v1.message.update, request)`

一次 DNS 抖动（`Temporary failure in name resolution`）就让那一帧直接失败。而代码
在失败后的动作不是重试，是**不可逆降级**：跳出卡片分支、把 message_id 从登记表里
摘掉（`self._card_message_ids.pop(message_id, None)`），此后再也回不到卡片形态。
一帧失败 = 这条回复后面全部退回纯文本。

## 用户看到的不一致

同一类故障，两种表现，取决于失败发生在哪一帧：

1. **降级成文本重发** —— 卡片停在某一帧的内容上，随后另发一条纯文本终稿。
   用户同时看到「半截卡片」和「一条文字」。
2. **网关那侧判定没投递成功** —— 日志
   `Normal final-send NOT suppressed ... possible duplicate send`，终稿再发一次。

两条路都不是内容错了，而是**同一条回复出现了两份形态不同的副本**。这解释的是
「为什么有时一致有时不一致」：取决于当时有没有恰好撞上网络抖动。

## 修法

给这两处加与发送路径同构的重试：3 次、指数退避、**只重试可恢复的故障**
（网络异常 / 408 / 429 / 5xx），4xx 语义错误立刻失败（重试也不会变好，只会拖长）。

重试耗尽才回落到原有的降级路径 —— 也就是说：**先争取保住卡片，实在不行才降级**，
而不是像现在这样一次失败就降级。

## 幂等

- 探测串 `_feishu_update_retryable` 在位即视为已打。
- 锚点必须恰好命中 1 次，否则拒绝写入（文件已漂移，请人工核对）。
- 写入前 `ast.parse`，语法错误绝不落盘。
- `hermes update` 覆盖后会回到裸调用，`--check` 能查出来。
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
from pathlib import Path

ADAPTER = "plugins/platforms/feishu/adapter.py"

#: 幂等探测：这段辅助函数存在 = 补丁在位。
PROBE = "def _feishu_update_retryable(exc: BaseException) -> bool:"

#: 重试次数与发送路径保持一致（``_FEISHU_SEND_ATTEMPTS = 3``）。
ATTEMPTS = 3

HELPER = '''#: 卡片/文本原地更新失败后是否值得重试。
#:
#: 与新消息发送路径（``_feishu_send_with_retry``）不同，这里先判断可恢复性：
#: 一次 DNS 抖动重试就能过，而 4xx 语义错误重试只会拖长降级时间。
_FEISHU_UPDATE_ATTEMPTS = 3
_FEISHU_UPDATE_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def _feishu_update_retryable(exc: BaseException) -> bool:
    """这个异常重试一次有没有可能成功。"""
    import requests as _requests  # 局部导入：adapter 顶层不依赖 requests 必定可用

    if isinstance(exc, (_requests.ConnectionError, _requests.Timeout)):
        return True
    if isinstance(exc, _requests.HTTPError):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        return status in _FEISHU_UPDATE_RETRYABLE_STATUS
    # 传输层原始异常（DNS、连接重置）不带 requests 包装时也按可恢复处理。
    return isinstance(exc, (ConnectionError, TimeoutError))


async def _feishu_update_with_retry(call, *, what: str, attempts: int = _FEISHU_UPDATE_ATTEMPTS):
    """执行一次原地更新调用，可恢复故障按指数退避重试。

    ``call`` 是无参可调用对象，每次重试重新发一次请求。失败到第 ``attempts`` 次
    才把最后一个异常抛出去 —— 调用方原有的降级路径因此保持不变，只是多争取了几次。
    """
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            return await call()
        except Exception as exc:  # noqa: BLE001 — 分类交给 _feishu_update_retryable
            last = exc
            if not _feishu_update_retryable(exc) or attempt >= attempts - 1:
                raise
            wait_seconds = 2 ** attempt
            logger.warning(
                "[Feishu] %s attempt %d/%d failed; retrying in %ds: %s",
                what, attempt + 1, attempts, wait_seconds, exc,
            )
            await asyncio.sleep(wait_seconds)
    raise last if last is not None else RuntimeError("unreachable")


'''

#: 卡片帧：裸 patch → 包进重试。
CARD_OLD = '''            try:
                response = await self._run_blocking(self._client.im.v1.message.patch, request)
                result = self._finalize_send_result(response, "update failed")'''

CARD_NEW = '''            try:
                # 一次 DNS 抖动不该让整条回复永久失去卡片形态：先退避重试，重试耗尽才降级。
                response = await _feishu_update_with_retry(
                    lambda: self._run_blocking(self._client.im.v1.message.patch, request),
                    what="Card patch",
                )
                result = self._finalize_send_result(response, "update failed")'''

#: 文本帧：裸 update → 包进重试。
TEXT_OLD = '''        async def _update(msg_type: str, payload: str) -> SendResult:
            body = self._build_update_message_body(msg_type=msg_type, content=payload)
            request = self._build_update_message_request(message_id=message_id, request_body=body)
            response = await self._run_blocking(self._client.im.v1.message.update, request)
            return self._finalize_send_result(response, "update failed")'''

TEXT_NEW = '''        async def _update(msg_type: str, payload: str) -> SendResult:
            body = self._build_update_message_body(msg_type=msg_type, content=payload)
            request = self._build_update_message_request(message_id=message_id, request_body=body)
            # 同卡片帧：原地更新也吃网络抖动，重试耗尽才把失败交给调用方降级。
            response = await _feishu_update_with_retry(
                lambda: self._run_blocking(self._client.im.v1.message.update, request),
                what="Message update",
            )
            return self._finalize_send_result(response, "update failed")'''

#: 辅助函数的插入锚点：紧跟在模块级 logger 定义之后。
ANCHOR_AFTER = '''logger = logging.getLogger(__name__)'''


def _agent_dir(explicit: str | None) -> Path:
    root = Path(explicit or os.environ.get("HERMES_AGENT_DIR") or "~/.hermes/hermes-agent")
    return root.expanduser()


#: 三步，各带一个在位探针 —— 崩了要能指出是哪一处形态变了。
STEPS = (
    ("helper", ANCHOR_AFTER, ANCHOR_AFTER + "\n\n\n" + HELPER.rstrip("\n"),
     "模块级 logger 定义", "def _feishu_update_retryable"),
    ("card-frame", CARD_OLD, CARD_NEW, "edit_message 的 patch 调用", 'what="Card patch"'),
    ("text-frame", TEXT_OLD, TEXT_NEW, "_update 的 update 调用", 'what="Message update"'),
)


def _missing(text: str) -> tuple[list[tuple[str, str, str]], list[str]]:
    """返回 (待改步骤, 漂移说明)。

    每步各凭自己的探针判定是否在位，所以**打了大半的树**照样能指出还缺哪一处，
    而不是被总探针一句「在位」带过。锚点一次没命中是**漂移**（上游形态变了），
    不是「这棵树本来就没有」，必须报出来；命中多于一次同样拒绝 —— 批量替换一个
    不该批量替换的位置，比不替换更糟。
    """
    todos, drifted = [], []
    for name, old, new, what, probe in STEPS:
        if probe in text:
            continue
        count = text.count(old)
        if count == 0:
            drifted.append("%s 锚点缺失（%s 形态变了）" % (name, what))
        elif count > 1:
            drifted.append("%s 锚点命中 %d 次（应为 1）" % (name, count))
        else:
            todos.append((name, old, new))
    return todos, drifted

def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--apply", action="store_true")
    ap.add_argument("--agent-dir", default=None)
    args = ap.parse_args()

    path = _agent_dir(args.agent_dir) / ADAPTER
    if not path.is_file():
        print("[FAIL] 找不到 %s" % path)
        return 2
    text = path.read_text(encoding="utf-8")
    todos, drifted = _missing(text)

    if args.check:
        if PROBE in text:
            print("[PASS] 卡片原地更新重试补丁：在位")
            return 0
        print("[FAIL] 卡片原地更新重试补丁：不在位")
        for problem in drifted:
            print("  - %s" % problem)
        print("  表现：一次网络抖动就让整条回复降级成纯文本，同一条回复出现两份副本。")
        return 1

    if drifted:
        for problem in drifted:
            print("[FAIL] %s ——文件已漂移，请人工核对" % problem)
        return 2
    if not todos:
        print("[PASS] 已在位，无需重放")
        return 0

    for name, old, new in todos:
        text = text.replace(old, new, 1)
        print("[OK] 已改 %s" % name)

    if PROBE not in text:
        print("[FAIL] 应用后探测串仍缺失（本文件不支持该树形，请人工核对）")
        return 2
    try:
        ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        print("[FAIL] 拒绝写入有语法错误的 adapter：%s" % exc)
        return 2
    path.write_text(text, encoding="utf-8")
    print("[PASS] 补丁已应用；重启网关后生效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
