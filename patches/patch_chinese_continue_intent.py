#!/usr/bin/env python3
"""Extend the host's bounded stall guard for narrow Chinese action tails.

Only the existing classifier is changed. The host still owns tool availability,
continuation budget (two), interim persistence, and finalization. No model call,
service restart, or configuration write is performed by this patcher.
"""
from __future__ import annotations

import argparse
import ast
import os
from pathlib import Path

TARGET = 'agent/agent_runtime_helpers.py'
OLD = '''    return bool(_TRAILING_CONTINUE_INTENT_RE.search(t[-160:]))'''
NEW = '''    # Chinese action tails share the existing length and retry bounds.
    return bool(_TRAILING_CONTINUE_INTENT_RE.search(t[-160:])
                or _chinese_continue_intent(t))'''
LEGACY_HELPER = r'''
# Narrow local-language extension: only a final sentence announcing an immediate
# inspection/test action. Advice, quotations, and approval waits are not actions.
_TRAILING_CONTINUE_INTENT_ZH_RE = re.compile(
    r"(?:^|[。！？.!?\n]\s*)"
    r"(?P<tail>(?:我)?(?:现在|接下来|下面|随后|先)(?:我)?(?:再|就|来)?"
    r"(?:查清|检查|排查|核对|定位|读取|查看|复现|验证|"
    r"运行(?:测试|用例)|跑(?:一次|一遍)?(?:测试|回归)|执行(?:测试|验证))"
    r"[^。！？.!?\n]{0,130}[。.…：:]?\s*)$"
)

def _chinese_continue_intent(text: str) -> bool:
    # Ignore fenced examples (including an unclosed fence); never act on quoted advice.
    if "```" in text or "~~~" in text:
        return False
    match = _TRAILING_CONTINUE_INTENT_ZH_RE.search(text)
    if not match:
        return False
    tail = match.group("tail")
    return not any(cue in tail for cue in (
        "建议", "请你", "需要你", "等你", "等用户", "等待", "待确认",
        "确认后", "批准", "授权", "如果", "暂不", "不执行", "无需",
        "已完成", "已经", "不必", "不要", "明天", "下次", "稍后", "可以吗",
    ))

'''

# Exact previous release bytes are retained only to recognize supported upgrades.
HELPER = r'''
# Only a final sentence announcing an immediate inspection/test action.
# Bare verbs require a technical diagnostic object, not arbitrary advice/actions.
_TRAILING_CONTINUE_INTENT_ZH_RE = re.compile(
    r"(?:^|[。！？.!?\n]\s*)"
    r"(?P<tail>(?:"
    r"(?:我)?(?:现在|接下来|下面|随后|先)(?:我)?(?:再|就|来)?"
    r"(?:查清|检查|排查|核对|定位|读取|查看|复现|验证|"
    r"运行(?:测试|用例)|跑(?:一次|一遍)?(?:测试|回归)|执行(?:测试|验证))"
    r"[^。！？.!?\n]{0,130}"
    r"|(?:我)?(?:查看|看|查)"
    r"[^。！？.!?\n]{0,65}(?:测试|用例|回归|日志|报错|失败|根因|召回|退出码)"
    r"[^。！？.!?\n]{0,65}"
    r")[。.…：:]?\s*)$"
)

def _chinese_continue_intent(text: str) -> bool:
    # Fenced examples, advice, questions, deferred work and completed statements
    # must not trigger a tool continuation. The host owns the bounded retry loop.
    if "```" in text or "~~~" in text:
        return False
    match = _TRAILING_CONTINUE_INTENT_ZH_RE.search(text)
    if not match:
        return False
    tail = match.group("tail")
    return not any(cue in tail for cue in (
        "建议", "你", "您", "大家", "等待", "待确认", "待用户",
        "确认后", "批准", "授权", "如果", "暂不", "不执行", "无需",
        "完成", "已经", "已", "完毕", "通过了", "不必", "不要",
        "明天", "下次", "稍后", "可以吗", "吗", "？", "?",
        "查不到", "看不到", "没有发现", "没发现", "未发现", "未见",
        "不能", "不了", "不用", "不需要", "即可", "便可", "什么", "如何",
        "根因是", "原因是", "结果是", "结果如下", "失败了", "无异常",
        "查到", "看到", "看过", "查过", "测试通过", "回归通过",
        "“", "”", "‘", "’", '"', "'", "`",
    ))

'''

def patched_source(source: str) -> str:
    """Validate the exact supported function before producing any bytes to write."""
    tree = ast.parse(source)
    matches = [n for n in tree.body if isinstance(n, ast.FunctionDef)
               and n.name == 'trailing_continue_intent']
    if len(matches) != 1:
        raise ValueError('expected exactly one trailing_continue_intent function')
    fn = matches[0]
    lines = source.splitlines(keepends=True)
    body = ''.join(lines[fn.lineno - 1:fn.end_lineno])
    # Preserve the narrow host preconditions; fail closed when upstream changes them.
    if ('len(t) > _TRAILING_CONTINUE_INTENT_MAX_CHARS' not in body
            or 't = (text or "").strip()' not in body):
        raise ValueError('host classifier preconditions changed')
    if NEW in body:
        if source.count(NEW) != 1:
            raise ValueError('partial or drifted patch')
        if source.count(HELPER) == 1 and LEGACY_HELPER not in source:
            return source
        if source.count(LEGACY_HELPER) == 1 and HELPER not in source:
            updated = source.replace(LEGACY_HELPER, HELPER)
            ast.parse(updated)
            return updated
        raise ValueError('partial or drifted patch')
    if source.count(OLD) != 1 or OLD not in body or '_chinese_continue_intent' in source:
        raise ValueError('unsupported or partially patched classifier')
    updated = source.replace(OLD, NEW)
    pos = sum(len(line) for line in lines[:fn.lineno - 1])
    updated = updated[:pos] + HELPER + updated[pos:]
    ast.parse(updated)
    return updated

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument('--agent-dir', type=Path, default=Path(os.environ.get(
        'HERMES_AGENT_DIR', str(Path.home() / '.hermes/hermes-agent'))))
    args = parser.parse_args(argv)
    path = args.agent_dir / TARGET
    try:
        source = path.read_text(encoding='utf-8')
        updated = patched_source(source)
    except (OSError, ValueError, SyntaxError) as exc:
        print('unsupported host: %s' % exc)
        return 2
    if source == updated:
        print('already applied')
        return 0
    if args.check:
        print('not applied')
        return 1
    path.write_text(updated, encoding='utf-8')
    print('applied Chinese continuation classifier')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
