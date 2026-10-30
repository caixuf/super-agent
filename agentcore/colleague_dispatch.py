"""Host-owned durable admission for general colleague turns.

The queue is dedicated to this protocol. It does not deliver messages: a host
must authenticate callers, authorize both admission and execution, and route
the terminal receipt only after checking its owner. Jobs are never replayed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from copy import deepcopy
from typing import Optional

from agentcore.pool import Pool, normalize_origin

_MEMBER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_REQUEST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def _payload(owner, member_id, generation, request_id, prompt):
    canonical = normalize_origin(owner)
    if canonical is None:
        raise PermissionError("authenticated_owner_required")
    owner = json.loads(canonical)
    if not isinstance(member_id, str) or not _MEMBER.fullmatch(member_id):
        raise ValueError("invalid_member_id")
    if type(generation) is not int or generation < 1:
        raise ValueError("invalid_generation")
    if not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id):
        raise ValueError("invalid_request_id")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
        raise ValueError("invalid_prompt")
    return {"owner": owner, "member_id": member_id, "generation": generation,
            "request_id": request_id, "prompt": prompt}


def compute_result_signature(result: dict, key: str) -> str:
    """Compute HMAC-SHA256 digital signature over trusted result attribution fields."""
    if not isinstance(result, dict) or not isinstance(key, str) or not key:
        raise ValueError("invalid_signature_args")
    body = result.get("text") or result.get("cli_stdout_unparsed") or ""
    canonical = json.dumps([
        result.get("request_id"),
        result.get("requested_by"),
        result.get("executed_by"),
        hashlib.sha256(str(body).encode("utf-8")).hexdigest(),
    ], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hmac.new(key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_result_signature(result: dict, key: str, signature: Optional[str] = None) -> bool:
    """Verify constant-time HMAC-SHA256 signature for trusted attribution."""
    expected_sig = signature or (result.get("signature") if isinstance(result, dict) else None)
    if not isinstance(expected_sig, str) or not expected_sig:
        return False
    try:
        computed = compute_result_signature(result, key)
        return hmac.compare_digest(computed, expected_sig)
    except Exception:
        return False


class ColleagueDispatch:
    """Admission and owner-bound inspection; callers must authenticate first."""

    def __init__(self, pool, *, authorize_assignment, dedicated=False, signing_key: Optional[str] = None):
        if not isinstance(pool, Pool) or not callable(authorize_assignment) or dedicated is not True:
            raise ValueError("dedicated_pool_and_authorizer_required")
        info = pool.root.stat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077
                or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
            raise ValueError("private_pool_required")
        self.pool, self.authorize_assignment = pool, authorize_assignment
        self.signing_key = signing_key

    def _authorize(self, payload):
        if self.authorize_assignment(deepcopy(payload["owner"]), payload["member_id"],
                                     payload["generation"]) is not True:
            raise PermissionError("assignment_not_authorized")

    def submit(self, *, owner, member_id, generation, request_id, prompt):
        payload = _payload(owner, member_id, generation, request_id, prompt)
        self._authorize(payload)
        # Scope a request to the authenticated caller, not the selected member:
        # reusing the request ID with different work must fail, not run twice.
        identity = json.dumps([payload["owner"]["profile"], payload["owner"]["session"], request_id], sort_keys=True,
                              separators=(",", ":"), ensure_ascii=False)
        key = "colleague:" + hashlib.sha256(identity.encode()).hexdigest()
        job = self.pool.submit(key, payload, replay_safe=False, max_tries=1, origin=None)
        return {"job_id": job, "state": self.pool.status(job)["status"]}

    def inspect(self, *, owner, job_id):
        canonical = normalize_origin(owner)
        if canonical is None:
            raise PermissionError("authenticated_owner_required")
        owner = json.loads(canonical)
        with self.pool.db() as db:
            row = db.execute("SELECT payload,status,result FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        if payload.get("owner") != owner:
            raise PermissionError("queue_owner_mismatch")
        self._authorize(payload)
        result = json.loads(row["result"]) if row["result"] else None
        state = row["status"]
        if state == "completed":
            # The Pool confirms that the runner process returned, not that an
            # unparsed CLI transcript is a verified answer or that delivery
            # occurred. Hosts must explicitly verify before claiming success.
            state = "result_unverified"
        out = {"job_id": job_id, "state": state, "queue_state": row["status"],
               "result": result}
        if self.signing_key and isinstance(result, dict):
            out["signature_valid"] = verify_result_signature(result, self.signing_key)
        return out


def execute_payload(payload, *, runner, authorize_assignment, signing_key: Optional[str] = None):
    """Execute a job in a trusted worker; the payload cannot choose an executable."""
    if not isinstance(payload, dict) or set(payload) != {
        "owner", "member_id", "generation", "request_id", "prompt"
    }:
        raise ValueError("invalid_colleague_job")
    checked = _payload(payload["owner"], payload["member_id"], payload["generation"],
                       payload["request_id"], payload["prompt"])
    if not callable(authorize_assignment) or authorize_assignment(
            deepcopy(checked["owner"]), checked["member_id"], checked["generation"]) is not True:
        raise PermissionError("assignment_not_authorized")
    owner = checked["owner"]
    result = runner.run(member_id=checked["member_id"], generation=checked["generation"],
                        request_id=checked["request_id"], prompt=checked["prompt"],
                        owner_profile=owner["profile"], owner_session=owner["session"])
    # Names in model output are not proof of identity. Only accept the host
    # runner's structured attribution when it matches the admitted assignment.
    if (not isinstance(result, dict) or result.get("request_id") != checked["request_id"]
            or not isinstance(result.get("requested_by"), dict)
            or result["requested_by"].get("profile") != owner["profile"]
            or result["requested_by"].get("session") != owner["session"]
            or not isinstance(result.get("executed_by"), dict)
            or result["executed_by"].get("member_id") != checked["member_id"]
            or type(result["executed_by"].get("generation")) is not int
            or result["executed_by"].get("generation") != checked["generation"]):
        raise ValueError("colleague_result_identity_mismatch")
    if signing_key:
        result["signature"] = compute_result_signature(result, signing_key)
    return result

