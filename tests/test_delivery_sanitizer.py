import os
import tempfile
from pathlib import Path
import pytest
from agentcore.delivery_sanitizer import sanitize_and_extract_artifacts


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

        sanitized, artifacts = sanitize_and_extract_artifacts(raw_text)

        # Path should NOT be visible
        assert str(html_path) not in sanitized
        assert str(png_path) not in sanitized

        # Placeholders should be in place
        assert "📎 [文件附件已随附上传: superagent-arch-review.html]" in sanitized
        assert "📷 [图片附件已随附上传: superagent-arch-review.png]" in sanitized

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
