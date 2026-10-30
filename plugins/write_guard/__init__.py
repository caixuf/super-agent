"""write-guard —— 把「长内容写文件落空」的两条纪律变成钩子，而不是提示词。

为什么需要它（这是实证，不是偏好）
-----------------------------------
长内容内联进 ``write_file`` 的 ``content`` 参数，一旦撞上输出 token 上限，JSON 就被截断。
截断发生在**模型输出侧**，工具层拿到的是残缺 JSON：能修的就修，修不了的就换成空对象
（``Unrepairable tool_call arguments for write_file — replaced with empty object``）。
症状是**整篇内容凭空消失，而 agent 以为自己写了** —— 写得越长越容易整篇丢，
所以验收报告、需求总账这类长文首当其冲。

本插件把两条纪律做成代码：

1. ``pre_tool_call``：``write_file`` 的调用**在派发前**检查两件事 ——
   ① ``content`` 超过阈值（默认 8000 字符）→ block，并给出分段写法；
   ② 调用**没有带 content**（参数被输出上限截断后剩下的空壳）→ block，并说明「内容已丢失，
   你的调用是残缺的，别原样重试」。这是第 ① 条的**兜底**：截断发生在钩子之前时，
   钩子看到的是 ``{}``，只有认出这个签名才能把「内容丢了」这件事讲清楚。
2. ``post_tool_call`` + ``transform_tool_result``：每次 ``write_file`` 之后**回读磁盘**
   （存在 + 是普通文件 + 字节数与提交内容相称），不符则把结论注回工具结果。
   「写了」变成「验过写了」。

为什么回读要跨两个钩子
----------------------
``post_tool_call`` 的返回值**被框架忽略**（observer hook 的返回值不改变行为），
所以「报错」不能靠它的返回值。真正能让模型看见错误的通道是 ``transform_tool_result``
（它可以返回替换后的结果字符串）。

**实测踩出来的坑：两个钩子的顺序与直觉相反。** 真实 agent 路径下，
``transform_tool_result`` 跑在 ``handle_function_call`` 内部（``model_tools.py``），
而 ``post_tool_call`` 由外层 executor 在其**之后**才发（``agent/tool_executor.py``）——
即 transform **先于** post。所以「post 算结论 → transform 取用」这种传值设计会丢结论。

因此本插件的做法是**顺序无关**的：``transform_tool_result`` 自己回读磁盘
（它拿到 args 与 result，足够定位文件），``post_tool_call`` 只负责留一条 warning 日志。
两个钩子都不需要靠 ``tool_call_id`` 互相配对，所以 ``tool_call_id`` 单独不唯一
（llama.cpp 会发常量 id）这件事也就自然不影响结论。

判定必须「宁窄勿宽」
--------------------
误报一次「你写失败了」会让模型重写一遍已经正确的长文件，代价比漏判高得多。所以：

* 回读只在**能定位到可信绝对路径**时做（用工具回报的 ``resolved_path``，
  不用 ``args["path"]`` —— 后者会被锚定到 task 的 base dir，直接拿来 stat 会**每次都误判**）；
* 读不到（权限、编码、路径非法）**不判失败**：判据不足不等于失败；
* 工具已报错时**不重复制造错误文本**，但仍然把回读结论作为结构化事实附上；
* ``WRITE_GUARD_OFF=1`` 一键关闭，出问题能立刻止血而不必卸载插件。

阈值为什么是 8000 字符
----------------------
这是**输出侧**的风险线：长内容整篇内联进一次 tool call 必然挤占输出预算，
截断即丢稿。8k 字符以下从未观察到损坏。注意单位是**字符**不是字节 ——
中文一字 3 字节，12KB 的中文稿只有约 4000 字符。阈值可用 ``WRITE_GUARD_MAX_CHARS`` 覆盖。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: ``content`` 超过它就拒绝派发，要求分块写。见模块文档「阈值为什么是 8000 字符」。
DEFAULT_MAX_CHARS = 8000


def _env_flag(name: str) -> bool:
    """只认 '1'/'true'/'yes'/'on' 为真 —— 空值与 '0' 都是关。"""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _is_off() -> bool:
    return _env_flag("WRITE_GUARD_OFF")


def max_chars() -> int:
    """分段阈值。环境变量非法时回落到默认值，绝不因为配错就放行不限。"""
    raw = os.environ.get("WRITE_GUARD_MAX_CHARS", "").strip()
    if raw:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return DEFAULT_MAX_CHARS


# ── ① 派发前：超长拒绝 / 残缺参数识别 ────────────────────────────────


def chunking_hint(limit: int) -> str:
    """分块写法的具体建议。必须**可自纠**：模型知道怎么改才不会原地重试。"""
    return (
        f"改法（每块 ≤{limit} 字符，优先第一条）：\n"
        f"  1) 分块写：先 write_file 写第 1 块，再用 terminal 追加：\n"
        f"     python3 -c \"open('<path>','a',encoding='utf-8').write(open('<part2>').read())\"\n"
        f"  2) 用 execute_code 里的 hermes_tools.write_file() 逐块拼装"
        f"（同进程，不占 tool-call 的输出预算）。\n"
        f"每块写完由 write-guard 自动回读校验。"
    )


def too_long_message(chars: int, limit: int) -> str:
    """超长时给模型看的拒绝语：说清原因、怎么改、下次多少。"""
    return (
        f"write_file refused: content 长 {chars} 字符，超过上限 {limit}。\n"
        "原因：把长文整篇内联进 content 会挤爆输出预算，JSON 被截断后工具层无法修复，"
        "参数被替换成空对象，**整篇内容会凭空消失而你以为写了**。\n"
        + chunking_hint(limit)
    )


def truncated_args_message(limit: int) -> str:
    """调用只剩空壳（参数被输出上限截断）时的拒绝语。"""
    return (
        "write_file refused: this call arrived WITHOUT its content argument.\n"
        "这几乎总是长内容被输出预算截断的残骸：正文已经丢了，文件不会落地，"
        "而你以为写了。别原样重试同一份长文 —— 先重建内容，再分块落盘。\n"
        + chunking_hint(limit)
    )


def _on_pre_tool_call(tool_name: str = "", args: Any = None, **_: Any) -> Optional[dict[str, Any]]:
    """派发前拦下两类 write_file：超长 content、参数被截断后的空壳。

    永不抛错：pre_tool_call 是 fail-closed（抛错会拦掉工具本身，反而更糟）。
    """
    if tool_name != "write_file" or _is_off() or not isinstance(args, dict):
        return None
    try:
        limit = max_chars()
        if "content" not in args:
            # 参数被输出上限截断后剩下的空壳：内容已经丢了。
            logger.warning("write-guard: write_file 调用缺少 content 参数（疑似被截断）")
            return {"action": "block", "message": truncated_args_message(limit)}
        content = args.get("content")
        if not isinstance(content, str):
            return None  # 类型错由工具自己报，别抢它的诊断
        chars = len(content)
        if chars <= limit:
            return None
        logger.info("write-guard: 拒绝超长 write_file（%d 字符 > %d）", chars, limit)
        return {"action": "block", "message": too_long_message(chars, limit)}
    except Exception:  # 判定出问题就放行，绝不因为插件故障卡住写文件
        logger.debug("write-guard pre_tool_call failed", exc_info=True)
        return None


# ── ② 写后：回读校验 ──────────────────────────────────────────────────


def _parse_result(result: Any) -> Any:
    if isinstance(result, str):
        try:
            return json.loads(result)
        except (ValueError, TypeError):
            return None
    return result


def _result_is_error(result: Any) -> bool:
    """工具结果已经是错误形状 → 工具自己已经报错了。"""
    parsed = _parse_result(result)
    return isinstance(parsed, dict) and bool(parsed.get("error"))


def _resolve_target(args: dict[str, Any], result: Any) -> Optional[str]:
    """算出「回读该看哪个路径」。**优先用工具自己回报的绝对路径**。

    为什么不直接用 ``args["path"]``：``write_file`` 会把相对路径锚定到 **task 的 base dir**
    （``_resolve_path_for_task``），那通常不是本进程的 cwd。拿原始相对路径去 ``Path()``
    会解析到别处，于是每次正常短写入都被判成「文件不存在」——**这正是最不能犯的误伤**。
    所以顺序是：``resolved_path`` → ``files_modified[0]`` → 原始 path（且仅当它是绝对的）。
    """
    parsed = _parse_result(result)
    if isinstance(parsed, dict):
        resolved = parsed.get("resolved_path")
        if isinstance(resolved, str) and resolved:
            return resolved
        modified = parsed.get("files_modified")
        if isinstance(modified, list) and modified and isinstance(modified[0], str):
            return modified[0]
    raw = args.get("path")
    if isinstance(raw, str) and raw and (raw.startswith("/") or raw.startswith("~")):
        return raw
    return None  # 相对路径且拿不到 resolved_path → 判据不足，不校验


def _looks_truncated(written_bytes: int, expected_chars: int) -> bool:
    """磁盘字节数明显小于期望 → 落盘不完整。

    UTF-8 中文 3 字节/字，所以磁盘字节数**恒 ≥** 字符数（纯 ASCII 时相等）。
    留 20% 余量只为容忍行尾归一化（CRLF→LF 会让字节数变小，但不会差这么多）；
    再低的字节数只可能是写少了。**故意不比对精确 hash**：那是工具自己 ``verified``
    字段的职责（``_verify_written_hash`` 已经做过），这里要抓的是「验证没跑到/被跳过，
    但文件确实不完整」这类盲区。
    """
    return written_bytes < expected_chars * 0.8


def readback_verdict(path: str, expected_chars: int) -> Optional[str]:
    """回读磁盘，返回问题描述；没问题返回 None。**纯函数**，便于钉住判定。

    只有三种情况判失败：
      * 文件不存在；
      * 存在但不是普通文件（目录 / 设备）；
      * 字节数明显少于期望（见 ``_looks_truncated``）。

    读不到（权限、编码、路径非法）一律**不判失败** —— 判据不足时不要把不确定说成失败。
    """
    try:
        target = Path(path).expanduser()
    except (OSError, ValueError):
        return None
    try:
        if not target.exists():
            return (f"write_file 返回成功，但回读时 {path} 不存在 —— 内容没有落盘。"
                    "重新写一次，并确认 path 是绝对路径或相对当前工作目录。")
        if not target.is_file():
            return f"write_file 返回成功，但 {path} 不是普通文件。"
        written_bytes = target.stat().st_size
    except OSError:
        return None  # 拿不到 stat：证据不足，不判失败
    if expected_chars <= 0:
        return None
    if _looks_truncated(written_bytes, expected_chars):
        return (f"write_file 返回成功，但回读发现内容不完整：磁盘 {written_bytes} 字节，"
                f"期望至少约 {expected_chars} 字符（UTF-8 下字节数不应小于字符数）。"
                "这通常是写入被截断或落到了别的路径 —— 重新读完文件确认后再补写。")
    return None


def _on_post_tool_call(tool_name: str = "", args: Any = None, result: Any = None,
                       task_id: str = "", session_id: str = "", tool_call_id: str = "", **_: Any) -> None:
    """写后回读磁盘，把结论写进日志（observer 钩子的返回值被框架忽略，不能用来报错）。

    每次 write_file 都读一次盘 —— **包括工具自己报错的那次**：验收判据要求「失败写入被当场
    报错」，而「文件没落盘」这个事实必须由回读来确认，不能假设工具报错了就等于内容没写。

    这里**不负责把结论送给模型**：送达由 ``transform_tool_result`` 负责 —— 它自己就能回读
    （args + result 足够），所以**不依赖两个钩子的先后顺序**。这一点是实测踩出来的：
    真实 agent 路径下 ``transform_tool_result`` 跑在 ``handle_function_call`` 内部，
    而 ``post_tool_call`` 由外层 executor 在其**之后**才发 —— 顺序与直觉相反，
    靠「post 算结论 → transform 取用」传值会丢结论（实测表现为返回里没有 readback 字段）。

    本钩子的价值是**那条 warning 日志**：结果里已经带结论时，日志给出时间线上的旁证。

    永不抛错（observer hook 本该 fail-open）。
    """
    if tool_name != "write_file" or _is_off() or not tool_call_id or not isinstance(args, dict):
        return
    try:
        content = args.get("content")
        if not isinstance(content, str):
            return
        target = _resolve_target(args, result)
        if target is None:
            return  # 拿不到可信的绝对路径 —— 判据不足，不校验
        problem = readback_verdict(target, len(content))
        if problem:
            logger.warning("write-guard: 回读校验失败 %s", target)
    except Exception:
        logger.debug("write-guard post_tool_call failed", exc_info=True)


def _on_transform_tool_result(tool_name: str = "", args: Any = None, result: Any = None,
                               task_id: str = "", session_id: str = "", tool_call_id: str = "",
                               **_: Any) -> Optional[str]:
    """把回读结论注进工具结果 —— 这是让「写了」变成「验过写了」的唯一可见通道。

    **自己回读，不依赖 post_tool_call 先跑**：本钩子拿到 args 与 result，足够定位文件并
    读盘。理由见 ``_on_post_tool_call`` 的说明（真实路径上两者顺序与直觉相反）。

    规则：
      * 回读通过 → 结果里带 ``readback.ok=true``，**不置 error、不改原字段**；
      * 回读失败且工具**声称成功** → 置 ``error``（模型与展示层都按失败处理）；
      * 回读失败且工具**已经报错** → 不重复制造错误文本，只把回读结论作为
        ``readback`` 结构化事实附上（错误原因仍以工具自己的为准）。
    """
    if tool_name != "write_file" or _is_off() or not isinstance(result, str):
        return None
    try:
        parsed = _parse_result(result)
        if not isinstance(parsed, dict):
            return None
        content = args.get("content") if isinstance(args, dict) else None
        if not isinstance(content, str):
            return None
        target = _resolve_target(args or {}, result)
        if target is None:
            return None  # 判据不足 → 不改结果
        problem = readback_verdict(target, len(content))
        parsed["readback"] = {"ok": not problem, "detail": problem} if problem else {"ok": True}
        if problem and not _result_is_error(result):
            parsed["error"] = problem  # 工具说成功、回读说没落盘 —— 以回读为准
        return json.dumps(parsed, ensure_ascii=False)
    except Exception:
        logger.debug("write-guard transform_tool_result failed", exc_info=True)
        return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
