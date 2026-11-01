from pathlib import Path
import importlib.util
import json
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('capture_test', ROOT / 'tools/doc_browser.py')
browser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(browser)


def test_pagination_is_contiguous_and_preserves_diagram():
    ranges = browser._page_ranges(6500, [{'top': 1800, 'bottom': 2700, 'protect': True},
                                         {'top': 4000, 'bottom': 4500, 'protect': True}], 2200)
    assert ranges[0][0] == 0 and ranges[-1][1] == 6500
    assert all(a[1] == b[0] for a, b in zip(ranges, ranges[1:]))
    assert not any(1800 < end < 2700 for _, end in ranges)
    assert all(end > start for start, end in ranges)


def test_heading_stays_with_its_following_content():
    # A heading plus the first lines below it form one protected range.
    ranges = browser._page_ranges(3000, [{'top': 2030, 'bottom': 2300, 'protect': True}], 2200)
    assert ranges[0][1] == 2030
    assert ranges[1][0] == 2030


def test_failed_browser_never_reuses_old_png(tmp_path):
    old = tmp_path / 'old.png'
    old.write_bytes(b'previous image')
    html = tmp_path / 'doc.html'
    html.write_text('<html>new</html>')
    with pytest.raises(browser.BrowserError):
        browser.capture_document(html, old, 900, 900, browser='/bin/false')
    assert old.read_bytes() == b'previous image'
    assert not old.with_suffix('.render.json').exists()


def _arch():
    if importlib.util.find_spec('markdown') is None:
        pytest.skip('python-markdown unavailable')
    spec = importlib.util.spec_from_file_location('capture_arch', ROOT / 'tools/arch_doc.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        module.which_browser()
        __import__('websocket')
    except (RuntimeError, ImportError):
        pytest.skip('browser capture dependencies unavailable')
    return module


def test_long_document_captures_bottom_and_all_pages(tmp_path):
    arch = _arch()
    source = '# Long guide\n\n> Complete delivery\n\n' + '\n\n'.join(
        '## Section ' + str(i) + '\n\n' + ('Detailed explanation with readable spacing. ' * 14)
        for i in range(20)) + '\n\nBOTTOM_SENTINEL\n'
    body, toc = arch.md_to_html_with_toc(source)
    html, png = tmp_path / 'long.html', tmp_path / 'long.png'
    arch.render(html, 'Long guide', body, footer='BOTTOM_SENTINEL')
    manifest = arch.screenshot(html, png, 1000, 900)
    assert manifest['complete'] is True
    assert manifest['height'] > 4200
    assert manifest['pages'][-1]['bottom'] >= manifest['footerBottom']
    assert len(manifest['pages']) > 2
    assert sum(p['bottom'] - p['top'] for p in manifest['pages']) == manifest['height']
    assert all((tmp_path / page['file']).stat().st_size > 5000 for page in manifest['pages'])
    assert png.with_suffix('.render.json').exists()


def test_invalid_diagram_is_not_delivered(tmp_path):
    arch = _arch()
    body = arch.md_to_html('# Bad\n\n```mermaid\nthis is not valid mermaid syntax !\n```\n')
    html, png = tmp_path / 'bad.html', tmp_path / 'bad.png'
    arch.render(html, 'Bad', body)
    with pytest.raises(RuntimeError, match='render failed'):
        arch.screenshot(html, png, 900, 900)
    assert not png.exists()


def test_png_only_uses_latest_source_despite_existing_html(tmp_path, monkeypatch):
    arch = _arch()
    md = tmp_path / 'fresh.md'
    md.write_text('# Current source\n\nThis is the new content.\n')
    html = tmp_path / 'fresh.html'
    html.write_text('<html>STALE FILE WITH NO RENDER ENGINE</html>')
    monkeypatch.setattr(sys, 'argv', ['arch_doc.py', str(md), '--png-only'])
    arch.main()
    metadata = json.loads((tmp_path / 'fresh.render.json').read_text())
    assert metadata['source_html'] is None
    assert metadata['source_markdown_sha256'] == __import__('hashlib').sha256(md.read_bytes()).hexdigest()
    assert (tmp_path / 'fresh.png').stat().st_size > 5000
    assert 'STALE FILE' in html.read_text()


def test_real_mermaid_code_element_residue_fails_verification(tmp_path):
    arch = _arch()
    body = arch.md_to_html('# Valid diagram\n\n```mermaid\nflowchart LR\nA --> B\n```\n')
    body += '<pre><code class="language-mermaid">graph TD</code></pre>'
    html = tmp_path / 'residue.html'
    arch.render(html, 'Residue', body)
    with pytest.raises(browser.BrowserError, match='Unrendered Mermaid code'):
        browser.inspect_document(html, expected_diagrams=1)


def test_literal_marker_text_is_not_a_mermaid_code_element(tmp_path):
    arch = _arch()
    body = arch.md_to_html('# Marker explanation\n\nThe marker is `class="language-mermaid"`; it is ordinary reference text.\n')
    html = tmp_path / 'reference.html'
    arch.render(html, 'Reference', body)
    metrics = browser.inspect_document(html, expected_diagrams=0)
    assert metrics['residualDiagramCode'] == 0


def test_browser_prefers_cached_chromium_before_snap(tmp_path, monkeypatch):
    import importlib.util
    import os
    from pathlib import Path
    module_path = Path(__file__).resolve().parents[1] / "tools/doc_browser.py"
    spec = importlib.util.spec_from_file_location("browser_discovery_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    binary = tmp_path / "chromium-1234/chrome-linux64/chrome"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.setattr(module, "__file__", str(tmp_path / "engine/tools/doc_browser.py"))
    monkeypatch.delenv("ARCH_DOC_BROWSER", raising=False)
    assert module.find_browser() == str(binary)
