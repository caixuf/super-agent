"""仓级业务无关性棘轮门禁 —— 这个仓里**任何**文件都不得出现业务字面量。

与 ``test_business_neutrality.py`` 的分工（两道门禁，不同射程）：

| 门禁 | 射程 | 允许的残留 |
| :--- | :--- | :--- |
| ``test_business_neutrality.py`` | ``agentcore/`` 包内 ``*.py`` 的**可执行代码** | 注释与文档字符串（那里写的是「为什么这么设计」） |
| 本文件（仓级棘轮） | **全仓所有会进版本库的文件**（``.py`` / ``.md`` / ``.yaml`` / ``.sh`` / ``.service`` / ``.patch`` …） | 三项，见下方 ``ALLOWLIST_*`` |

为什么需要第二道：包内门禁只管代码，于是业务词会从**注释、文档、部署脚本、
模板、技能**这些地方漏进来 —— 一个「业务无关」的仓，README 里写着某家企业的
领域名、install 脚本里写死某个 profile 名，照样没法拿去复用。棘轮的意思是：
**只许收紧，不许放松**；真需要举例时用占位符（``<business-domain>``、
``{{BUSINESS_OVERLAY}}``），不要写真实名字。

扫描范围用 ``git ls-files --cached --others --exclude-standard``：
已跟踪 + 未跟踪但**不被 .gitignore 忽略** = 「下一次提交会把它们带进版本库的全部文件」。
所以新建目录里的文件在 commit 之前就已经被门禁覆盖，不留「先合进去再补门禁」的窗口。

二进制的处理：首 8KB 含 NUL 字节的文件（随包分发的 ``vendor/vec0.so``）按二进制跳过 ——
本门禁管的是**我们写的文本**，不是 vendored 的编译产物。

词表放在**本文件**（射程最广的这道门禁）：只有这里必须能写出这些字面量，包内那道门禁
按文件名动态加载它，两处不会漂移。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: **词表单一事实源**（大小写不敏感）。包内门禁 ``test_business_neutrality.py``
#: 按文件名加载本文件来取这份表，不在这里之外重抄一遍 —— 重抄会让两处漂移。
BANNED = ("imfl", "mff", "tms", "tmas", "djs", "R6", "R7", "飞书", "车机")

#: 允许清单 —— **三项，且每项都必须带理由**（训诫：不许用它长期开口子）。
#:
#: ① **本文件自身**：上面那份词表就定义在这里。校验别人的人必须先能写出这些字面量，
#:    否则门禁自己先红。
#: ② ``patches/*.patch``：上游源码的**镜像补丁** —— 它要与真实文件**字节一致**才能
#:    ``git apply``，其中的上下文行无法改写，不属本仓原创内容。（本仓自己新增的注释
#:    仍在射程内；这里豁免的是 ``.patch`` 文件本身，不是给它里面的内容开口子。）
#: ③ ``tools/assets/*.min.js``：第三方压缩产物（mermaid），不可读、不可改写；
#:    它的字符串常量不是本仓业务内容。源映射/未压缩源不在此豁免范围。
ALLOWLIST_NAMES = frozenset({Path(__file__).name})
ALLOWLIST_GLOBS = ("patches/*.patch", "tools/assets/*.min.js")


def _is_allowed(path: Path) -> bool:
    if path.name in ALLOWLIST_NAMES:
        return True
    try:
        rel = path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return False
    return any(Path(rel).match(glob) for glob in ALLOWLIST_GLOBS)


def _pattern(token: str) -> "re.Pattern[str]":
    """ASCII 词加标识符边界（否则会误判成某个长英文词里的一段）；中文直接子串。"""
    if all(ord(ch) < 128 for ch in token):
        return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(token) + r"(?![A-Za-z0-9_])",
                          re.IGNORECASE)
    return re.compile(re.escape(token))


PATTERNS = [(token, _pattern(token)) for token in BANNED]


#: 生成物 / 缓存目录 —— **必须显式排除**，不能只靠 `.gitignore` 和 git：
#: 门禁在「没有 .git 的导出目录」里跑时（干净室就是这么跑的：`git archive` 不带 `.git`），
#: `git ls-files` 会失败并退化为**递归遍历工作树**，于是缓存目录被当成「会进版本库的文件」——
#: 而 pytest 在收集阶段就写出了 `.pytest_cache/v/cache/nodeids`，参数化自检用例名里带词表
#: 字面量（`[imfl]` / `[R6]` …），门禁因此红在自己身上（实测：本地绿、干净室红）。
EXCLUDED_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", ".mypy_cache",
                           ".ruff_cache", "node_modules", ".venv", "venv"})


def _is_generated(path: Path) -> bool:
    """路径落在生成物/缓存目录里 = 不参与扫描（不是我们写的文本）。"""
    return any(part in EXCLUDED_DIRS or part.endswith(".egg-info") for part in path.parts)


def _committable_files():
    """下一次提交会进版本库的全部文件（相对路径）。git 不可用时退化为遍历工作树。"""
    try:
        out = subprocess.run(
            ["git", "-c", "safe.directory=*", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):  # git 不在
        out = None
    if out is None or out.returncode != 0:
        return sorted(p for p in REPO_ROOT.rglob("*")
                      if p.is_file() and not _is_generated(p))
    return [REPO_ROOT / line for line in out.stdout.splitlines() if line.strip()]


def _looks_binary(path: Path) -> bool:
    try:
        return b"\x00" in path.open("rb").read(8192)
    except OSError:
        return True


def _violations(path: Path):
    text = path.read_bytes().decode("utf-8", "ignore")
    for line_no, line in enumerate(text.splitlines(), 1):
        for token, pattern in PATTERNS:
            if pattern.search(line):
                yield token, line_no, line.strip()[:160]


def test_gate_covers_real_files():
    """自检：门禁确实扫到了一大片文件，而不是一个恒真断言。"""
    files = _committable_files()
    assert len(files) > 10, "只扫到 %d 个文件，路径枚举大概率坏了" % len(files)
    assert any(f.suffix == ".py" for f in files)
    assert any(f.suffix in (".md", ".sh") for f in files)


def test_allowlist_is_exactly_three_entries():
    """自检：允许清单只有三项（本文件 + 上游镜像补丁 + 压缩第三方资产），不得悄悄膨胀。"""
    assert ALLOWLIST_NAMES == frozenset({Path(__file__).name}), ALLOWLIST_NAMES
    assert ALLOWLIST_GLOBS == ("patches/*.patch", "tools/assets/*.min.js"), ALLOWLIST_GLOBS


def test_generated_dirs_are_excluded():
    """自检：生成物 / 缓存不参与扫描 —— 否则门禁会被 pytest 自己写的缓存弄红（干净室里实测过）。"""
    assert _is_generated(REPO_ROOT / ".pytest_cache" / "v" / "cache" / "nodeids")
    assert _is_generated(REPO_ROOT / "agentcore" / "__pycache__" / "memory_store.cpython-312.pyc")
    assert _is_generated(REPO_ROOT / "agentcore.egg-info" / "PKG-INFO")
    assert not _is_generated(REPO_ROOT / "identity" / "SOUL.generic.md")


def test_no_business_literals_anywhere_in_repo():
    found = []
    for path in _committable_files():
        if _is_generated(path) or _is_allowed(path):
            continue
        if _looks_binary(path):
            continue
        for token, line_no, text in _violations(path):
            found.append("%s:%s [%s] %s"
                         % (path.relative_to(REPO_ROOT), line_no, token, text))
    assert not found, (
        "本仓不得出现业务字面量（举例请用占位符，如 <business-domain> / "
        "{{BUSINESS_OVERLAY}}）：\n" + "\n".join(found))


@pytest.mark.parametrize("token", BANNED)
def test_gate_detects_synthetic_violation(token):
    """自检：每个词都能被真正抓到（含中英文两种匹配路径）。"""
    sample = "placeholder line with %s inside\n</business>\n" % token
    hits = list(_violations(_write_probe(sample)))
    assert any(hit[0].lower() == token.lower() for hit in hits), (
        "词表里的 %r 抓不到，门禁是假的" % token)


def _write_probe(text: str) -> Path:
    import tempfile
    fd = tempfile.NamedTemporaryFile("w", suffix=".probe.txt", delete=False,
                                     encoding="utf-8")
    with fd:
        fd.write(text)
    return Path(fd.name)