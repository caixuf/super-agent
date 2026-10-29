#!/usr/bin/env bash
# Exercise real Mermaid + browser verification; no stubbed SVG or attributes.
set -euo pipefail
doc_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
doc_engine_args=()
if [ "${1:-}" = '--engine-root' ]; then
  [ "$#" -eq 2 ] || { echo 'Usage: selftest_verify_render.sh [--engine-root ROOT]' >&2; exit 2; }
  doc_engine_args=(--engine-root "$2")
elif [ "$#" -ne 0 ]; then
  echo 'Usage: selftest_verify_render.sh [--engine-root ROOT]' >&2; exit 2
fi
doc_engine="$(python3 "$doc_here/resolve_engine.py" "${doc_engine_args[@]}")"
doc_scratch="$(mktemp -d "${TMPDIR:-/tmp}/doc-render-selftest.XXXXXX")"
trap 'rm -rf -- "$doc_scratch"' EXIT
python3 - "$doc_engine" "$doc_scratch" <<'PY'
import importlib.util
from pathlib import Path
import sys

engine, output = Path(sys.argv[1]), Path(sys.argv[2])
spec = importlib.util.spec_from_file_location('selftest_arch_doc', engine / 'tools' / 'arch_doc.py')
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)
source = '# Real render gate\n\n> Local diagrams must really render.\n\n## Data flow\n\n```mermaid\nflowchart LR\n  A[Input] --> B[Checked output]\n```\n'
body, toc = renderer.md_to_html_with_toc(source)
renderer.render(output / 'good.html', 'Real diagram', body, toc=toc)
renderer.render(output / 'bad.html', 'Real residue', body + '<pre><code class="language-mermaid">graph TD</code></pre>', toc=toc)
renderer.render(output / 'selfref.html', 'Literal reference', body + '<p>The literal <code>class="language-mermaid"</code> is reference text.</p>', toc=toc)
PY
doc_failures=0
doc_check() {
  local doc_file="$1" doc_wanted="$2" doc_expected="$3" doc_label="$4" doc_rc=0
  bash "$doc_here/verify_render.sh" "$doc_scratch/$doc_file" "$doc_wanted" \
    --engine-root "$doc_engine" >"$doc_scratch/$doc_label.log" 2>&1 || doc_rc=$?
  if [ "$doc_rc" -eq "$doc_expected" ]; then
    printf 'PASS: %s returned %s as expected\n' "$doc_label" "$doc_rc"
  else
    printf 'FAIL: %s returned %s, expected %s\n' "$doc_label" "$doc_rc" "$doc_expected" >&2
    cat "$doc_scratch/$doc_label.log" >&2
    doc_failures=$((doc_failures + 1))
  fi
}
doc_check good.html 1 0 real-diagram
doc_check bad.html 1 1 residual-code
doc_check selfref.html 1 0 literal-reference
doc_check good.html 2 1 wrong-count
if [ "$doc_failures" -ne 0 ]; then
  echo "SELFTEST FAIL: $doc_failures of 4 expectations failed" >&2
  exit 1
fi
echo 'SELFTEST PASS: 4/4 expectations, with real Mermaid and browser checks'
