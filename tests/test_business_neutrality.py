"""业务无关性门禁 —— 把「通用引擎」与「业务约定」分开的硬约束。

规则：``agentcore/`` 包内**可执行代码**不得出现任何具体企业的业务字面量。
扫描时排除：
- ``vendor/``（第三方二进制与随包资源）；
- 注释（``#``）与文档字符串（module/class/function 的第一个字符串表达式）,
  它们是「为什么这么设计」的历史说明，不是运行时代码。

这样，残留的业务字面量只可能出现在真正的代码里（正则词表、种子常量、
默认参数、prompt 模板等），正是本门禁要拦住的东西。

**业务约定应由宿主通过 MemoryConfig 注入**，而不是写死在引擎里。

**词表不在这里**：单一事实源在仓级门禁 ``test_repo_neutrality.py``（射程更广的那道）。
本文件按文件名动态加载它 —— 两处各留一份词表是漂移的起点，漂移的那天没人会发现。
"""

from __future__ import annotations

import ast
import importlib.util
import io
import re
import tokenize
from pathlib import Path

import pytest

#: 词表单一事实源所在文件（仓级门禁）。按文件名加载，避免 test 之间产生 import 顺序耦合。
_WORDLIST_OWNER = Path(__file__).resolve().parent / "test_repo_neutrality.py"


def _load_banned():
    spec = importlib.util.spec_from_file_location("_agentcore_banned_words", _WORDLIST_OWNER)
    if spec is None or spec.loader is None:  # pragma: no cover - 该文件一定与本文件同目录
        raise RuntimeError("找不到词表源：%s" % _WORDLIST_OWNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return tuple(module.BANNED)


#: 禁止出现在引擎代码里的业务字面量（大小写不敏感）。
BANNED = _load_banned()

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "agentcore"


def _char_offset(line_text: str, byte_offset: int) -> int:
    """把 ast 的 UTF-8 字节列偏移换算成字符列偏移（中文行必须换算）。"""
    return len(line_text.encode("utf-8")[:byte_offset].decode("utf-8", "ignore"))


def _docstring_spans(tree: ast.AST, lines):
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            srow, erow = first.lineno, first.end_lineno
            yield (
                srow,
                _char_offset(lines[srow - 1], first.col_offset),
                erow,
                _char_offset(lines[erow - 1], first.end_col_offset),
            )


def code_only(source: str) -> str:
    """返回去掉注释与文档字符串后的源码文本（保留行号与所有代码字符串字面量）。

    两个要点（原实现踩过坑）：
    1. **注释用原始源码 tokenize 取范围**。先脱敏再 tokenize 会让被清空的
       缩进块变成非法缩进（实测 IndentationError），把「门禁自身报错」误报成违规。
    2. **按字符位置原地替换为空格，逐行保留前导缩进**，绝不删除整行内容。
    """
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)

    def blank(srow: int, scol: int, erow: int, ecol: int) -> None:
        """把 [srow,scol]..[erow,ecol] 区域替换成空格，保留换行与前导缩进。"""
        if srow == erow:
            line = lines[srow - 1]
            lines[srow - 1] = line[:scol] + " " * (ecol - scol) + line[ecol:]
            return
        for row in range(srow, erow + 1):
            line = lines[row - 1]
            start = scol if row == srow else 0
            end = ecol if row == erow else len(line.rstrip("\n"))
            if end < start:
                continue
            lines[row - 1] = line[:start] + " " * (end - start) + line[end:]

    for span in _docstring_spans(tree, lines):
        blank(*span)

    # 注释范围必须来自**原始**源码：脱敏后的文本可能已无法 tokenize。
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            blank(*tok.start, *tok.end)
    return "".join(lines)


def _pattern(token: str) -> "re.Pattern[str]":
    # ASCII 词加标识符边界，避免把两三个字母的短词误判成某个长英文词里的一段；中文直接子串匹配。
    if all(ord(ch) < 128 for ch in token):
        return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(token) + r"(?![A-Za-z0-9_])",
                          re.IGNORECASE)
    return re.compile(re.escape(token))


def _iter_python_files():
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if "vendor" in path.parts:
            continue
        yield path


def _violations(source: str):
    code = code_only(source)
    for token in BANNED:
        for match in _pattern(token).finditer(code):
            line_no = code.count("\n", 0, match.start()) + 1
            yield token, line_no, code.splitlines()[line_no - 1].strip()[:120]


def test_package_exists():
    assert PACKAGE_ROOT.is_dir(), "agentcore 包目录不存在"
    assert list(_iter_python_files()), "没有可扫描的源码文件"


def test_no_business_literals_in_agentcore_source():
    found = []
    for path in _iter_python_files():
        source = path.read_text(encoding="utf-8")
        for token, line_no, text in _violations(source):
            found.append("%s:%s [%s] %s" % (path.relative_to(PACKAGE_ROOT.parent),
                                            line_no, token, text))
    assert not found, (
        "agentcore 包内代码出现业务字面量（应改为由 MemoryConfig 注入）：\n"
        + "\n".join(found))


def test_business_words_only_in_injected_config():
    """默认 MemoryConfig 保持中性：不内置任何企业的领域名/代际名/业务词表。"""
    from agentcore.memory.config import MemoryConfig

    default = MemoryConfig()
    blobs = [*default.seed_domains, *default.seed_genres, *default.era_domains]
    for pattern, _weight, domain in default.seed_domain_signals:
        blobs.extend([pattern, domain])
    for prefix, domain in default.seed_source_prefixes:
        blobs.extend([prefix, domain])
    joined = "\n".join(str(b) for b in blobs)
    for token in BANNED:
        assert not _pattern(token).search(joined), (
            "默认配置里出现业务字面量 %r，应由宿主注入" % token)


@pytest.mark.parametrize("token", BANNED)
def test_gate_detects_synthetic_violation(token):
    """自检：门禁确实能抓到代码里的业务字面量，而不是一个恒真断言。"""
    sample = "def f():\n    # %s\n    return \"%s\"  # 业务字面量\n" % (token, token)
    # 注释会被剥离，但代码字符串里的字面量必须被检出
    hits = list(_violations(sample))
    assert any(hit[0].lower() == token.lower() for hit in hits)