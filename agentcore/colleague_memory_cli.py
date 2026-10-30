"""Fixed-profile localvec query runner; the only untrusted argument is query."""
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
        from agentcore.memory.runtime_bridge import prefetch_context
        result=prefetch_context(a.query,limit=6,max_chars=5000)
        if result.get("ok") is not True:
            print(json.dumps({"ok":False,"error":"memory_recall_unavailable"})); return 2
        print(json.dumps({"ok":True,"results":result.get("facts",[]),
                          "empty_reason":result.get("empty_reason")},ensure_ascii=False))
        return 0
    except Exception:
        print(json.dumps({"ok":False,"error":"memory_recall_unavailable"})); return 2

if __name__=="__main__":
    raise SystemExit(main())
