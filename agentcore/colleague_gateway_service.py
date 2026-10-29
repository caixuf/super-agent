"""Trusted registration boundary for colleague Gateway dispatch tools.

A real host installs a fully constructed GatewayColleagueDispatcher after it has
bound Registry, Supervisor, readiness backend, session bindings and profile-bound
Gateway connections. Model tool arguments never carry host identities.
"""
from __future__ import annotations
import json
import re
import threading

_PROFILE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_SERVICES = {}
_LOCK = threading.RLock()
_SESSION_ENV = None


def configure_session_env(provider):
    """Install the host's session-environment reader (Hermes' ``get_session_env``).

    The engine cannot import the host's session module, and guessing whether a
    turn is interactive would silently widen the tool's reach.
    """
    global _SESSION_ENV
    if not callable(provider):
        raise TypeError("session_env_provider_required")
    _SESSION_ENV = provider


def register_gateway_dispatch_service(profile_name: str, dispatcher):
    """Install the trusted dispatcher for one Hermes runtime profile.

    Call from host bootstrap after constructing the real Registry/Supervisor,
    Provisioner, HermesSessionBindingStore and HermesGatewayHost. The colleague
    plugin intentionally does not manufacture any of those security authorities.
    """
    from agentcore.colleague_gateway_dispatch import GatewayColleagueDispatcher
    if not isinstance(profile_name, str) or not _PROFILE.fullmatch(profile_name):
        raise ValueError("invalid_host_profile")
    if type(dispatcher) is not GatewayColleagueDispatcher:
        raise TypeError("trusted_gateway_dispatcher_required")
    if (dispatcher.supervisor.profile_id != dispatcher.registry.profile_id
            or not callable(getattr(dispatcher.provisioner, "status", None))
            or not callable(getattr(dispatcher.bindings, "resolve", None))):
        raise PermissionError("incomplete_gateway_dispatch_service")
    with _LOCK:
        old = _SERVICES.get(profile_name)
        if old is not None and old is not dispatcher:
            raise RuntimeError("gateway_dispatch_service_already_installed")
        _SERVICES[profile_name] = dispatcher
    return dispatcher


def unregister_gateway_dispatch_service(profile_name: str, dispatcher) -> bool:
    """Remove exactly the instance installed by this host during orderly shutdown."""
    with _LOCK:
        if _SERVICES.get(profile_name) is not dispatcher:
            return False
        del _SERVICES[profile_name]
        return True


def _dispatcher(profile_name):
    with _LOCK:
        return _SERVICES.get(profile_name)


def _owner(profile_name, session_id, *, session_context_profile, session_context_id):
    if (not isinstance(profile_name, str) or not _PROFILE.fullmatch(profile_name)
            or not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 512):
        raise PermissionError("trusted_session_context_required")
    if session_context_id != session_id:
        raise PermissionError("session_context_mismatch")
    if session_context_profile and session_context_profile != profile_name:
        raise PermissionError("profile_context_mismatch")
    if _SESSION_ENV is None:
        raise RuntimeError("session_env_provider_required")
    if _SESSION_ENV("HERMES_CRON_SESSION", "") == "1":
        raise PermissionError("interactive_session_required")
    dispatcher = _dispatcher(profile_name)
    if dispatcher is None:
        raise RuntimeError("gateway_dispatch_host_unavailable")
    return dispatcher, {"profile": profile_name, "session": session_id}


def _member(dispatcher, profile_name, reference):
    from agentcore.colleague_registry import Actor
    registry, supervisor = dispatcher.registry, dispatcher.supervisor
    actor = Actor(registry.profile_id, profile_name)
    matches = []
    cursor = ""
    while True:
        page = registry.list_members(actor=actor, after=cursor, limit=200)
        matches.extend(item for item in page
                       if item["member_id"] == reference or item["display_name"].casefold() == reference.casefold())
        if len(page) < 200:
            break
        cursor = page[-1]["member_id"]
    if not matches:
        raise ValueError("unknown_colleague")
    if len(matches) != 1:
        raise ValueError("ambiguous_colleague_name")
    item = matches[0]
    if item["member_id"] == registry.primary_member(supervisor=supervisor):
        raise PermissionError("primary_protected")
    if item["status"] != "active":
        raise PermissionError("colleague_not_accepting_work")
    return item


def assign(profile_name, session_id, session_context_profile, session_context_id,
           colleague, request_id, task):
    dispatcher, owner = _owner(profile_name, session_id, session_context_profile=session_context_profile,
                               session_context_id=session_context_id)
    member = _member(dispatcher, profile_name, colleague)
    return _present_status(dispatcher.submit(owner=owner, member_id=member["member_id"], generation=member["generation"],
                                             request_id=request_id, prompt=task))


def inspect(profile_name, session_id, session_context_profile, session_context_id, request_id, response_offset=0, response_revision=None):
    dispatcher, owner = _owner(profile_name, session_id, session_context_profile=session_context_profile,
                               session_context_id=session_context_id)
    result = dispatcher.inspect(owner=owner, request_id=request_id, response_offset=response_offset,
                                response_revision=response_revision)
    return _present_status(result) if result is not None else {"state": "unknown", "error": "task_not_found"}


def _present_status(result):
    """Explain host evidence without treating a terminal model turn as full completion."""
    value = dict(result)
    state = value.get("state")
    reservation = value.get("reservation")
    outcome = value.get("outcome")
    reply = value.get("result")
    if state == "queued":
        message = "同事已接单，正在等待执行。"
    elif state == "started":
        message = "同事正在处理任务。"
    elif state != "terminal":
        message = "任务状态待确认；请先查询原任务，不要重复派工。"
    elif outcome in {"failed", "interrupted", "cancelled"}:
        message = "同事本轮未完成。"
        if reservation != "closed":
            message += "后台工作是否结束仍待确认。"
    elif outcome != "completed":
        message = "同事本轮已结束，但结果状态尚未确认。"
    elif (not isinstance(reply, dict) or reply.get("completed") is not True
          or reply.get("failed") is not False or reply.get("interrupted") is not False):
        message = "同事本轮已结束，但结果尚未确认。"
    elif reservation != "closed":
        message = "同事已回复；后台工作是否全部结束仍待确认。"
    else:
        message = "同事已完成任务。"
    if isinstance(reply, dict) and reply.get("response_truncated") is True:
        message += "答复本页已截断；可用返回的 response_next_offset 和 response_revision 继续读取。"
    value["status_message"] = message
    return value


def json_result(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
