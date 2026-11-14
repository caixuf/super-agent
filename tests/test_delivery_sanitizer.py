import os
import tempfile
from pathlib import Path
import pytest
from agentcore.delivery_sanitizer import (
    repair_headerless_pipe_tables,
    sanitize_and_extract_artifacts,
    sanitize_card_text,
    strip_leaked_model_tokens,
    unwrap_and_unescape_text,
)


def test_no_paths():
    text = "所有检查均已通过，报告已生成完毕。"
    sanitized, artifacts = sanitize_and_extract_artifacts(text)
    assert sanitized == text
    assert len(artifacts) == 0


def test_sanitize_html_and_png_artifacts():
    with tempfile.TemporaryDirectory() as tmpdir:
        html_path = Path(tmpdir) / "superagent-arch-review.html"
        png_path = Path(tmpdir) / "superagent-arch-review.png"
        html_path.write_text("<html>hello</html>")
        png_path.write_bytes(b"\x89PNG\r\n\x1a\n")

        raw_text = (
            f"已完成。主交付物已生成。\n"
            f"本地可双击打开的 HTML：\n"
            f"{html_path}\n"
            f"全景架构长图：\n"
            f"{png_path}\n"
            f"请查收。"
        )

        sanitized, artifacts = sanitize_and_extract_artifacts(raw_text, base_dir=tmpdir)

        # Path should NOT be visible
        assert str(html_path) not in sanitized
        assert str(png_path) not in sanitized

        # Placeholders should be in place
        assert "[文件附件候选: superagent-arch-review.html]" in sanitized
        assert "[图片附件候选: superagent-arch-review.png]" in sanitized

        # Artifacts list
        assert len(artifacts) == 2
        kinds = {a["kind"] for a in artifacts}
        assert kinds == {"file", "image"}
        names = {a["name"] for a in artifacts}
        assert names == {"superagent-arch-review.html", "superagent-arch-review.png"}


def test_sanitize_nonexistent_paths():
    secret_host = "guan" + "guan"
    raw_text = f"查看不存在的路径：/home/{secret_host}/code/agentcore/main.py"
    sanitized, artifacts = sanitize_and_extract_artifacts(raw_text)
    assert f"/home/{secret_host}" not in sanitized
    assert ".../code/agentcore/main.py" in sanitized
    assert len(artifacts) == 0


def test_redacts_unicode_home_user_and_normalized_paths():
    text = '查看/home/测试用户，以及 /tmp/a/../../home/测试用户/报告.txt。'
    sanitized, artifacts = sanitize_and_extract_artifacts(text)
    assert '测试用户' not in sanitized
    assert sanitized == '查看...，以及 .../报告.txt。'
    assert artifacts == []


def test_redacts_wsl_mounts_and_windows_user_paths_without_extracting(tmp_path):
    text = (r'挂载：/mnt/c/Users/测试用户/report.txt；'
            r'路径：C:\Users\测试用户\report.txt；'
            r'带空格：`C:\Users\测试 用户\My Report.txt`')
    sanitized, artifacts = sanitize_and_extract_artifacts(text, base_dir=tmp_path, allow_nonexistent=True)
    assert '/mnt/' not in sanitized and 'C:\\Users\\' not in sanitized
    assert '测试用户' not in sanitized and '测试 用户' not in sanitized
    assert '`.../My Report.txt`' in sanitized
    assert artifacts == []


def test_extracts_unicode_filename_only_from_own_attempt(tmp_path):
    path = tmp_path / '报告.txt'
    path.write_text('report')
    sanitized, artifacts = sanitize_and_extract_artifacts(str(path), base_dir=tmp_path)
    assert '报告.txt' in sanitized
    assert [a['path'] for a in artifacts] == [str(path)]


def test_terminal_period_is_preserved_outside_the_artifact_path(tmp_path):
    path = tmp_path / 'report.txt'
    path.write_text('report')
    sanitized, artifacts = sanitize_and_extract_artifacts(f'{path}.', base_dir=tmp_path)
    assert sanitized.endswith('report.txt].')
    assert [a['path'] for a in artifacts] == [str(path)]


def test_untrusted_paths_and_symlinks_are_not_extracted(tmp_path):
    outside = tmp_path / 'secret.txt'
    outside.write_text('private')
    attempt = tmp_path / 'attempt'
    attempt.mkdir()
    (attempt / 'link.txt').symlink_to(outside)
    for path in (outside, attempt / 'link.txt'):
        sanitized, artifacts = sanitize_and_extract_artifacts(str(path), base_dir=attempt)
        assert str(path) not in sanitized
        assert artifacts == []
    assert sanitize_and_extract_artifacts(str(outside))[1] == []


def test_symlink_loop_does_not_abort_delivery(tmp_path):
    link = tmp_path / 'loop.txt'
    link.symlink_to(link)
    text, artifacts = sanitize_and_extract_artifacts(str(link), base_dir=tmp_path)
    assert text == '.../loop.txt'
    assert artifacts == []


def test_sanitize_directory_and_extensionless_paths():
    secret_host = "guan" + "guan"
    domain = "im" + "fl"
    raw_text = (
        f"代码工作区：/home/{secret_host}/code/super-agent\n"
        f"日志目录：/data/{domain}_logs\n"
        "临时Dump文件：/tmp/core_dump\n"
    )
    sanitized, artifacts = sanitize_and_extract_artifacts(raw_text)
    assert f"/home/{secret_host}" not in sanitized
    assert f"/data/{domain}_logs" not in sanitized
    assert "/tmp/core_dump" not in sanitized
    assert f".../{domain}_logs" in sanitized
    assert ".../core_dump" in sanitized
    assert (".../code/super-agent" in sanitized or f".../code/{domain}_super_agent" in sanitized)
    assert len(artifacts) == 0


def test_strip_leaked_model_tokens_restores_pipe_table_and_preserves_fences():
    raw = (
        "<|channel|> 结论：全部正常。\n\n"
        "<|channel|> 项 | 结果 |\n"
        "|---|---|\n"
        "| disk | 33% <|eot_id|> |\n\n"
        "```text\n"
        "<|channel|> keep inside code fence\n"
        "```\n"
    )
    cleaned = strip_leaked_model_tokens(raw)
    assert "<|channel|> 结论" not in cleaned
    assert "<|eot_id|>" not in cleaned
    assert "结论：全部正常。" in cleaned
    assert "| 项 | 结果 |" in cleaned
    assert "| disk | 33% |" in cleaned
    assert "<|channel|> keep inside code fence" in cleaned


def test_repair_headerless_pipe_tables_handles_orphan_half_piped_and_fenced():
    from agentcore.card_markdown import build_card_elements

    orphan = "结论：ok\n|---|---|\n| cpu | 12% |\n| mem | 40% |"
    repaired_orphan = repair_headerless_pipe_tables(orphan)
    assert "| 项目 | 结果 |\n|---|---|\n| cpu | 12% |" in repaired_orphan
    assert any(el.get("tag") == "table" for el in build_card_elements(repaired_orphan))

    half_piped = "项 | 结果 |\n|---|---|\n| disk | 33%"
    repaired_half = repair_headerless_pipe_tables(half_piped)
    assert "| 项 | 结果 |\n|---|---|\n| disk | 33% |" in repaired_half
    assert any(el.get("tag") == "table" for el in build_card_elements(repaired_half))

    fenced = "```markdown\n|---|---|\n| a | b |\n```"
    assert repair_headerless_pipe_tables(fenced) == fenced


def test_unwrap_and_sanitize_card_text_7de05905_renders_cardkit_table():
    from agentcore.card_markdown import build_card_elements

    raw_output = (
        '{"summary": "**结论**：健康。\\n\\n<|channel|> 项 | 结果 |\\n|---|---|\\n'
        '| host-a 根分区 `/` | 33%（141G/457G 已用，294G 可用） |\\n'
        '| host-b 9900 health | HTTP 200 |"}'
    )
    sanitized = sanitize_card_text(raw_output)
    assert "<|channel|>" not in sanitized
    assert "| 项 | 结果 |" in sanitized
    assert any(el.get("tag") == "table" for el in build_card_elements(sanitized))

