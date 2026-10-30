"""Host-injected one-turn Hermes execution for isolated colleague pilots.

This module is deliberately not registered as a Hermes production plugin. The host
must authenticate the submitting session, load owner-only member/session bindings,
and inject a real Provisioner before exposing dispatch.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import tempfile
import time
from typing import Callable, Mapping

from agentcore import colleague_containment as containment
from agentcore.colleague_registry import Supervisor

_PROFILE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_MEMBER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,511}\Z")
_SESSION_ID = re.compile(r"local-[0-9a-f]{64}\Z")
_REQUEST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


@dataclass(frozen=True)
class ProfileBinding:
    member_id: str
    generation: int
    owner_profile: str
    owner_session: str
    hermes_profile: str
    hermes_session_id: str


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    # Whole-attempt evidence. A turn is only settled when the attempt is proven
    # empty; a launcher that cannot prove it must leave this False.
    quiescent: bool = False
    containment: dict | None = None
    # What the sweep could observe. "attempt" covers every process the turn
    # could have started. "cli_client" covers only the client tree: a turn that
    # runs inside a long-lived gateway starts its tools' processes from that
    # gateway, which never inherited the attempt environment, so an empty sweep
    # proves only that the client exited.
    containment_scope: str = "attempt"


class HermesExecutionError(RuntimeError):
    pass


def load_profile_bindings(path: str | Path) -> dict[tuple[str, int, str, str], ProfileBinding]:
    """Load administrator-owned JSON bindings from an owner-only regular file.

    Each inbound owner conversation maps to its own pre-created Hermes session ID.
    Title lookup is never used, because duplicate titles can resolve ambiguously.
    """
    source = Path(path)
    if not source.is_absolute() or source.is_symlink():
        raise ValueError("absolute_binding_file_required")
    info = source.stat()
    if (not source.is_file() or info.st_mode & 0o077 or info.st_size > 65536
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
        raise ValueError("private_binding_file_required")
    raw = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or len(raw) > 4096:
        raise ValueError("invalid_bindings")
    result: dict[tuple[str, int, str, str], ProfileBinding] = {}
    profiles: dict[tuple[str, int], str] = {}
    sessions: set[tuple[str, str]] = set()
    for item in raw:
        fields = {"member_id", "generation", "owner_profile", "owner_session",
                  "hermes_profile", "hermes_session_id"}
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError("invalid_binding_entry")
        member, generation = item["member_id"], item["generation"]
        owner_profile, owner_session = item["owner_profile"], item["owner_session"]
        profile, hermes_session_id = item["hermes_profile"], item["hermes_session_id"]
        if (not isinstance(member, str) or not _MEMBER.fullmatch(member)
                or type(generation) is not int or generation < 1
                or not isinstance(owner_profile, str) or not _IDENTITY.fullmatch(owner_profile)
                or not isinstance(owner_session, str) or not _IDENTITY.fullmatch(owner_session)
                or not isinstance(profile, str) or not _PROFILE.fullmatch(profile)
                or profile in {"default", "latest"}
                or not isinstance(hermes_session_id, str)
                or not _SESSION_ID.fullmatch(hermes_session_id)):
            raise ValueError("invalid_binding_entry")
        key = (member, generation, owner_profile, owner_session)
        session_key = (profile, hermes_session_id)
        if key in result or session_key in sessions:
            raise ValueError("duplicate_binding")
        member_key = (member, generation)
        if member_key in profiles and profiles[member_key] != profile:
            raise ValueError("member_profile_binding_conflict")
        if any(k[0] != member_key[0] or k[1] != member_key[1]
               for k in profiles if profiles[k] == profile):
            raise ValueError("profile_shared_between_members")
        result[key] = ProfileBinding(member, generation, owner_profile, owner_session,
                                     profile, hermes_session_id)
        profiles[member_key] = profile
        sessions.add(session_key)
    return result



class HermesSessionCreationPending(RuntimeError):
    """The host create call may have committed; retry only via its same request ID."""


class HermesGatewaySessionCreator:
    """Call Hermes' profile-bound GatewaySessionStore session.create API.

    connection_for_profile is trusted host infrastructure and must authenticate to
    the requested profile with a stable principal across retries. The Gateway's local
    creation receipt is durable and keyed by that principal plus request_id.
    """

    def __init__(self, connection_for_profile: Callable, *, cwd_for_profile: Callable,
                 store_factory: Callable | None = None):
        """``store_factory(connection)`` builds the host's session store.

        The store belongs to the host runtime; this package only drives the
        idempotent ``session.create`` call, so it cannot import it.
        """
        if not callable(connection_for_profile) or not callable(cwd_for_profile):
            raise ValueError("trusted_profile_connection_and_cwd_required")
        if store_factory is not None and not callable(store_factory):
            raise ValueError("trusted_session_store_factory_required")
        self.connection_for_profile = connection_for_profile
        self.cwd_for_profile = cwd_for_profile
        self.store_factory = store_factory

    def create(self, profile: str, request_id: str) -> str:
        if self.store_factory is None:
            raise RuntimeError("session_store_factory_required")

        cwd = self.cwd_for_profile(profile)
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            raise ValueError("absolute_profile_workdir_required")
        store = self.store_factory(self.connection_for_profile(profile))
        # Stable parameters on every retry; no title lookup, ambient route, or client
        # environment is sent. request_id is consumed by gateway.session_controls,
        # whose durable local receipt maps it back to the exact session ID.
        snapshot = store.call("session.create", request_id=request_id, source="cli",
                             cwd=cwd, row_cwd="")
        if not isinstance(snapshot, dict):
            raise RuntimeError("invalid_hermes_session_create_result")
        session_id = snapshot.get("stored_session_id") or snapshot.get("session_id")
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise RuntimeError("invalid_hermes_session_id")
        return session_id


class HermesSessionBindingStore:
    """Durably bind an owner conversation to a member-generation Hermes session.

    A pending row keeps the same deterministic Hermes create request ID across
    restarts. If the Gateway committed but the response was lost, the same public
    session.create request recovers that session instead of minting a duplicate.
    """

    def __init__(self, *, registry, supervisor: Supervisor, profiles: Mapping,
                 creator, state_dir: str | Path, lock_timeout: float = 30):
        if type(supervisor) is not Supervisor or supervisor.profile_id != registry.profile_id:
            raise PermissionError("trusted_supervisor_required")
        if not callable(getattr(creator, "create", None)):
            raise ValueError("idempotent_hermes_session_creator_required")
        directory = Path(state_dir)
        if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
            raise ValueError("private_state_directory_required")
        info = directory.stat()
        if info.st_mode & 0o077 or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
            raise ValueError("private_state_directory_required")
        if isinstance(lock_timeout, bool) or not isinstance(lock_timeout, (int, float)) or lock_timeout <= 0:
            raise ValueError("invalid_lock_timeout")
        self.registry, self.supervisor, self.creator = registry, supervisor, creator
        self.profiles, self.state_dir = dict(profiles), directory
        self.lock_timeout = float(lock_timeout)
        seen_profiles = {}
        for key, profile in self.profiles.items():
            if (not isinstance(key, tuple) or len(key) != 2
                    or not isinstance(key[0], str) or not _MEMBER.fullmatch(key[0])
                    or type(key[1]) is not int or key[1] < 1
                    or not isinstance(profile, str) or not _PROFILE.fullmatch(profile)
                    or profile in {"default", "latest"}):
                raise ValueError("invalid_member_profile_map")
            if profile in seen_profiles and seen_profiles[profile] != key:
                raise ValueError("profile_shared_between_member_generations")
            seen_profiles[profile] = key
        self.db_path = directory / "hermes_session_bindings.sqlite"
        if self.db_path.is_symlink():
            raise ValueError("private_binding_database_required")
        with self._db() as db:
            os.chmod(self.db_path, 0o600, follow_symlinks=False)
            db.execute("""CREATE TABLE IF NOT EXISTS bindings (
                member_id TEXT NOT NULL, generation INTEGER NOT NULL,
                owner_profile TEXT NOT NULL, owner_session TEXT NOT NULL,
                hermes_profile TEXT NOT NULL, request_id TEXT NOT NULL,
                hermes_session_id TEXT, state TEXT NOT NULL CHECK(state IN ('creating','ready')),
                PRIMARY KEY(member_id,generation,owner_profile,owner_session),
                UNIQUE(hermes_profile,hermes_session_id), UNIQUE(hermes_profile,request_id))""")

    @contextmanager
    def _db(self):
        import sqlite3
        db = sqlite3.connect(str(self.db_path), timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _request_id(self, member_id, generation, owner_profile, owner_session):
        identity = json.dumps([self.registry.profile_id, member_id, generation,
                               owner_profile, owner_session], separators=(",", ":"))
        return "colleague-session-" + hashlib.sha256(identity.encode()).hexdigest()

    @contextmanager
    def _binding_lock(self, key):
        name = hashlib.sha256(json.dumps(key, separators=(",", ":")).encode()).hexdigest()
        path = self.state_dir / (name + ".binding.lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1
                    or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
                raise ValueError("private_binding_lock_required")
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("session_binding_busy") from None
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)

    def resolve(self, member_id: str, generation: int, owner_profile: str,
                owner_session: str, *, allow_draining: bool = False) -> ProfileBinding:
        if (not isinstance(owner_profile, str) or not _IDENTITY.fullmatch(owner_profile)
                or not isinstance(owner_session, str) or not _IDENTITY.fullmatch(owner_session)):
            raise ValueError("trusted_owner_required")
        member = self.registry.role_for_member(
            supervisor=self.supervisor, member_id=member_id, generation=generation)
        allowed_statuses = {"active", "draining"} if allow_draining else {"active"}
        if member["status"] not in allowed_statuses:
            raise PermissionError("member_not_accepting_work")
        key = (member_id, generation, owner_profile, owner_session)
        profile = self.profiles.get((member_id, generation))
        if profile is None:
            raise ValueError("member_profile_binding_missing")
        request_id = self._request_id(*key)
        with self._binding_lock(key):
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("""SELECT * FROM bindings
                    WHERE member_id=? AND generation=? AND owner_profile=? AND owner_session=?""", key).fetchone()
                if row is not None:
                    if (row["hermes_profile"], row["request_id"]) != (profile, request_id):
                        raise ValueError("session_binding_conflict")
                    if row["state"] == "ready":
                        return ProfileBinding(member_id, generation, owner_profile, owner_session,
                                              profile, row["hermes_session_id"])
                else:
                    db.execute("""INSERT INTO bindings
                        (member_id,generation,owner_profile,owner_session,hermes_profile,request_id,state)
                        VALUES(?,?,?,?,?,?, 'creating')""", (*key, profile, request_id))
            try:
                session_id = self.creator.create(profile, request_id)
            except BaseException as exc:
                raise HermesSessionCreationPending(
                    "session.create outcome unknown; retry the same binding to recover") from exc
            if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
                raise HermesSessionCreationPending("session.create returned no canonical Hermes session ID")
            try:
                with self._db() as db:
                    changed = db.execute("""UPDATE bindings SET hermes_session_id=?,state='ready'
                        WHERE member_id=? AND generation=? AND owner_profile=? AND owner_session=?
                          AND hermes_profile=? AND request_id=? AND state='creating'""",
                        (session_id, *key, profile, request_id))
                    if changed.rowcount != 1:
                        raise RuntimeError("session_binding_state_changed")
            except Exception as exc:
                # The native create has a stable idempotency key; retain the creating
                # row so a restart can recover its exact result.
                raise HermesSessionCreationPending("session binding persistence failed; retry same binding") from exc
            return ProfileBinding(member_id, generation, owner_profile, owner_session, profile, session_id)


class HermesColleagueRunner:
    """Run one Hermes turn after host identity, readiness and admission checks.

    The launcher receives an argv vector and input text; it must not use a shell.
    A call is never automatically replayed. Unknown outcomes leave the reservation
    open and quarantine the session until explicit host reconciliation.
    """

    def __init__(self, *, registry, supervisor: Supervisor, provisioner, bindings: Mapping,
                 executable: str | Path, state_dir: str | Path,
                 launcher: Callable | None = None, timeout: float = 180,
                 session_lock_timeout: float = 300):
        if type(supervisor) is not Supervisor or supervisor.profile_id != registry.profile_id:
            raise PermissionError("trusted_supervisor_required")
        if not callable(getattr(provisioner, "status", None)):
            raise ValueError("provisioner_required")
        exe = Path(executable)
        if not exe.is_absolute() or not exe.is_file():
            raise ValueError("absolute_hermes_executable_required")
        state = Path(state_dir)
        if not state.is_absolute() or state.is_symlink() or not state.is_dir():
            raise ValueError("private_state_directory_required")
        state_info = state.stat()
        if (state_info.st_mode & 0o077 or (hasattr(os, "getuid") and state_info.st_uid != os.getuid())):
            raise ValueError("private_state_directory_required")
        for value, code in ((timeout, "invalid_timeout"), (session_lock_timeout, "invalid_session_lock_timeout")):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(code)
        self.binding_store = bindings if callable(getattr(bindings, "resolve", None)) else None
        checked_bindings = {} if self.binding_store is not None else dict(bindings)
        sessions = set()
        profiles = {}
        for key, binding in checked_bindings.items():
            if (not isinstance(binding, ProfileBinding)
                    or key != (binding.member_id, binding.generation,
                               binding.owner_profile, binding.owner_session)):
                raise ValueError("invalid_profile_binding_map")
            session_key = (binding.hermes_profile, binding.hermes_session_id)
            if session_key in sessions:
                raise ValueError("duplicate_profile_session_binding")
            sessions.add(session_key)
            member_key = (binding.member_id, binding.generation)
            prior = profiles.get(member_key)
            if prior is not None and prior != binding.hermes_profile:
                raise ValueError("member_profile_binding_conflict")
            if any(other[0] != member_key[0] or other[1] != member_key[1]
                   for other, profile in profiles.items() if profile == binding.hermes_profile):
                raise ValueError("profile_shared_between_members")
            profiles[member_key] = binding.hermes_profile
        self.registry, self.supervisor, self.provisioner = registry, supervisor, provisioner
        self.bindings, self.executable, self.state_dir = checked_bindings, str(exe), state
        self.launcher = launcher or _launch
        self._builtin_launcher = launcher is None
        self.timeout = float(timeout)
        self.session_lock_timeout = float(session_lock_timeout)

    @property
    def attempt_containment(self) -> str:
        """What this runner can prove about an attempt it has already started.

        ``gateway_delegated``: the built-in launcher runs ``hermes chat``, and
        that CLI never builds an agent or runs a tool — it submits the turn to a
        long-lived gateway and replays its events. Every process the turn starts
        is a child of that gateway, so it never inherits this attempt's
        environment token and no sweep from here can see it. The launcher can
        prove that the client exited, never that the attempt did.

        ``host_owned``: a host-injected launcher keeps ownership of the proof.
        Neither scope may be treated as whole-attempt containment by a queue
        worker; an unsupervised worker must refuse both.
        """
        return "gateway_delegated" if self._builtin_launcher else "host_owned"

    def _resolve_binding(self, member_id, generation, owner_profile, owner_session, *, allow_draining=False):
        if self.binding_store is not None:
            return self.binding_store.resolve(member_id, generation, owner_profile, owner_session,
                                              allow_draining=allow_draining)
        key = (member_id, generation, owner_profile, owner_session)
        return self.bindings.get(key)

    def _key(self, binding):
        return hashlib.sha256(
            json.dumps([binding.hermes_profile, binding.hermes_session_id],
                       separators=(",", ":")).encode()
        ).hexdigest()

    @contextmanager
    def _member_lock(self, member_id, generation):
        # Lock order is always member/generation first, then Hermes session.
        name = hashlib.sha256(
            json.dumps([self.registry.profile_id, member_id, generation],
                       separators=(",", ":")).encode()
        ).hexdigest()
        path = self.state_dir / (name + ".member.lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1
                    or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
                raise ValueError("private_member_lock_required")
            deadline = time.monotonic() + self.session_lock_timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("colleague_member_busy") from None
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)

    @contextmanager
    def _session_lock(self, binding):
        path = self.state_dir / (self._key(binding) + ".lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1
                    or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
                raise ValueError("private_session_lock_required")
            deadline = time.monotonic() + self.session_lock_timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("hermes_session_busy") from None
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)

    def _uncertain_path(self, binding):
        return self.state_dir / (self._key(binding) + ".uncertain.json")

    def _read_uncertain(self, binding):
        path = self._uncertain_path(binding)
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096
                    or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
                raise ValueError("private_uncertain_record_required")
            return json.load(stream)

    def _mark_uncertain(self, binding, work_id, request_id):
        record = {"member_id": binding.member_id, "generation": binding.generation,
                  "work_id": work_id, "request_id": request_id,
                  "hermes_profile": binding.hermes_profile, "hermes_session_id": binding.hermes_session_id}
        fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix=".uncertain-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self._uncertain_path(binding))
            directory_fd = os.open(self.state_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def run(self, *, member_id: str, generation: int, request_id: str, prompt: str,
            owner_profile: str, owner_session: str) -> dict:
        # Refuse before a reservation exists: this launcher cannot produce
        # whole-attempt evidence, so a started turn could never be settled.
        if self.attempt_containment == "gateway_delegated":
            raise HermesExecutionError(
                "hermes_turn_runs_in_shared_gateway; attempt_containment_unprovable")
        if not isinstance(member_id, str) or not _MEMBER.fullmatch(member_id):
            raise ValueError("invalid_member_id")
        if type(generation) is not int or generation < 1:
            raise ValueError("invalid_generation")
        if not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id):
            raise ValueError("invalid_request_id")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
            raise ValueError("invalid_prompt")
        if (not isinstance(owner_profile, str) or not _IDENTITY.fullmatch(owner_profile)
                or not isinstance(owner_session, str) or not _IDENTITY.fullmatch(owner_session)):
            raise ValueError("trusted_owner_required")

        work_id = hashlib.sha256(
            json.dumps([self.registry.profile_id, member_id, generation,
                        owner_profile, owner_session, request_id],
                       separators=(",", ":")).encode()
        ).hexdigest()[:32]

        # Reserve before creating a Hermes session. Deactivation finalization waits
        # for this durable open reservation, so creation is always tracked.
        with self._member_lock(member_id, generation):
            member = self.registry.role_for_member(
                supervisor=self.supervisor, member_id=member_id, generation=generation)
            if member["status"] != "active":
                raise PermissionError("member_not_accepting_work")
            work = self.registry.work_status(
                supervisor=self.supervisor, member_id=member_id, generation=generation)
            if work["tracked_open"]:
                raise RuntimeError("member_has_unresolved_work")
            ready = self.provisioner.status(member_id, generation)
            if not isinstance(ready, dict) or ready.get("ready") is not True:
                raise RuntimeError("member_not_ready")
            reservation = self.registry.reserve_work_exclusive(
                supervisor=self.supervisor, member_id=member_id,
                generation=generation, work_id=work_id)
            if reservation.get("created") is not True:
                raise RuntimeError("work_already_reserved")

            # The binding resolver may create a remote session. Any exception leaves
            # the durable reservation open for same-owner host reconciliation.
            binding = self._resolve_binding(member_id, generation, owner_profile, owner_session,
                                             allow_draining=True)
            key = (member_id, generation, owner_profile, owner_session)
            if not isinstance(binding, ProfileBinding) or (
                    binding.member_id, binding.generation, binding.owner_profile,
                    binding.owner_session) != key:
                raise ValueError("member_owner_session_binding_missing")

            with self._session_lock(binding):
                if self._read_uncertain(binding) is not None:
                    raise RuntimeError("hermes_session_outcome_uncertain")
                assignment = (
                    f"Host assignment: member_id={member_id}; generation={generation}. "
                    f"Your registered duty is context: {member['duty']}. This duty does not restrict "
                    "your general Hermes capabilities or tools.\n\nTask:\n" + prompt.strip()
                )
                argv = [self.executable, "--profile", binding.hermes_profile,
                        "chat", "--query-file", "-", "--oneshot", "-Q",
                        "--resume", binding.hermes_session_id]
                started = time.monotonic()
                # Crash-safe uncertainty fence written before launching any turn.
                self._mark_uncertain(binding, work_id, request_id)
                try:
                    output = self.launcher(argv, input=assignment, timeout=self.timeout)
                except BaseException as exc:
                    raise HermesExecutionError("hermes_outcome_unknown; reservation retained") from exc
                if not isinstance(output, ProcessResult):
                    raise HermesExecutionError("invalid_launcher_result; reservation retained")
                if output.timed_out:
                    raise HermesExecutionError("hermes_turn_timeout; reservation retained")
                if output.returncode != 0:
                    raise HermesExecutionError("hermes_turn_failed; reservation retained")
                # Process exit is not attempt completion: descendants that left the
                # process group can still be running. Settle only on proven emptiness,
                # otherwise the reservation and the uncertainty marker stay in place.
                evidence = output.containment
                evidence_id = containment.evidence_id(evidence)
                if output.containment_scope != "attempt" or evidence_id is None:
                    raise HermesExecutionError(
                        "hermes_attempt_scope_unproven; reservation retained")
                if output.quiescent is not True:
                    raise HermesExecutionError("hermes_attempt_not_quiescent; reservation retained")
                self.registry.settle_work(supervisor=self.supervisor, member_id=member_id,
                                          generation=generation, work_id=work_id)
                marker = self._uncertain_path(binding)
                try:
                    marker.unlink()
                    directory_fd = os.open(self.state_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except FileNotFoundError:
                    pass
                return {
                    "execution_state": "hermes_process_succeeded",
                    "request_id": request_id,
                    "work_id": work_id,
                    "requested_by": {"profile": owner_profile, "session": owner_session},
                    "executed_by": {"member_id": member_id, "generation": generation,
                                    "hermes_profile": binding.hermes_profile,
                                    "hermes_session_id": binding.hermes_session_id},
                    "cli_stdout_unparsed": output.stdout[-64000:],
                    "cli_stdout_is_parsed_answer": False,
                    "task_result_status": "output_unparsed",
                    "quiescence_evidence_id": evidence_id,
                    "attempt_containment": self.attempt_containment,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }

    def reconcile_uncertain(self, *, member_id: str, generation: int, request_id: str,
                            owner_profile: str, owner_session: str,
                            process_quiesced: bool, side_effects_reviewed: bool) -> dict:
        """Host-only explicit resolution after checking process and external effects."""
        if process_quiesced is not True or side_effects_reviewed is not True:
            raise PermissionError("explicit_quiescence_and_effect_review_required")
        with self._member_lock(member_id, generation):
            # Allow recovery while draining: the open reservation prevents final
            # deactivation, and the same durable request ID recovers the exact session.
            binding = self._resolve_binding(member_id, generation, owner_profile, owner_session,
                                            allow_draining=True)
            with self._session_lock(binding):
                work_id = hashlib.sha256(
                    json.dumps([self.registry.profile_id, member_id, generation,
                                owner_profile, owner_session, request_id],
                               separators=(",", ":")).encode()
                ).hexdigest()[:32]
                record = self._read_uncertain(binding)
                if record is not None and (record.get("work_id"), record.get("request_id")) != (work_id, request_id):
                    raise ValueError("uncertain_record_mismatch")
                self.registry.settle_work(supervisor=self.supervisor, member_id=member_id,
                                          generation=generation, work_id=work_id)
                path = self._uncertain_path(binding)
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                return {"resolved": True, "work_id": work_id, "retry_permitted": False}


def _launch(argv: list[str], *, input: str, timeout: float) -> ProcessResult:
    """Launch Hermes chat from stdin, without shell interpolation, then sweep the client tree.

    The child gets its own session, so a timeout kills Hermes and its group
    without touching the caller. Descendants that leave that group are found by
    the attempt token and swept.

    The result's scope is ``cli_client``, never ``attempt``: ``hermes chat``
    submits the turn to a long-lived gateway and runs no tool itself, so the
    processes the turn actually starts are gateway children that never inherit
    this environment. An empty sweep here means the client exited, nothing more.
    """
    # Reuse the token minted by the queue for this attempt, so a supervisor that
    # outlives this call can still find and stop the same descendants.
    try:
        token = containment.checked_token(os.environ.get(containment.ENV))
    except ValueError:
        token = containment.new_token()
    env = dict(os.environ)
    env[containment.ENV] = token
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True,
                            close_fds=True, env=env)
    timed_out = False
    evidence = None
    try:
        try:
            stdout, stderr = proc.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                # An escaped descendant can still hold our stdout/stderr pipes;
                # end the attempt before the final read, or this waits on a
                # process that was already decided against.
                evidence = containment.sweep(token)
                stdout, stderr = proc.communicate()
    finally:
        # Always runs: a launcher exception must not leave an attempt behind.
        if evidence is None:
            evidence = containment.sweep(token)
    return ProcessResult(proc.returncode, stdout, stderr, timed_out=timed_out,
                         quiescent=False, containment=evidence,
                         containment_scope="cli_client")
