"""Profile-scoped gate for unattended cron memory rewrites."""
import importlib.util
import json
from pathlib import Path


def check_write(path,before,after):
    from gateway.session_context import get_session_env
    if str(get_session_env('HERMES_CRON_SESSION','')).lower() not in ('1','true','yes','on'):return None
    import hermes_yaml as yaml
    home=Path(path).resolve().parent.parent
    config_path=home/'config.yaml'
    if not config_path.exists():return None
    config=yaml.safe_load(config_path.read_text()) or {}
    settings=(config.get('memory') or {}).get('conservation') or {}
    if settings.get('enabled') is not True:return None
    engine=Path(settings.get('engine_root',''));queue=Path(settings.get('queue_root',''))
    if not engine.is_absolute() or not queue.is_absolute():raise ValueError('absolute conservation paths required')
    spec=importlib.util.spec_from_file_location('_memory_conservation',engine/'agentcore/memory/conservation.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    refs={}
    for name,item in settings.get('references',{}).items():
        resource=Path(item['path'])
        if not resource.is_absolute() or resource.stat().st_size>512*1024:raise ValueError('reference budget')
        refs[name]=dict(text=resource.read_text(),pointer=item['pointer'],authority=item.get('authority') is True)
    key=module.proposal_key(path,before,after)
    approved=queue/'reviewed'/(key+'.json')
    review=json.loads(approved.read_text()) if approved.is_file() else None
    report=module.evaluate(before,after,references=refs,review=review)
    if report['passed']:return None
    try:
        pending=module.stage(queue,path,before,after,report)
    except module.CapacityExceeded:
        return dict(success=False,staged=False,conservation_capacity_exceeded=True,
                    error='保全候选队列已满，原记忆未改；请先核对或归档已有候选，本轮停止重试。')
    return dict(success=False,staged=True,conservation_pending=True,proposal_id=pending['id'],
                unresolved_count=pending['unresolved_count'],
                error='自动压缩尚缺保全证据，候选已保存，原记忆未改；请补齐校验后再应用。')
