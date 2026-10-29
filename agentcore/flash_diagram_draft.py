"""Generate a source-linked diagram draft; rendering and semantic review are separate."""
from __future__ import annotations

import json
import re


class DraftError(ValueError):
    """Bounded public error; does not include source text or model output."""


_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")


def draft_diagram(facts, *, model):
    """Return a review-required graph from trusted source facts, never render or send.

    Text provenance is checked mechanically. This does not prove that edges or
    paraphrases are semantically correct; the caller must review before delivery.
    """
    if not callable(model):
        raise DraftError("trusted_model_required")
    if (not isinstance(facts, list) or not 1 <= len(facts) <= 30
            or any(not isinstance(f, str) or not f.strip() or len(f) > 1000 for f in facts)):
        raise DraftError("invalid_facts")
    if sum(len(f) for f in facts) > 12000:
        raise DraftError("facts_too_large")
    messages = [{"role": "system", "content": (
        "Create a draft graph from the numbered facts only. Return JSON with nodes "
        "[{id,label,fact}] and edges [{from,to,fact}]. fact is the 1-based source number. "
        "Each node label must occur verbatim in its referenced fact; cite the fact that "
        "supports each edge. Do not add details, run tools, or output Markdown.")},
        {"role": "user", "content": json.dumps({"facts": facts}, ensure_ascii=False)}]
    try:
        raw = model(messages, timeout_seconds=45)
    except Exception:
        raise DraftError("model_call_failed") from None
    if not isinstance(raw, str) or len(raw) > 100000:
        raise DraftError("invalid_model_output")
    text = raw.strip()
    fence = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1)
    try:
        result = json.loads(text)
    except ValueError:
        raise DraftError("invalid_model_json") from None
    if not isinstance(result, dict) or set(result) != {"nodes", "edges"}:
        raise DraftError("invalid_graph")
    nodes, edges = result["nodes"], result["edges"]
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 40 or not isinstance(edges, list) or len(edges) > 80:
        raise DraftError("invalid_graph")
    ids = set()
    labels = {}
    for node in nodes:
        if not isinstance(node, dict) or set(node) != {"id", "label", "fact"}:
            raise DraftError("invalid_node")
        ident, label, ref = node["id"], node["label"], node["fact"]
        if (not isinstance(ident, str) or not _ID.fullmatch(ident) or ident in ids
                or not isinstance(label, str) or not 1 <= len(label) <= 80
                or type(ref) is not int or not 1 <= ref <= len(facts) or label not in facts[ref - 1]):
            raise DraftError("unverified_node")
        ids.add(ident)
        labels[ident] = label
    for edge in edges:
        if not isinstance(edge, dict) or set(edge) != {"from", "to", "fact"}:
            raise DraftError("invalid_edge")
        if (not isinstance(edge["from"], str) or edge["from"] not in ids
                or not isinstance(edge["to"], str) or edge["to"] not in ids
                or type(edge["fact"]) is not int or not 1 <= edge["fact"] <= len(facts)):
            raise DraftError("unverified_edge")
        evidence = facts[edge["fact"] - 1]
        if labels[edge["from"]] not in evidence or labels[edge["to"]] not in evidence:
            raise DraftError("unverified_edge")
    return {"nodes": nodes, "edges": edges, "review_required": True}
