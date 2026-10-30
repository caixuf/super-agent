#!/usr/bin/env python3
"""import 边界门禁：agentcore 不得依赖任何第三方/宿主包。

为什么单独建这道闸
------------------
「业务无关性」门禁只扫业务**字面量**（见 BANNED 词表…）。
它能通过，而这个包却仍然 `from some_host_pkg.client import chat` ——
这类依赖是**形态耦合**：不留业务名词，但把库绑死在宿主的目录布局上。
实测就是这样漏掉了 4 处 `from ai_agent.client import chat_completions`。

规则：包内每个模块的**顶层 import** 只能是
  1) Python 标准库；
  2) 本包自身（`agentcore` 前缀，或相对导入 level>0）。

宿主依赖必须改为注入（`MemoryConfig.llm_fn` / `llm_loader` / `embed_fn`），
而不是 import 进来。第三方可选依赖（如 sqlite-vec 扩展）走**文件系统/扩展加载**，
不产生 import 依赖。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "agentcore"

#: 3.10+ 才有 stdlib_module_names；低版本退化成一个够用的白名单。
if hasattr(sys, "stdlib_module_names"):
    _STDLIB = set(sys.stdlib_module_names)
else:  # pragma: no cover
    _STDLIB = {
        "abc", "argparse", "ast", "base64", "collections", "contextlib", "copy",
        "csv", "dataclasses", "datetime", "enum", "fnmatch", "functools", "glob",
        "gzip", "hashlib", "io", "itertools", "json", "logging", "math", "os",
        "pathlib", "random", "re", "shutil", "sqlite3", "struct", "subprocess",
        "sys", "tempfile", "textwrap", "threading", "time", "traceback", "types",
        "typing", "unicodedata", "urllib", "uuid", "warnings", "weakref", "zlib",
    }


def _iter_sources():
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if "vendor" in path.parts:
            continue
        yield path


def _top_level_imports(tree: ast.AST):
    """产出 (行号, 顶层模块名 或 None(相对导入), 原文)。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name.split(".")[0], f"import {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                yield node.lineno, None, f"from {'.' * node.level}{node.module or ''} import ..."
            elif node.module:
                yield node.lineno, node.module.split(".")[0], f"from {node.module} import ..."


def test_all_imports_are_stdlib_or_self():
    violations = []
    for path in _iter_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, mod, raw in _top_level_imports(tree):
            if mod is None:
                continue  # 相对导入 = 本包内部
            if mod == "agentcore" or mod in _STDLIB:
                continue
            violations.append(
                f"{path.relative_to(PACKAGE_ROOT.parent)}:{lineno} [{mod}] {raw}")
    assert not violations, (
        "agentcore 出现宿主/第三方 import 依赖（应改为配置注入）：\n"
        + "\n".join(violations))


@pytest.mark.parametrize("token", ["ai_agent", "autotest_server", "tools"])
def test_host_packages_not_imported(token):
    """自检：门禁确实能抓到宿主包依赖，而不是恒真断言。"""
    src = f"from {token}.client import chat\n"
    mods = [m for _, m, _ in _top_level_imports(ast.parse(src))]
    assert token in mods


def test_gate_detects_synthetic_violation():
    """自检：对合成的违规样本，门禁判定函数确实会产出违规项。"""
    src = "from some_host.client import chat\nimport requests, os\n"
    bad = [m for _, m, _ in _top_level_imports(ast.parse(src))
           if m and m != "agentcore" and m not in _STDLIB]
    assert bad == ["some_host", "requests"]