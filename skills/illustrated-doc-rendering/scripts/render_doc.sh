#!/usr/bin/env bash
# Keep the renderer CLI intact; consume only this wrapper's engine override.
set -euo pipefail
doc_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
doc_engine_args=()
doc_render_args=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --engine-root)
      [ "$#" -ge 2 ] || { echo 'ERROR: --engine-root requires a path' >&2; exit 2; }
      doc_engine_args=(--engine-root "$2"); shift 2 ;;
    --)
      shift; doc_render_args+=("$@"); break ;;
    *) doc_render_args+=("$1"); shift ;;
  esac
done
doc_engine="$(python3 "$doc_here/resolve_engine.py" "${doc_engine_args[@]}")"
if [ -z "${PLAYWRIGHT_BROWSERS_PATH:-}" ] && [ -d "$doc_engine/.browser-cache" ]; then
  export PLAYWRIGHT_BROWSERS_PATH="$doc_engine/.browser-cache"
fi
doc_python="$doc_engine/.superagent-venv/bin/python"
[ -x "$doc_python" ] || doc_python=python3
exec "$doc_python" "$doc_engine/tools/arch_doc.py" "${doc_render_args[@]}"
