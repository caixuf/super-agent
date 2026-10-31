"""Fixed-profile localvec query runner; the only untrusted argument is query.

Localvec injects recalled *statements*. Prefer-A ``prefetch_context`` returns L1
index stubs (empty statement); this CLI therefore calls ``recall_facts`` so the
subprocess JSON contract stays body-bearing for automatic recall.
"""
import argparse, json
from agentcore.colleague_shared_memory import configure_memory_from_binding_file

def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument("--bindings",required=True)
    p.add_argument("--profile",required=True)
    p.add_argument("query")
    a=p.parse_args(argv)
    if len(a.query)>8192:
        print(json.dumps({"ok":False,"error":"query_too_long"})); return 2
    try:
        configure_memory_from_binding_file(a.bindings,a.profile)
        from agentcore.memory import memory_store
        result=memory_store.recall_facts(a.query,limit=6)
        if result.get("ok") is not True:
            print(json.dumps({"ok":False,"error":"memory_recall_unavailable"})); return 2
        rows=[]
        for item in result.get("results") or []:
            if not isinstance(item, dict):
                continue
            statement=str(item.get("statement") or "").strip()
            fact_id=str(item.get("fact_id") or "").strip()
            if not statement:
                continue
            rows.append({
                "fact_id": fact_id,
                "subject": str(item.get("subject") or ""),
                "statement": statement,
            })
        print(json.dumps({"ok":True,"results":rows,
                          "empty_reason":result.get("empty_reason")},ensure_ascii=False))
        return 0
    except Exception:
        print(json.dumps({"ok":False,"error":"memory_recall_unavailable"})); return 2

if __name__=="__main__":
    raise SystemExit(main())
