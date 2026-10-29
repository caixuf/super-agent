"""记忆引擎端到端行为测试。

覆盖验收标准：
- 写入 -> 后台汇合 -> 召回，并断言读回的 statement 内容一致；
- 全新目录免初始化直接用；
- 短命子进程写库后退出，父进程仍能召回（非 daemon + atexit 汇合）；
- 非默认 data_dir / default_user 下读写身份解析一致；
- 代际桶硬隔离；
- 空库召回返回 ``empty_reason='no_match'``，不与「索引坏了」混淆；
- sqlite-vec 扩展按包相对路径可找到，无扩展环境 skip。

示例文本一律用**中性样例**（阈值 / 扭矩 / 子系统这类通用工程词），不写任何具体行业对象。
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap


def test_roundtrip(engine):
    """写 3 条 -> flush -> 逐条召回，断言读回内容与写入完全一致。"""
    facts = [
        ("阈值门限超过 3.0% 时退出自动模式", "G1", "阈值门限"),
        ("扭矩阈值超过 4.0Nm 时降级输出", "G1", "扭矩阈值"),
        ("子系统失效后触发接管请求", "G1", "子系统失效"),
    ]
    written = []
    for statement, ebm, subject in facts:
        out = engine.write_fact(statement, ebm=ebm, subject=subject)
        assert out["ok"] is True and out["written"] is True
        written.append(statement)

    # 异步向量写入必须在断言前汇合，否则索引可能没落盘。
    assert engine.flush_background(timeout=30) == 0

    for statement, ebm, query in facts:
        result = engine.recall_facts(query, limit=3, ebm=ebm)
        assert result["ok"] is True
        statements = [r["statement"] for r in result["results"]]
        assert statement in statements, (
            "召回未读回原 statement；query=%r 实得=%r" % (query, statements))

    # 全部 3 条都应能被某次查询读到（不是只回了一条）
    seen = set()
    for _, ebm, query in facts:
        seen.update(r["statement"] for r in engine.recall_facts(query, limit=3, ebm=ebm)["results"])
    assert set(written) <= seen


def test_fresh_dir(tmp_path, configured):
    """不预先创建任何目录/表，直接写入 + 召回成功。"""
    data_dir = tmp_path / "brand_new_dir"
    assert not data_dir.exists()

    with configured(data_dir) as ms:
        assert not data_dir.exists()
        out = ms.write_fact("全新目录写入的事实：阈值门限 3.0%", ebm="G1",
                            subject="阈值门限")
        assert out["ok"] is True
        assert ms.flush_background(timeout=30) == 0
        result = ms.recall_facts("阈值门限", limit=3, ebm="G1")
        assert "全新目录写入的事实：阈值门限 3.0%" in [
            r["statement"] for r in result["results"]]
    # 初始化已下沉到连接工厂：目录与表在第一次使用时就建好了
    assert data_dir.exists()
    assert (data_dir / "coding_memory_cv.db").exists()


CHILD_WRITER = textwrap.dedent(
    '''
    import hashlib, math, os, re, sys
    from pathlib import Path

    sys.path.insert(0, os.environ["AGENTCORE_TEST_REPO"])
    from agentcore.memory.config import MemoryConfig, configure

    def fake_embed(texts, dim=64):
        out = []
        for t in texts:
            v = [0.0] * dim
            s = (t or "").lower()
            grams = list(re.findall(r"[a-z0-9_]+", s))
            for run in re.findall(r"[\\u4e00-\\u9fff]+", s):
                grams += [run[i] for i in range(len(run))]
                grams += [run[i:i + 2] for i in range(len(run) - 1)]
            for g in grams:
                h = hashlib.sha256(g.encode("utf-8")).digest()
                v[int.from_bytes(h[:8], "big") % dim] += 1.0 if h[8] & 1 else -1.0
            n = math.sqrt(sum(x * x for x in v))
            out.append([x / n for x in v] if n else v)
        return out

    configure(MemoryConfig(
        data_dir=Path(os.environ["AGENTCORE_TEST_DIR"]),
        embed_dim=64,
        embed_fn=lambda texts: fake_embed(texts, 64),
        era_domains=("automotive",),
        # 代际写法也是宿主约定；测试用 "G1"/"G2" 这组中性写法
        era_pattern=r"\\bG(\\d+)(?:\\.\\d+)?\\b",
        era_prefix="G",
    ))
    from agentcore.memory import memory_store as ms
    ms.write_fact("短命进程写入：阈值门限 3.0% 时退出", ebm="G1",
                  subject="阈值门限")
    # 刻意不调用 flush_background：验证 atexit 汇合在进程退出时兜底
    '''
)


def test_short_lived_process(tmp_path, configured, repo_root):
    """子进程写库后立即退出，父进程打开同一库仍能召回（没有丢索引）。"""
    data_dir = tmp_path / "subproc_dir"
    data_dir.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "child_writer.py"
    script.write_text(CHILD_WRITER, encoding="utf-8")

    env = dict(os.environ)
    env["AGENTCORE_TEST_REPO"] = str(repo_root)
    env["AGENTCORE_TEST_DIR"] = str(data_dir)
    proc = subprocess.run(
        [sys.executable, str(script)], env=env, cwd=str(repo_root),
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, "子进程写入失败:\n%s\n%s" % (proc.stdout, proc.stderr)

    with configured(data_dir) as ms:
        result = ms.recall_facts("阈值门限", limit=3, ebm="G1")
        assert "短命进程写入：阈值门限 3.0% 时退出" in [
            r["statement"] for r in result["results"]], (
            "父进程未能召回子进程写入的事实，说明退出时丢了向量索引")


def test_config_injection(tmp_path, configured):
    """非默认 data_dir + default_user：写入用同一 config 能召回；换身份读不到。"""
    data_dir = tmp_path / "injected_dir"

    with configured(data_dir, default_user="alice") as ms:
        assert ms.get_config().default_user == "alice"
        out = ms.write_fact("身份注入事实：阈值门限 3.0%", ebm="G1",
                            subject="阈值门限")
        assert out["ok"] is True
        assert ms.flush_background(timeout=30) == 0
        # 不显式传 user_id：入口应从当前 config 解析出 alice
        result = ms.recall_facts("阈值门限", limit=3, ebm="G1")
        assert "身份注入事实：阈值门限 3.0%" in [
            r["statement"] for r in result["results"]]

    # 同一个库、换一个 default_user：读不到 alice 的记录（证明读写两侧身份一致解析）
    with configured(data_dir, default_user="bob") as ms:
        result = ms.recall_facts("阈值门限", limit=3, ebm="G1")
        assert result["results"] == []
        assert result["empty_reason"] == "no_match"


def test_era_isolation(engine):
    """G1/G2 两条不同门限事实，按 ebm='G1' 召回只回 G1 那条。"""
    engine.write_fact("G1 阈值门限 3.0%", ebm="G1", subject="阈值门限")
    engine.write_fact("G2 阈值门限 5.0%", ebm="G2", subject="阈值门限")
    assert engine.flush_background(timeout=30) == 0

    result = engine.recall_facts("阈值门限", limit=3, ebm="G1")
    assert result["ok"] is True
    assert result["results"], "G1 桶应至少召回一条"
    for hit in result["results"]:
        assert hit["workspace_id"] == "G1", hit
        assert "G2" not in hit["statement"], hit
    assert "G2 阈值门限 5.0%" not in [r["statement"] for r in result["results"]]


def test_empty_reason_distinguishes(tmp_path, configured):
    """空库返回 no_match；有数据但查询无关时给出不同原因（区分「没数据」与「没命中」）。"""
    with configured(tmp_path / "empty_dir") as ms:
        result = ms.recall_facts("任意查询", limit=3)
        assert result["ok"] is True
        assert result["results"] == []
        assert result["empty_reason"] == "no_match"

        ms.write_fact("阈值门限超过 3.0% 时退出自动模式", ebm="G1",
                      subject="阈值门限")
        assert ms.flush_background(timeout=30) == 0

        other = ms.recall_facts("完全不相关的词汇zzz", limit=3)
        assert other["ok"] is True
        assert other["results"] == []
        # 关键：库里明明有数据，不能再报 no_match
        assert other["empty_reason"] != "no_match"


def test_package_exports():
    """骨架要求：包顶层与 memory 子包导出关键 API。"""
    import agentcore
    from agentcore import (
        MemoryConfig,
        configure,
        flush_background,
        recall_facts,
        reset_config,
        write_fact,
    )

    for obj in (MemoryConfig, configure, reset_config, write_fact, recall_facts,
                flush_background):
        assert callable(obj) or isinstance(obj, type)
    assert agentcore.__version__
    assert agentcore.memory.MemoryConfig is MemoryConfig


def test_vec_extension_is_packaged():
    """sqlite-vec 扩展必须按包相对路径找到，不依赖 CWD。"""
    from agentcore.memory import vec_index

    ext = vec_index.find_extension()
    assert ext is not None, "agentcore/memory/vendor/vec0.so 未随包分发"
    assert ext.parent.name == "vendor"
    assert ext.name.startswith("vec0")


def test_vec_index_roundtrip(tmp_path, configured):
    """扩展可用时，写入后 vec0 索引应有数据；不可用则 skip 而不是 fail。"""
    from agentcore.memory import vec_index

    if vec_index.find_extension() is None:
        import pytest
        pytest.skip("sqlite-vec 扩展不可用，向量索引层跳过")

    with configured(tmp_path / "vec_dir") as ms:
        out = ms.write_fact("向量索引验证：扭矩阈值 4.0Nm", ebm="G1",
                            subject="扭矩阈值")
        assert out["ok"] is True
        assert ms.flush_background(timeout=30) == 0

        conn = ms._connect()
        try:
            stats = vec_index.stats(conn)
        finally:
            conn.close()
        if not stats.get("loaded"):
            import pytest
            pytest.skip("sqlite-vec 扩展加载失败（环境不支持 load_extension）")
        assert stats["rows"] >= 1