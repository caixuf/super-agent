"""Local Chromium document capture with explicit render readiness and full coverage.

websocket-client is optional until a browser capture is requested. No browser
profile or live user tab is used; network requests are blocked during capture.
"""
from __future__ import annotations

import base64
import argparse
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import signal
import tempfile
import time


class BrowserError(RuntimeError):
    pass


def find_browser(candidates=()):
    configured = os.environ.get('ARCH_DOC_BROWSER')
    names = ([configured] if configured else []) + list(candidates) + [
        str(Path.home() / '.local/bin/chromium'), str(Path.home() / '.local/bin/google-chrome'),
        'chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable',
    ]
    for name in names:
        found = shutil.which(name)
        if found and os.access(found, os.X_OK):
            return found
    raise BrowserError('No local Chromium browser; set ARCH_DOC_BROWSER to its executable')


class Browser:
    def __init__(self, executable, width, height, timeout=60):
        self.executable, self.width, self.height, self.timeout = executable, width, height, timeout
        self._id = 0
        self.ws = None
        self.proc = None

    def __enter__(self):
        try:
            import websocket
        except ImportError as exc:
            raise BrowserError('Browser capture requires websocket-client') from exc
        self.tmp = tempfile.TemporaryDirectory(prefix='arch-doc-browser-')
        self.log = tempfile.TemporaryFile()
        command = [self.executable, '--headless=new', '--disable-gpu', '--no-sandbox',
                   '--disable-dev-shm-usage', '--hide-scrollbars', '--no-first-run',
                   '--no-default-browser-check', '--remote-allow-origins=*',
                   '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
                   '--user-data-dir=' + self.tmp.name, 'about:blank']
        self.proc = subprocess.Popen(command, stdout=self.log, stderr=self.log, start_new_session=True)
        try:
            port_file = Path(self.tmp.name) / 'DevToolsActivePort'
            deadline = time.monotonic() + self.timeout
            while not port_file.exists():
                if self.proc.poll() is not None:
                    raise BrowserError('Chromium exited before DevTools became ready')
                if time.monotonic() > deadline:
                    raise BrowserError('Chromium startup timed out')
                time.sleep(.03)
            port, endpoint = port_file.read_text().splitlines()[:2]
            self.ws = websocket.create_connection('ws://127.0.0.1:' + port + endpoint,
                                                   timeout=self.timeout, suppress_origin=True)
            target = self.call('Target.createTarget', {'url': 'about:blank'})['targetId']
            self.session = self.call('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
            self.call('Page.enable', {}, self.session)
            self.call('Runtime.enable', {}, self.session)
            self.call('Network.enable', {}, self.session)
            self.call('Network.setBlockedURLs', {'urls': ['http://*', 'https://*']}, self.session)
            self.call('Emulation.setDeviceMetricsOverride', {'width': self.width, 'height': self.height,
                      'deviceScaleFactor': 1.25, 'mobile': False}, self.session)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def call(self, method, params=None, session=None):
        self._id += 1
        message = {'id': self._id, 'method': method, 'params': params or {}}
        if session:
            message['sessionId'] = session
        self.ws.send(json.dumps(message))
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            reply = json.loads(self.ws.recv())
            if reply.get('id') == self._id:
                if 'error' in reply:
                    raise BrowserError('Chromium protocol failure: ' + str(reply['error'].get('message', 'unknown')))
                return reply.get('result', {})
        raise BrowserError('Chromium command timed out')

    def evaluate(self, expression, await_promise=False):
        result = self.call('Runtime.evaluate', {'expression': expression, 'awaitPromise': await_promise,
                           'returnByValue': True}, self.session)
        if result.get('exceptionDetails'):
            raise BrowserError('Document script failed during layout inspection')
        return result.get('result', {}).get('value')

    def open(self, path):
        result = self.call('Page.navigate', {'url': Path(path).resolve().as_uri()}, self.session)
        if result.get('errorText'):
            raise BrowserError('Document navigation failed')
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            value = self.evaluate('document.documentElement && ({state:document.documentElement.dataset.renderState,error:document.documentElement.dataset.renderError})')
            if value and value.get('state') == 'error':
                raise BrowserError('Document render failed: ' + str(value.get('error') or 'diagram error')[:240])
            if value and value.get('state') == 'ready':
                return
            time.sleep(.03)
        raise BrowserError('Document render readiness timed out')

    def __exit__(self, *_):
        if self.ws:
            try:
                self.call('Browser.close')
            except Exception:
                pass
            self.ws.close()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if self.proc:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if hasattr(self, 'log'):
            self.log.close()
        if hasattr(self, 'tmp'):
            for attempt in range(3):
                try:
                    self.tmp.cleanup()
                    break
                except OSError:
                    if attempt == 2:
                        raise BrowserError('Browser profile cleanup failed') from None
                    time.sleep(.1)


def _page_ranges(height, blocks, page_height):
    """Contiguous full coverage, preferring whitespace around content blocks."""
    points = sorted({0, height} | {max(0, min(height, int(b[k])))
                                  for b in blocks for k in ('top', 'bottom')})
    protected = [(b['top'], b['bottom']) for b in blocks if b.get('protect')]
    result, start = [], 0
    while start < height:
        target = min(height, start + page_height)
        if target == height:
            end = height
        else:
            options = [p for p in points if start + page_height * .55 <= p <= target
                       and not any(a + 2 < p < z - 2 for a, z in protected)]
            end = max(options) if options else target
            for a, z in protected:
                if a + 2 < end < z - 2:
                    end = int(a) if a > start + 120 else min(height, int(math.ceil(z)))
                    break
        if end <= start:
            end = min(height, start + page_height)
        result.append((start, end))
        start = end
    return result


def _document_metrics(client, width):
    metrics = client.evaluate('''(() => {
      const doc=document.querySelector('.doc');
      const rects=[...document.querySelectorAll('.doc > h2,.doc > h3,.doc > p,.doc > ul,.doc > ol,.doc > blockquote,.doc > pre,.doc > .codehilite,.doc > table,.doc > .mermaid,.cover,.toc,.footer')].map(e=>{
        const r=e.getBoundingClientRect(),n=e.nextElementSibling?.getBoundingClientRect();
        const bottom=e.matches('h2,h3')&&n?Math.max(r.bottom,Math.min(n.bottom,n.top+100)):r.bottom;
        return {top:Math.max(0,Math.floor(r.top+scrollY-10)),bottom:Math.ceil(bottom+scrollY+10),protect:e.matches('h2,h3,table,pre,.codehilite,.mermaid,.cover')};});
      const labels=[...document.querySelectorAll('.mermaid svg text,.mermaid .nodeLabel')].map(e=>e.getBoundingClientRect().height).filter(n=>n>0);
      return {height:Math.ceil(doc.getBoundingClientRect().bottom+scrollY+parseFloat(getComputedStyle(document.body).paddingBottom)),
        width:document.documentElement.clientWidth,scrollWidth:document.documentElement.scrollWidth,
        diagrams:document.querySelectorAll('.mermaid').length,
        rendered:document.querySelectorAll('.mermaid > svg').length,
        residualDiagramCode:document.querySelectorAll('code.language-mermaid,pre.language-mermaid,div.language-mermaid,span.language-mermaid').length,
        footerBottom:Math.ceil(document.querySelector('.footer').getBoundingClientRect().bottom+scrollY),
        minimumDiagramLabelHeight:labels.length?Math.min(...labels):null,
        errors:document.querySelectorAll('.error-icon,.error-text').length,blocks:rects};
    })()''')
    if not metrics or metrics['height'] > 100000:
        raise BrowserError('Document exceeds capture height budget')
    if metrics['diagrams'] != metrics['rendered'] or metrics['errors']:
        raise BrowserError('Not all diagrams rendered successfully')
    if metrics['residualDiagramCode']:
        raise BrowserError('Unrendered Mermaid code remains in document elements')
    if metrics['scrollWidth'] > width + 2:
        raise BrowserError('Document overflows horizontally; simplify wide content')
    metrics['warnings'] = []
    label_size = metrics['minimumDiagramLabelHeight']
    if label_size is not None and label_size < 9:
        raise BrowserError('Diagram labels are too small; split the diagram into readable views')
    if label_size is not None and label_size < 12:
        metrics['warnings'].append('Diagram label height below 12 CSS pixels; visual review required')
    return metrics


def inspect_document(html_path, width=1100, expected_diagrams=None, *, browser=None, timeout=60):
    if expected_diagrams is not None and expected_diagrams < 0:
        raise ValueError('expected_diagrams must be nonnegative')
    with Browser(browser or find_browser(), width, 900, timeout) as client:
        client.open(html_path)
        metrics = _document_metrics(client, width)
    if expected_diagrams is not None and metrics['rendered'] != expected_diagrams:
        raise BrowserError('Diagram count differs from the expected source count')
    metrics.pop('blocks', None)
    metrics['complete'] = True
    return metrics


def capture_document(html_path, png_path, width, viewport_height, *, browser=None,
                     page_height=2200, timeout=60):
    if not 480 <= width <= 2400 or not 240 <= viewport_height <= 12000:
        raise BrowserError('Capture dimensions outside supported bounds')
    png_path = Path(png_path).resolve()
    png_path.parent.mkdir(parents=True, exist_ok=True)
    staged = []
    try:
        with Browser(browser or find_browser(), width, min(viewport_height, 2400), timeout) as client:
            client.open(html_path)
            metrics = _document_metrics(client, width)
            ranges = _page_ranges(metrics['height'], metrics['blocks'], page_height)
            pages = []
            for index, (top, bottom) in enumerate(ranges):
                target = png_path if index == 0 else png_path.with_name(png_path.stem + '.page-' + str(index + 1).zfill(2) + '.png')
                result = client.call('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': True,
                    'clip': {'x': 0, 'y': top, 'width': width, 'height': bottom - top, 'scale': 1}}, client.session)
                data = base64.b64decode(result['data'])
                fd, name = tempfile.mkstemp(prefix='.' + target.stem + '.', suffix='.png', dir=target.parent)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(data)
                from PIL import Image
                with Image.open(name) as image:
                    dimensions = image.size
                    image.verify()
                if abs(dimensions[0] - width * 1.25) > 2 or abs(dimensions[1] - (bottom - top) * 1.25) > 2:
                    raise BrowserError('Chromium returned an incomplete screenshot')
                staged.append((Path(name), target))
                pages.append({'file': target.name, 'top': top, 'bottom': bottom,
                              'pixel_width': dimensions[0], 'pixel_height': dimensions[1]})
            manifest = dict(metrics)
            manifest.pop('blocks', None)
            manifest.update({'schema': 1, 'complete': True, 'pages': pages,
                             'source_html': Path(html_path).name})
            fd, name = tempfile.mkstemp(prefix='.' + png_path.stem + '.', suffix='.json', dir=png_path.parent)
            with os.fdopen(fd, 'w') as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2)
            manifest_path = png_path.with_suffix('.render.json')
            staged.append((Path(name), manifest_path))
            for temporary, target in staged:
                os.replace(temporary, target)
            return manifest
    finally:
        for temporary, _ in staged:
            if temporary.exists():
                temporary.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify', required=True, metavar='HTML')
    parser.add_argument('--expected', type=int)
    parser.add_argument('--width', type=int, default=1100)
    args = parser.parse_args()
    try:
        print(json.dumps(inspect_document(args.verify, args.width, args.expected), ensure_ascii=False))
    except (BrowserError, ValueError) as error:
        parser.exit(1, 'FAIL: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
