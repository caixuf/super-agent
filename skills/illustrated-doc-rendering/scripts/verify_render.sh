#!/usr/bin/env bash
# Verify actual local browser rendering, count, readiness and readability.
set -euo pipefail
doc_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
doc_engine_args=()
doc_positionals=()
doc_width=1100
while [ "$#" -gt 0 ]; do
  case "$1" in
    --engine-root)
      [ "$#" -ge 2 ] || { echo 'ERROR: --engine-root requires a path' >&2; exit 2; }
      doc_engine_args=(--engine-root "$2"); shift 2 ;;
    --width)
      [ "$#" -ge 2 ] || { echo 'ERROR: --width requires a value' >&2; exit 2; }
      doc_width="$2"; shift 2 ;;
    -h|--help)
      echo 'Usage: verify_render.sh HTML EXPECTED [--engine-root ROOT] [--width 1100]'; exit 0 ;;
    --*) echo "ERROR: unsupported option $1" >&2; exit 2 ;;
    *) doc_positionals+=("$1"); shift ;;
  esac
done
[ "${#doc_positionals[@]}" -eq 2 ] || {
  echo 'Usage: verify_render.sh HTML EXPECTED [--engine-root ROOT] [--width 1100]' >&2; exit 2;
}
doc_engine="$(python3 "$doc_here/resolve_engine.py" "${doc_engine_args[@]}")"
exec python3 "$doc_engine/tools/doc_browser.py" --verify "${doc_positionals[0]}" \
  --expected "${doc_positionals[1]}" --width "$doc_width"
