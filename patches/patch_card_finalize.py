#!/usr/bin/env python3
"""宿主侧补丁：让「一任务一张卡」在工具边界不换消息，且只在真正终态转绿。

背景（docs/reports/2026-10-26_delivery.md 的 T5 一节 / docs/reports/2026-10-26_t5_interaction_form.md §4）：

1. 网关在**工具边界**也会以 ``finalize=True`` 调 ``edit_message``（用来封一段前言）。
   卡片补丁把 ``finalize`` 直接当成终态 → 每次工具调用后卡片都转绿、并且摘掉卡片登记，
   后续段落退回纯文本。
2. 更根本的一层：``_end_segment()`` 对非 cumulative transport 会调
   ``_reset_message_state()``，把 ``_message_id`` 与 ``_accumulated`` 一起清掉。
   于是一个回合里每跨一次工具边界就**新开一条消息**（新卡），最后终稿还是一条纯文本。
   T5 要求「一任务一卡」——必须让卡片适配器显式声明「整轮保持同一个 message handle」，
   由 ``_cumulative_transport()`` 认这个能力；默认关闭，只影响卡片路径。

改动（精确锚点替换，每处 ``count == 1``；每条自带在位探针，可重入）：

1. ``gateway/stream_consumer_transport.py``
   * ``_edit_message`` 增加 ``is_turn_final``，并把它放进传给适配器的 metadata
     （**复制**一份，不就地改 ``self.metadata``；即使上游 metadata 为空，段落结束也要
     把 ``is_turn_final=False`` 送到适配器）；
   * ``_edit_existing`` 的调用点把 ``is_turn_final`` 传下去（上游已有该变量，只是没转发）；
   * ``_first_send`` 收 ``is_turn_final``：段落结束的「首发」也要 ``expect_edits=True``，
     否则第一段正文恰好横跨工具边界时会发成纯文本，卡片路径整轮丢失。
2. ``plugins/platforms/feishu/adapter.py``
   * ``edit_message`` 收 ``metadata``，用 ``finalize and is_turn_final`` 决定是否转绿/摘登记；
   * 声明 ``REQUIRES_EDIT_FINALIZE``（仅本增强开启时）——正文没变时网关会短路收尾编辑，
     没有它卡片永远摘不掉「运行中」；
   * 声明 ``PERSISTENT_STREAM_MESSAGE``（仅本增强开启时）——工具边界不换消息。
3. ``gateway/stream_consumer.py``：``_cumulative_transport()`` 认 ``PERSISTENT_STREAM_MESSAGE``，
   让 ``_end_segment()`` 不再清掉消息句柄与累计正文。

默认关闭：三个能力都取 ``bool(self._card_replies)``，``card_replies=False`` 时
``REQUIRES_EDIT_FINALIZE`` / ``PERSISTENT_STREAM_MESSAGE`` 都是 False，网关行为与改动前一致。

用法：``python3 patch_card_finalize.py --check | --apply [--agent-dir DIR]``
退出码：0 = 在位/成功；1 = 不在位（--check）；2 = 锚点对不上（文件已漂移，需人工核对）。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

TRANSPORT = "gateway/stream_consumer_transport.py"
CONSUMER = "gateway/stream_consumer.py"
ADAPTER = "plugins/platforms/feishu/adapter.py"

# ── 1. 网关：_edit_message 传 is_turn_final ──────────────────────────────
T_OLD = '''    async def _edit_message(self, *, message_id: str, content: str, finalize: bool = False):
        """Edit via the adapter, passing routing metadata when supported."""
        # Contract: adapters must accept finalize= even when False (test-guarded).
        kwargs = dict(chat_id=self.chat_id, message_id=message_id, content=content,
                      finalize=finalize)
        if self.metadata:
            try:
                params = inspect.signature(self.adapter.edit_message).parameters
                if "metadata" in params or any(
                    param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()):
                    kwargs["metadata"] = self.metadata
            except (TypeError, ValueError):
                pass
        return await self.adapter.edit_message(**kwargs)'''

T_NEW = '''    async def _edit_message(self, *, message_id: str, content: str, finalize: bool = False,
                            is_turn_final: bool = True):
        """Edit via the adapter, passing routing metadata when supported."""
        # Contract: adapters must accept finalize= even when False (test-guarded).
        kwargs = dict(chat_id=self.chat_id, message_id=message_id, content=content,
                      finalize=finalize)
        if self.metadata:
            try:
                params = inspect.signature(self.adapter.edit_message).parameters
                if "metadata" in params or any(
                    param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()):
                    # 段落结束（工具边界）与整轮结束都以 finalize=True 到达，只有后者
                    # 可以摘掉卡片的「运行中」。复制一份再改，绝不动共享的 self.metadata。
                    md = dict(self.metadata)
                    md["is_turn_final"] = bool(is_turn_final)
                    kwargs["metadata"] = md
            except (TypeError, ValueError):
                pass
        return await self.adapter.edit_message(**kwargs)'''

# ── 1b. 网关：上游 metadata 为空时，段落结束也要把标志送到适配器 ─────────
T2_OLD = '''        if self.metadata:
            try:
                params = inspect.signature(self.adapter.edit_message).parameters
                if "metadata" in params or any(
                    param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()):
                    # 段落结束（工具边界）与整轮结束都以 finalize=True 到达，只有后者
                    # 可以摘掉卡片的「运行中」。复制一份再改，绝不动共享的 self.metadata。
                    md = dict(self.metadata)
                    md["is_turn_final"] = bool(is_turn_final)
                    kwargs["metadata"] = md'''
T2_NEW = '''        if self.metadata or not is_turn_final:
            try:
                params = inspect.signature(self.adapter.edit_message).parameters
                if "metadata" in params or any(
                    param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()):
                    # 段落结束（工具边界）与整轮结束都以 finalize=True 到达，只有后者
                    # 可以摘掉卡片的「运行中」。复制一份再改，绝不动共享的 self.metadata。
                    # 上游 metadata 为空时也要发：段落结束缺了 is_turn_final，适配器会按
                    # 默认 True 处理，卡片又在工具边界转绿。
                    md = dict(self.metadata) if self.metadata else {}
                    md["is_turn_final"] = bool(is_turn_final)
                    kwargs["metadata"] = md'''

# ── 2. 网关：调用点把 is_turn_final 传下去 ───────────────────────────────
C_OLD = '''        result = await self._edit_message(message_id=self._message_id, content=text,
                                          finalize=finalize)'''
C_NEW = '''        result = await self._edit_message(message_id=self._message_id, content=text,
                                          finalize=finalize, is_turn_final=is_turn_final)'''

# ── 3. 适配器：随开关声明 REQUIRES_EDIT_FINALIZE ─────────────────────────
# 与 telegram / dingtalk 用类属性同一约定；这里必须是实例属性（随 card_replies 走）。
P_OLD = '''        self._allowed_group_users = set(settings.allowed_group_users)
        self._admins = set(settings.admins)
        self._default_group_policy = settings.default_group_policy or settings.group_policy'''
P_NEW = '''        self._allowed_group_users = set(settings.allowed_group_users)
        self._admins = set(settings.admins)
        self._default_group_policy = settings.default_group_policy or settings.group_policy
        # 卡片路径要一个**显式**的终态编辑：正文没变时网关会短路掉收尾编辑，卡片就永远
        # 摘不掉「运行中」。只在本增强开启时声明，纯文本路径维持原行为。
        self.REQUIRES_EDIT_FINALIZE = bool(self._card_replies)'''

# ── 4. 适配器：声明「整轮保持同一 message handle」 ───────────────────────
# 必须在 REQUIRES_EDIT_FINALIZE 那一行之后应用（锚点依赖它先存在）。
D_OLD = '''        self.REQUIRES_EDIT_FINALIZE = bool(self._card_replies)'''
D_NEW = '''        self.REQUIRES_EDIT_FINALIZE = bool(self._card_replies)
        # 卡片在整轮里是同一个 message_id：工具边界**不换消息**，只在同一张卡上推进。
        # 没有这一条，网关的 _end_segment() 会清掉 _message_id/_accumulated，每个段落
        # 新开一张卡，最后终稿又是纯文本 —— 正是「一任务一卡」要挡掉的形态。
        self.PERSISTENT_STREAM_MESSAGE = bool(self._card_replies)'''

# ── 5. 网关：cumulative transport 认这个能力 ─────────────────────────────
G_OLD = '''    def _cumulative_transport(self) -> bool:
        """Stream-is-the-message drafts and WeCom native: one append-only stream per turn."""
        stream_draft = self._stream_is_message() and self._use_draft_streaming
        return stream_draft or self._use_native_streaming'''
G_NEW = '''    def _cumulative_transport(self) -> bool:
        """Stream-is-the-message drafts, WeCom native, and adapters that explicitly keep one
        message per turn (a stable platform handle, e.g. a card)."""
        persistent = getattr(self.adapter, "PERSISTENT_STREAM_MESSAGE", False) is True
        stream_draft = self._stream_is_message() and self._use_draft_streaming
        return stream_draft or self._use_native_streaming or persistent'''

# ── 6. 适配器：edit_message 收 metadata + 只在整轮结束转绿 ───────────────
E_OLD = '''    async def edit_message(self, chat_id: str, message_id: str, content: str, *, finalize: bool = False) -> SendResult:
        """Edit a previously sent Feishu text/post message."""
        if not self._client:
            return SendResult(success=False, error="Not connected")

        content = self.format_message(content)

        # 卡片消息：原地更新卡片内容（流式预览不重排；finalize 帧转绿并摘掉运行时长）。
        if getattr(self, "_card_replies", False) and self._is_card_message(message_id):
            card = self._build_reply_card(
                content, finalize=finalize, started_at=self._card_message_ids.get(message_id))'''
E_NEW = '''    async def edit_message(self, chat_id: str, message_id: str, content: str, *,
                           finalize: bool = False,
                           metadata: Optional[dict[str, Any]] = None) -> SendResult:
        """Edit a previously sent Feishu text/post message."""
        if not self._client:
            return SendResult(success=False, error="Not connected")

        content = self.format_message(content)

        # 卡片消息：原地更新卡片内容（流式预览不重排）。
        # **只有整轮结束**（is_turn_final）才转绿并摘掉运行时长：工具边界也以
        # finalize=True 到达（网关用它封一段前言），在那里转绿会让卡片提前显示「完成」，
        # 摘掉登记还会让后续段落退回纯文本。
        turn_final = bool(finalize and (metadata or {}).get("is_turn_final", True))
        if getattr(self, "_card_replies", False) and self._is_card_message(message_id):
            card = self._build_reply_card(
                content, finalize=turn_final, started_at=self._card_message_ids.get(message_id))'''

# ── 7. 适配器：只有整轮结束才摘掉卡片登记 ───────────────────────────────
F_OLD = '''                if result.success:
                    result.message_id = message_id
                    if finalize:
                        self._card_message_ids.pop(message_id, None)
                    return result'''
F_NEW = '''                if result.success:
                    result.message_id = message_id
                    if turn_final:
                        self._card_message_ids.pop(message_id, None)
                    return result'''

# ── 8. 网关：_first_send 也要认 is_turn_final（段落结束首发仍建可编辑卡）──
FS1_OLD = '''    async def _first_send(self, text: str, *, finalize: bool) -> bool:'''
FS1_NEW = '''    async def _first_send(self, text: str, *, finalize: bool, is_turn_final: bool = True) -> bool:'''

FS2_OLD = '''            metadata=self._metadata_for_send(final=finalize, expect_edits=not finalize))'''
FS2_NEW = '''            metadata=self._metadata_for_send(
                final=finalize, expect_edits=not (finalize and is_turn_final)))'''

FS3_OLD = '''                return await self._first_send(text, finalize=finalize)'''
FS3_NEW = '''                return await self._first_send(text, finalize=finalize,
                                               is_turn_final=is_turn_final)'''

#: (文件, 旧锚点, 新文本, 在位探针)；顺序敏感：T 在 T2 前，P 在 D 前。
EDITS = [
    (TRANSPORT, T_OLD, T_NEW, "is_turn_final: bool = True):"),
    (TRANSPORT, T2_OLD, T2_NEW, "if self.metadata or not is_turn_final:"),
    (TRANSPORT, C_OLD, C_NEW, "finalize=finalize, is_turn_final=is_turn_final)"),
    (ADAPTER, P_OLD, P_NEW, "self.REQUIRES_EDIT_FINALIZE = bool(self._card_replies)"),
    (ADAPTER, D_OLD, D_NEW, "self.PERSISTENT_STREAM_MESSAGE = bool(self._card_replies)"),
    (CONSUMER, G_OLD, G_NEW,
     'getattr(self.adapter, "PERSISTENT_STREAM_MESSAGE", False) is True'),
    (ADAPTER, E_OLD, E_NEW, "turn_final = bool(finalize and (metadata or {})"),
    (ADAPTER, F_OLD, F_NEW,
     "                    if turn_final:\n                        self._card_message_ids.pop"),
    (TRANSPORT, FS1_OLD, FS1_NEW, "finalize: bool, is_turn_final: bool = True) -> bool:"),
    (TRANSPORT, FS2_OLD, FS2_NEW, "expect_edits=not (finalize and is_turn_final)"),
    (TRANSPORT, FS3_OLD, FS3_NEW,
     "return await self._first_send(text, finalize=finalize,\n"
     "                                               is_turn_final=is_turn_final)"),
]


def agent_dir(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser()
    return Path(os.environ.get("HERMES_AGENT_DIR", Path.home() / ".hermes" / "hermes-agent"))


def read(root: Path, rel: str) -> str:
    p = root / rel
    if not p.exists():
        sys.exit(f"[FAIL] 找不到 {p}")
    return p.read_text(encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="补丁是否在位")
    g.add_argument("--apply", action="store_true", help="幂等应用")
    ap.add_argument("--agent-dir", default=None, help="Hermes 安装树（默认 $HERMES_AGENT_DIR 或 ~/.hermes/hermes-agent）")
    a = ap.parse_args()
    root = agent_dir(a.agent_dir)

    texts = {rel: read(root, rel) for rel in (TRANSPORT, CONSUMER, ADAPTER)}
    missing = [(rel, probe) for rel, _old, _new, probe in EDITS if probe not in texts[rel]]

    if a.check:
        if not missing:
            print(f"[PASS] 卡片终态/同卡补丁：在位（{len(EDITS)} 处探针齐全）")
            return 0
        print(f"[FAIL] 卡片终态/同卡补丁：不在位（缺 {len(missing)} 处）")
        for rel, probe in missing:
            print(f"  - {rel}: 缺 {probe.strip()[:70]!r}")
        return 1

    if not missing:
        print("[PASS] 已在位，无需重放")
        return 0

    # 先在内存里全部替换成功再落盘 —— 半途失败不留半截文件
    for rel, old, new, probe in EDITS:
        if probe in texts[rel]:
            continue
        n = texts[rel].count(old)
        if n != 1:
            sys.exit(f"[FAIL] {rel}: 锚点命中 {n} 次（应为 1）—— 文件已漂移，请人工核对")
        texts[rel] = texts[rel].replace(old, new, 1)
        print(f"[OK] 已改 {rel}: {probe.strip()[:56]}")
    for rel, text in texts.items():
        (root / rel).write_text(text, encoding="utf-8")
    still = [(rel, probe) for rel, _old, _new, probe in EDITS if probe not in texts[rel]]
    print("[PASS] 补丁已应用" if not still else f"[FAIL] 应用后仍缺 {len(still)} 处探针")
    return 0 if not still else 2


if __name__ == "__main__":
    raise SystemExit(main())
