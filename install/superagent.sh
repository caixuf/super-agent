#!/usr/bin/env bash
# Super Agent one-command installer.
#
#   ./install/superagent.sh install [--profile NAME] [--apply]
#   ./install/superagent.sh update  [--profile NAME] [--apply]
#   ./install/superagent.sh doctor  [--profile NAME]
#   ./install/superagent.sh uninstall [--profile NAME] [--apply] [--keep-data]
#   ./install/superagent.sh plan    [--profile NAME]
#
# The managed root contains releases, state, config and backups. Hermes retains
# only plugin links/config entries; uninstall is manifest-driven.
# Without --apply, install/update/uninstall print a plan and change nothing.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-python3}"
cmd=""
declare -a args=()
for arg in "$@"; do
  if [ -z "$cmd" ] && [[ "$arg" != --* ]]; then cmd="$arg"; else args+=("$arg"); fi
done
if [ -z "$cmd" ]; then
  printf 'usage: %s {plan|install|update|doctor|uninstall|candidate-hermes} [--profile NAME] [--apply]\n' "$0" >&2
  exit 2
fi
case "$cmd" in
  install|update|uninstall)
    apply=0
    for arg in "${args[@]}"; do [ "$arg" = "--apply" ] && apply=1; done
    if [ "$apply" = 0 ]; then
      "$PY" "$ROOT/tools/superagent.py" "${args[@]}" plan
      printf '\nPlan only. Re-run with --apply to make changes.\n'
      exit 0
    fi
    ;;
  plan|doctor|candidate-hermes) ;;
  *) printf 'unknown command: %s\n' "$cmd" >&2; exit 2 ;;
esac
filtered=()
for arg in "${args[@]}"; do [ "$arg" = "--apply" ] || filtered+=("$arg"); done
exec "$PY" "$ROOT/tools/superagent.py" "$cmd" "${filtered[@]}"
