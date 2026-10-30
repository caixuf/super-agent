"""Resolve a model's context window from an OpenAI-compatible route.

Why this exists: ``/v1/models`` is the standard catalog, but several gateways
(notably LiteLLM) publish ``max_input_tokens`` only on ``/v1/model/info``. A
client that probes only ``/v1/models`` falls back to a generic 256K window and
compresses long sessions far too early. This module merges both views and
compares the result with the profile's manual pins, so a deployment can prove
the automatic path instead of maintaining a stale number by hand.

Only stdlib is used; the agentcore import boundary stays intact.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

_INPUT_KEYS = (
    "max_input_tokens",
    "context_length",
    "context_window",
    "max_context_length",
    "max_model_len",
    "max_position_embeddings",
)
_OUTPUT_KEYS = ("max_output_tokens", "max_completion_tokens", "max_tokens")
_DEFAULT_TIMEOUT = 5.0


class ProbeError(RuntimeError):
    """The endpoint could not be reached or returned an unreadable payload."""


@dataclass(frozen=True)
class Window:
    model: str
    max_input_tokens: Optional[int] = None
    max_output_tokens: Optional[int] = None
    source: str = ""


@dataclass
class Catalog:
    base_url: str
    windows: Dict[str, Window] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def effective(self, model: str) -> Optional[Window]:
        if model in self.windows:
            return self.windows[model]
        wanted = model.lower()
        for name, window in self.windows.items():
            if name.lower() == wanted:
                return window
        return None


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _first_int(payload: Dict[str, Any], keys) -> Optional[int]:
    for key in keys:
        number = _positive_int(payload.get(key))
        if number is not None:
            return number
    return None


def _endpoint(base_url: str, suffix: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("base_url is empty")
    # Accept both http://host:port and http://host:port/v1. The probe paths are
    # the conventional OpenAI paths under the versioned root.
    if not base.endswith("/v1"):
        base += "/v1"
    return base + "/" + suffix.lstrip("/")


def parse_models(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """``{id: {max_input_tokens, max_output_tokens}}`` from /v1/models."""
    out: Dict[str, Dict[str, Any]] = {}
    for entry in payload.get("data", []) if isinstance(payload, dict) else []:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        window = {}
        context = _first_int(entry, _INPUT_KEYS)
        if context is not None:
            window["max_input_tokens"] = context
        output = _first_int(entry, _OUTPUT_KEYS)
        if output is not None:
            window["max_output_tokens"] = output
        if window:
            out[model_id] = window
    return out


def parse_model_info(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """``{model_name: {max_input_tokens, max_output_tokens}}`` from /v1/model/info.

    A routing group may have several deployments; the smallest window is the
    only safe one to advertise for the group.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for entry in payload.get("data", []) if isinstance(payload, dict) else []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("model_name") or entry.get("model") or entry.get("id")
        if not isinstance(name, str) or not name:
            continue
        params = entry.get("litellm_params")
        info = params.get("model_info") if isinstance(params, dict) else None
        if not isinstance(info, dict):
            info = entry.get("model_info") if isinstance(entry.get("model_info"), dict) else {}
        window = out.setdefault(name, {})
        context = _first_int(info, _INPUT_KEYS)
        if context is not None:
            window["max_input_tokens"] = min(context, window.get("max_input_tokens", context))
        output = _first_int(info, _OUTPUT_KEYS)
        if output is not None:
            window["max_output_tokens"] = min(output, window.get("max_output_tokens", output))
    return {name: window for name, window in out.items() if window}


def merge_windows(
    models: Dict[str, Dict[str, Any]],
    info: Dict[str, Dict[str, Any]],
) -> Dict[str, Window]:
    """Merge the standard catalog with the admin metadata.

    The standard list wins where it has a value; ``/v1/model/info`` fills the
    gaps (and contributes models that /v1/models omitted).
    """
    names = list(models) + [name for name in info if name not in models]
    windows: Dict[str, Window] = {}
    for name in names:
        standard = models.get(name, {})
        admin = info.get(name, {})
        context = standard.get("max_input_tokens", admin.get("max_input_tokens"))
        output = standard.get("max_output_tokens", admin.get("max_output_tokens"))
        source = "models" if standard.get("max_input_tokens") is not None else "model/info"
        windows[name] = Window(
            model=name,
            max_input_tokens=context,
            max_output_tokens=output,
            source=source if context is not None else "",
        )
    return windows


def _fetch_json(url: str, api_key: str = "", timeout: float = _DEFAULT_TIMEOUT) -> Dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        raise ProbeError(f"HTTP {error.code} from {url}") from error
    except (urllib.error.URLError, OSError) as error:
        raise ProbeError(f"cannot reach {url}: {error}") from error
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProbeError(f"invalid JSON from {url}") from error
    if not isinstance(payload, dict):
        raise ProbeError(f"unexpected payload from {url}")
    return payload


def probe(
    base_url: str,
    *,
    api_key: str = "",
    timeout: float = _DEFAULT_TIMEOUT,
    fetch: Optional[Callable[[str, str, float], Dict[str, Any]]] = None,
) -> Catalog:
    """Read /v1/models and /v1/model/info; a failure in one does not hide the other."""
    fetcher = fetch or _fetch_json
    catalog = Catalog(base_url=base_url)
    models: Dict[str, Dict[str, Any]] = {}
    info: Dict[str, Dict[str, Any]] = {}
    try:
        models = parse_models(fetcher(_endpoint(base_url, "models"), api_key, timeout))
    except Exception as error:
        catalog.warnings.append(f"/v1/models: {error}")
    try:
        info = parse_model_info(fetcher(_endpoint(base_url, "model/info"), api_key, timeout))
    except Exception as error:
        catalog.warnings.append(f"/v1/model/info: {error}")
    if not models and not info:
        raise ProbeError("; ".join(catalog.warnings) or "no model metadata")
    catalog.windows = merge_windows(models, info)
    return catalog


def _scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if value[0:1] in ("'", '"') and value[-1:] == value[0]:
        return value[1:-1]
    lowered = value.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "~"):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def load_config_subset(path: str | Path) -> Dict[str, Any]:
    """Parse the mapping-only YAML subset used by profile configs.

    Lists, block scalars and anchors are skipped; the model/providers/
    compression keys this module needs are plain block mappings.
    """
    text = Path(path).read_text(encoding="utf-8")
    root: Dict[str, Any] = {}
    stack: List[tuple[int, Dict[str, Any]]] = [(-1, root)]
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        raw = lines[index]
        index += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        content = raw.strip()
        if content.startswith("- ") or content == "-":
            # Skip a list item and its nested block; this module only reads maps.
            while index < len(lines):
                nxt = lines[index]
                if not nxt.strip():
                    index += 1
                    continue
                nindent = len(nxt) - len(nxt.lstrip(" "))
                if nindent <= indent:
                    break
                index += 1
            continue
        key, separated, value = content.partition(":")
        if not separated:
            continue
        key = key.strip().strip("'\"")
        value = value.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            stack = [(-1, root)]
        parent = stack[-1][1]
        if value in ("", "|", ">"):
            child: Dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
            if value in ("|", ">"):
                while index < len(lines):
                    nxt = lines[index]
                    if nxt.strip() and (len(nxt) - len(nxt.lstrip(" "))) <= indent:
                        break
                    index += 1
        else:
            parent[key] = _scalar(value.split(" #", 1)[0].strip())
    return root


def _lookup(config: Dict[str, Any], *path: str) -> Any:
    cursor: Any = config
    for key in path:
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(key)
    return cursor


def resolve_base_url(config: Dict[str, Any]) -> str:
    """``model.base_url`` first, then the configured provider's URL."""
    configured = _lookup(config, "model", "base_url")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    provider = _lookup(config, "model", "provider")
    if not isinstance(provider, str) or not provider.strip():
        return ""
    name = provider.strip()
    if name.startswith("custom:"):
        name = name.split(":", 1)[1]
    url = _lookup(config, "providers", name, "base_url")
    return url.strip() if isinstance(url, str) else ""


def check_config(catalog: Catalog, config: Dict[str, Any], model: str) -> List[str]:
    """Return human-readable inconsistencies between the catalog and config."""
    warnings: List[str] = []
    window = catalog.effective(model)
    if window is None or window.max_input_tokens is None:
        warnings.append(f"catalog has no context window for model {model!r}")
        return warnings
    context = window.max_input_tokens
    pin = _lookup(config, "model", "context_length")
    if pin is not None:
        try:
            pin = int(pin)
        except (TypeError, ValueError):
            pin = None
        if pin is not None:
            if pin != context:
                warnings.append(
                    f"manual model.context_length={pin} differs from catalog {context}; "
                    f"remove the pin so the catalog stays authoritative")
            else:
                warnings.append(
                    f"manual model.context_length={pin} duplicates the catalog; remove the pin")
    compression = _lookup(config, "auxiliary", "compression", "context_length")
    if compression is not None:
        try:
            compression = int(compression)
        except (TypeError, ValueError):
            compression = None
        if compression is not None and compression != context:
            warnings.append(
                f"auxiliary.compression.context_length={compression} differs from catalog {context}")
    threshold = _lookup(config, "compression", "threshold_tokens")
    if threshold is not None:
        try:
            threshold = int(threshold)
        except (TypeError, ValueError):
            threshold = None
        if threshold is not None and threshold >= context:
            warnings.append(
                f"compression.threshold_tokens={threshold} is not below the catalog window {context}")
    return warnings



def check_compression_budget(config: Dict[str, Any], budget_tokens: int) -> List[str]:
    """Compare a user-chosen performance budget with the configured trigger.

    The trigger is not a hard ceiling: one large turn can overshoot it and
    compression can fail. This check never changes a profile.
    """
    if isinstance(budget_tokens, bool) or not isinstance(budget_tokens, int) or budget_tokens <= 0:
        raise ValueError('budget_tokens must be a positive integer')
    if _lookup(config, 'compression', 'enabled') is False:
        return ['compression is disabled; the chosen performance budget is not enforced']
    threshold = _positive_int(_lookup(config, 'compression', 'threshold_tokens'))
    if threshold is not None and threshold > budget_tokens:
        return [f'compression.threshold_tokens={threshold} exceeds the chosen '
                f'performance budget {budget_tokens}; compression may not start before that budget']
    return []


def estimate_tokens(content: Union[str, Dict[str, Any], Sequence[Dict[str, Any]]]) -> int:
    """Fast approximation of token count for text or message list."""
    if isinstance(content, str):
        cjk = len(re.findall(r"[\u4e00-\u9fff]", content))
        words = len(re.findall(r"[A-Za-z0-9_]+", content))
        other = max(0, len(content) - (cjk + words * 4)) // 4
        return max(1, cjk + words + other)
    elif isinstance(content, dict):
        text = str(content.get("content") or "")
        return 4 + estimate_tokens(text)
    elif isinstance(content, (list, tuple)):
        return sum(estimate_tokens(m) for m in content) + 2
    return 1


def compact_context_window(
    messages: Sequence[Dict[str, Any]],
    *,
    max_tokens: int,
    threshold_tokens: Optional[int] = None,
    keep_recent_turns: int = 2,
    reserve_tokens: int = 200,
) -> List[Dict[str, Any]]:
    """Structured context sliding compaction when window approaches threshold.

    Preserves head system instructions and tail recent turns, while compacting
    interim turns into a structured summary to prevent context overflow or attention dilution.
    """
    if type(max_tokens) is not int or max_tokens <= 0:
        raise ValueError("max_tokens must be a positive integer")
    if threshold_tokens is not None and (type(threshold_tokens) is not int or threshold_tokens <= 0):
        raise ValueError("threshold_tokens must be a positive integer")
    if not messages:
        return []
    threshold = threshold_tokens if threshold_tokens is not None else int(max_tokens * 0.8)
    total_tokens = estimate_tokens(messages)
    if total_tokens <= threshold:
        return [dict(m) for m in messages]

    head_system: List[Dict[str, Any]] = []
    dialogue: List[Dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "system" and not dialogue:
            head_system.append(dict(m))
        else:
            dialogue.append(dict(m))

    tail_count = max(1, keep_recent_turns * 2)
    if len(dialogue) <= tail_count:
        return head_system + dialogue

    middle_turns = dialogue[:-tail_count]
    tail_turns = dialogue[-tail_count:]

    summary_lines = [
        f"[Context Sliding Compaction: condensed {len(middle_turns)} historical dialogue turns]"
    ]
    for idx, turn in enumerate(middle_turns, 1):
        role = turn.get("role", "unknown")
        raw_content = str(turn.get("content") or "").strip()
        first_line = raw_content.splitlines()[0][:120] if raw_content else ""
        summary_lines.append(f"- Turn {idx} ({role}): {first_line}")

    summary_message = {
        "role": "system",
        "content": "\n".join(summary_lines),
    }

    compacted = head_system + [summary_message] + tail_turns
    target_budget = max(100, threshold - reserve_tokens)
    while len(tail_turns) > 2 and estimate_tokens(compacted) > target_budget:
        slide_turn = tail_turns.pop(0)
        role = slide_turn.get("role", "unknown")
        raw_content = str(slide_turn.get("content") or "").strip()
        first_line = raw_content.splitlines()[0][:120] if raw_content else ""
        summary_lines.append(f"- Slid Turn ({role}): {first_line}")
        summary_message["content"] = "\n".join(summary_lines)
        compacted = head_system + [summary_message] + tail_turns

    return compacted


def _print_report(catalog: Catalog, model: str, warnings: List[str]) -> None:
    window = catalog.effective(model)
    print(f"base_url: {catalog.base_url}")
    print(f"model:    {model}")
    if window is None:
        print("context:  UNKNOWN")
    else:
        print(f"context:  {window.max_input_tokens or 'UNKNOWN'} (source={window.source or 'none'})")
        if window.max_output_tokens:
            print(f"output:   {window.max_output_tokens}")
    for warning in catalog.warnings + warnings:
        print(f"warning:  {warning}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="", help="OpenAI-compatible root, e.g. http://127.0.0.1:4000/v1")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--config", default="", help="profile config.yaml used for model/provider defaults")
    parser.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT)
    parser.add_argument("--performance-budget-tokens", type=int, default=None,
                        help="optional workload budget to compare with compression trigger")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    config: Dict[str, Any] = {}
    if args.config:
        try:
            config = load_config_subset(args.config)
        except OSError as error:
            print(f"cannot read config: {error}", file=sys.stderr)
            return 2
    base_url = args.base_url or (resolve_base_url(config) if config else "")
    model = args.model or (_lookup(config, "model", "default") if config else "") or "main"
    if not base_url:
        print("no --base-url and no model/base_url or providers.<provider>.base_url in config", file=sys.stderr)
        return 2
    try:
        catalog = probe(base_url, api_key=args.api_key, timeout=args.timeout)
    except ProbeError as error:
        print(f"probe failed: {error}", file=sys.stderr)
        return 2
    warnings = check_config(catalog, config, model) if config else []
    if args.performance_budget_tokens is not None:
        try:
            warnings.extend(check_compression_budget(config, args.performance_budget_tokens))
        except ValueError as error:
            parser.error(str(error))
    if args.json:
        window = catalog.effective(model)
        payload = {
            "base_url": catalog.base_url,
            "model": model,
            "context_length": window.max_input_tokens if window else None,
            "output_length": window.max_output_tokens if window else None,
            "source": window.source if window else None,
            "warnings": catalog.warnings + warnings,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_report(catalog, model, warnings)
    window = catalog.effective(model)
    return 0 if window is not None and window.max_input_tokens else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a CLI
    raise SystemExit(main())
