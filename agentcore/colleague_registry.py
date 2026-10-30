"""Durable colleague membership and retirement primitives.

The host authenticates every ``Actor`` and ``Supervisor`` and supplies it out
of band. These Python types are not authentication against arbitrary Python
code; an adapter must never construct either from model-supplied arguments.

Membership does not start or stop a worker. A supervisor must fence *all*
admissions with ``reserve_work`` and close reservations only after durable
settlement. Retirement additionally needs the supervisor's authoritative
external-idle observation. The registry must not be used to retire production
workers until that admission fence is integrated end to end.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
from typing import NamedTuple
import unicodedata


_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_ROLE_ID = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_REQUEST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_RUNTIME = frozenset({"unknown", "starting", "running", "stopping", "stopped", "error"})
_MAX_OPEN_WORK = 1024
_MAX_ROLES = 256
_MAX_MEMBERS = 4096
_INIT_LOCK = threading.Lock()


class Actor(NamedTuple):
    """Identity bound by an authenticated host, never parsed from a model call."""
    profile_id: str
    member_id: str


class Supervisor(NamedTuple):
    """Host-bound lifecycle and work observation authority, not a model grant."""
    profile_id: str


def _identity(value, name):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("invalid_" + name)
    return value


def _request(value):
    if not isinstance(value, str) or not _REQUEST.fullmatch(value):
        raise ValueError("invalid_request_id")
    return value


def _text(value, maximum, error):
    if not isinstance(value, str):
        raise ValueError(error)
    value = value.strip()
    if not value or len(value) > maximum or any(unicodedata.category(c).startswith("C") for c in value):
        raise ValueError(error)
    return value


def _label(value):
    return _text(value, 80, "invalid_display_name")


def _role(value):
    if not isinstance(value, str) or not _ROLE_ID.fullmatch(value):
        raise ValueError("invalid_role_id")
    return value


def _boundaries(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 6:
        raise ValueError("invalid_role_boundaries")
    result = [_text(item, 180, "invalid_role_boundary") for item in value]
    if len(set(result)) != len(result):
        raise ValueError("duplicate_role_boundary")
    return result


class Registry:
    """One profile's persisted directory; workers and queues stay host-owned."""

    def __init__(self, path, *, profile_id, primary_id, primary_name="Primary"):
        self.path = Path(path)
        if not self.path.is_absolute() or not self.path.parent.is_dir() or self.path.is_symlink():
            raise ValueError("absolute_registry_path_required")
        parent_info = self.path.parent.stat()
        if parent_info.st_mode & 0o077 or (hasattr(os, "getuid") and parent_info.st_uid != os.getuid()):
            raise ValueError("private_registry_directory_required")
        self.profile_id = _identity(profile_id, "profile_id")
        primary_id = _identity(primary_id, "primary_id")
        primary_name = _label(primary_name)
        # Never raw-open/close a SQLite database: same-process raw close drops
        # POSIX locks held by other connections. SQLite creates the file inside
        # an owner-only directory, and chmod does not open or close a database FD.
        with _INIT_LOCK:
            self._initialize(primary_id, primary_name)

    def _initialize(self, primary_id, primary_name):
        if self.path.is_symlink():
            raise ValueError("absolute_registry_path_required")
        if self.path.exists():
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError("private_registry_file_required")
        with self._db() as db:
            os.chmod(self.path, 0o600, follow_symlinks=False)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS colleague_profiles (
                  profile_id TEXT PRIMARY KEY, primary_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS colleague_roles (
                  profile_id TEXT NOT NULL, role_id TEXT NOT NULL, label TEXT NOT NULL,
                  duty TEXT NOT NULL, boundaries TEXT NOT NULL,
                  PRIMARY KEY(profile_id,role_id),
                  FOREIGN KEY(profile_id) REFERENCES colleague_profiles(profile_id));
                CREATE TABLE IF NOT EXISTS colleague_members (
                  profile_id TEXT NOT NULL, member_id TEXT NOT NULL, role_id TEXT NOT NULL,
                  display_name TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','draining','inactive')),
                  generation INTEGER NOT NULL CHECK(generation > 0),
                  can_manage INTEGER NOT NULL DEFAULT 0 CHECK(can_manage IN (0,1)),
                  PRIMARY KEY(profile_id,member_id),
                  FOREIGN KEY(profile_id,role_id) REFERENCES colleague_roles(profile_id,role_id));
                CREATE TABLE IF NOT EXISTS colleague_runtime (
                  profile_id TEXT NOT NULL, member_id TEXT NOT NULL, generation INTEGER NOT NULL,
                  state TEXT NOT NULL, PRIMARY KEY(profile_id,member_id),
                  FOREIGN KEY(profile_id,member_id) REFERENCES colleague_members(profile_id,member_id));
                CREATE TABLE IF NOT EXISTS colleague_work (
                  profile_id TEXT NOT NULL, member_id TEXT NOT NULL, generation INTEGER NOT NULL,
                  work_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('open','closed')),
                  PRIMARY KEY(profile_id,member_id,generation,work_id),
                  FOREIGN KEY(profile_id,member_id) REFERENCES colleague_members(profile_id,member_id));
                CREATE TABLE IF NOT EXISTS colleague_exclusive_work (
                  profile_id TEXT NOT NULL, member_id TEXT NOT NULL, generation INTEGER NOT NULL,
                  work_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('open','closed')),
                  PRIMARY KEY(profile_id,member_id),
                  FOREIGN KEY(profile_id,member_id) REFERENCES colleague_members(profile_id,member_id));
                CREATE TABLE IF NOT EXISTS colleague_history (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL,
                  action TEXT NOT NULL, actor_id TEXT NOT NULL, target_id TEXT NOT NULL,
                  generation INTEGER, detail TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS colleague_commands (
                  profile_id TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                  response TEXT NOT NULL, PRIMARY KEY(profile_id,request_id));
                CREATE TRIGGER IF NOT EXISTS protect_primary_delete BEFORE DELETE ON colleague_members
                WHEN OLD.member_id = (SELECT primary_id FROM colleague_profiles WHERE profile_id=OLD.profile_id)
                BEGIN SELECT RAISE(ABORT,'primary_protected'); END;
                CREATE TRIGGER IF NOT EXISTS protect_primary_update BEFORE UPDATE ON colleague_members
                WHEN OLD.member_id = (SELECT primary_id FROM colleague_profiles WHERE profile_id=OLD.profile_id)
                  AND (NEW.status != 'active' OR NEW.member_id != OLD.member_id OR NEW.profile_id != OLD.profile_id
                       OR NEW.role_id != 'primary' OR NEW.generation != OLD.generation)
                BEGIN SELECT RAISE(ABORT,'primary_protected'); END;
                CREATE TRIGGER IF NOT EXISTS protect_profile_update BEFORE UPDATE ON colleague_profiles
                BEGIN SELECT RAISE(ABORT,'primary_protected'); END;
                CREATE TRIGGER IF NOT EXISTS protect_profile_delete BEFORE DELETE ON colleague_profiles
                BEGIN SELECT RAISE(ABORT,'primary_protected'); END;
                CREATE TRIGGER IF NOT EXISTS protect_primary_role_delete BEFORE DELETE ON colleague_roles
                WHEN OLD.role_id='primary' BEGIN SELECT RAISE(ABORT,'primary_protected'); END;
                CREATE TRIGGER IF NOT EXISTS protect_primary_role_update BEFORE UPDATE ON colleague_roles
                WHEN OLD.role_id='primary' BEGIN SELECT RAISE(ABORT,'primary_protected'); END;
                CREATE TRIGGER IF NOT EXISTS protect_profile_replace BEFORE INSERT ON colleague_profiles
                WHEN EXISTS(SELECT 1 FROM colleague_profiles WHERE profile_id=NEW.profile_id)
                BEGIN SELECT RAISE(ABORT,'primary_protected'); END;
                CREATE TRIGGER IF NOT EXISTS protect_primary_replace BEFORE INSERT ON colleague_members
                WHEN NEW.member_id=(SELECT primary_id FROM colleague_profiles WHERE profile_id=NEW.profile_id)
                  AND EXISTS(SELECT 1 FROM colleague_members WHERE profile_id=NEW.profile_id AND member_id=NEW.member_id)
                BEGIN SELECT RAISE(ABORT,'primary_protected'); END;
                CREATE TRIGGER IF NOT EXISTS protect_primary_role_replace BEFORE INSERT ON colleague_roles
                WHEN NEW.role_id='primary' AND EXISTS(SELECT 1 FROM colleague_roles WHERE profile_id=NEW.profile_id AND role_id='primary')
                BEGIN SELECT RAISE(ABORT,'primary_protected'); END;
            """)
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT primary_id FROM colleague_profiles WHERE profile_id=?", (self.profile_id,)).fetchone()
            if row is None:
                db.execute("INSERT INTO colleague_profiles VALUES (?,?)", (self.profile_id, primary_id))
                row = db.execute("SELECT primary_id FROM colleague_profiles WHERE profile_id=?", (self.profile_id,)).fetchone()
            if row["primary_id"] != primary_id:
                raise ValueError("primary_identity_mismatch")
            if db.execute("SELECT 1 FROM colleague_roles WHERE profile_id=? AND role_id='primary'", (self.profile_id,)).fetchone() is None:
                db.execute("INSERT INTO colleague_roles VALUES (?, 'primary', ?, ?, ?)",
                           (self.profile_id, primary_name, "Coordinate work and protect the primary identity.",
                            json.dumps(["Primary identity cannot be removed."])))
            if db.execute("SELECT 1 FROM colleague_members WHERE profile_id=? AND member_id=?", (self.profile_id, primary_id)).fetchone() is None:
                db.execute("INSERT INTO colleague_members VALUES (?,?,'primary',?,'active',1,0)",
                           (self.profile_id, primary_id, primary_name))
            row = self._member(db, primary_id)
            if row["role_id"] != "primary" or row["status"] != "active":
                raise RuntimeError("primary_invariant_failed")
            db.execute("INSERT OR IGNORE INTO colleague_runtime VALUES (?,?,1,'unknown')", (self.profile_id, primary_id))

    @contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _primary(self, db):
        return db.execute("SELECT primary_id FROM colleague_profiles WHERE profile_id=?", (self.profile_id,)).fetchone()[0]

    def _member(self, db, member_id):
        row = db.execute("SELECT * FROM colleague_members WHERE profile_id=? AND member_id=?",
                         (self.profile_id, member_id)).fetchone()
        if row is None:
            raise ValueError("unknown_member")
        return row

    def _reader(self, db, actor):
        if type(actor) is not Actor or actor.profile_id != self.profile_id:
            raise PermissionError("trusted_actor_required")
        _identity(actor.member_id, "actor_id")
        row = self._member(db, actor.member_id)
        if row["status"] != "active":
            raise PermissionError("active_member_required")
        return row

    def _actor(self, db, actor, *, primary_only=False):
        row = self._reader(db, actor)
        primary = actor.member_id == self._primary(db)
        if (primary_only and not primary) or (not primary and not row["can_manage"]):
            raise PermissionError("management_permission_required")
        return actor.member_id

    def _host(self, supervisor):
        if type(supervisor) is not Supervisor or supervisor.profile_id != self.profile_id:
            raise PermissionError("trusted_supervisor_required")

    def _primary_target(self, db, member_id):
        if member_id == self._primary(db):
            raise PermissionError("primary_protected")

    def _event(self, db, action, actor, target, generation=None, detail=None):
        db.execute("INSERT INTO colleague_history(profile_id,action,actor_id,target_id,generation,detail) VALUES (?,?,?,?,?,?)",
                   (self.profile_id, action, actor, target, generation, json.dumps(detail or {}, sort_keys=True)))

    def _replay(self, db, request_id, actor, action, fields):
        _request(request_id)
        encoded = json.dumps([actor, action, fields], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        row = db.execute("SELECT fingerprint,response FROM colleague_commands WHERE profile_id=? AND request_id=?",
                         (self.profile_id, request_id)).fetchone()
        if row:
            if row["fingerprint"] != fingerprint:
                raise ValueError("request_id_conflict")
            response = json.loads(row["response"])
            response["replayed"] = True
            return response
        return None

    def _command(self, db, request_id, actor, action, fields, change):
        replay = self._replay(db, request_id, actor, action, fields)
        if replay is not None:
            return replay
        encoded = json.dumps([actor, action, fields], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        response = change()
        response["replayed"] = False
        db.execute("INSERT INTO colleague_commands VALUES (?,?,?,?)",
                   (self.profile_id, request_id, fingerprint, json.dumps(response, ensure_ascii=False)))
        return response

    def define_role(self, *, actor, role_id, label, duty, boundaries, request_id):
        role_id = _role(role_id)
        label = _label(label)
        duty = _text(duty, 300, "invalid_role_duty")
        boundaries = _boundaries(boundaries)
        encoded_boundaries = json.dumps(boundaries, ensure_ascii=False, separators=(",", ":"))
        if role_id == "primary":
            raise PermissionError("primary_protected")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            caller = self._actor(db, actor)
            def apply():
                row = db.execute("SELECT label,duty,boundaries FROM colleague_roles WHERE profile_id=? AND role_id=?",
                                 (self.profile_id, role_id)).fetchone()
                if row and (row["label"], row["duty"], row["boundaries"]) != (label, duty, encoded_boundaries):
                    raise ValueError("role_definition_conflict")
                if not row:
                    count = db.execute("SELECT count(*) FROM colleague_roles WHERE profile_id=?", (self.profile_id,)).fetchone()[0]
                    if count >= _MAX_ROLES:
                        raise ValueError("role_capacity")
                    db.execute("INSERT INTO colleague_roles VALUES (?,?,?,?,?)",
                               (self.profile_id, role_id, label, duty, encoded_boundaries))
                    self._event(db, "role_defined", caller, role_id)
                return {"role_id": role_id, "created": row is None}
            return self._command(db, request_id, caller, "define_role", [role_id, label, duty, boundaries], apply)

    def hire(self, *, actor, member_id, role_id, display_name, request_id):
        member_id, role_id = _identity(member_id, "member_id"), _role(role_id)
        display_name = _label(display_name)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            caller = self._actor(db, actor)
            self._primary_target(db, member_id)
            if role_id == "primary":
                raise PermissionError("primary_protected")
            def apply():
                if db.execute("SELECT 1 FROM colleague_roles WHERE profile_id=? AND role_id=?",
                              (self.profile_id, role_id)).fetchone() is None:
                    raise ValueError("unknown_role")
                row = db.execute("SELECT * FROM colleague_members WHERE profile_id=? AND member_id=?",
                                 (self.profile_id, member_id)).fetchone()
                if row:
                    if row["role_id"] != role_id or row["display_name"] != display_name or row["status"] != "active":
                        raise ValueError("member_identity_conflict")
                    return {"member_id": member_id, "status": "active", "generation": row["generation"], "created": False}
                count = db.execute("SELECT count(*) FROM colleague_members WHERE profile_id=?", (self.profile_id,)).fetchone()[0]
                if count >= _MAX_MEMBERS:
                    raise ValueError("member_capacity")
                db.execute("INSERT INTO colleague_members VALUES (?,?,?,?,'active',1,0)",
                           (self.profile_id, member_id, role_id, display_name))
                db.execute("INSERT INTO colleague_runtime VALUES (?,?,1,'unknown')", (self.profile_id, member_id))
                self._event(db, "member_hired", caller, member_id, 1, {"role_id": role_id})
                return {"member_id": member_id, "status": "active", "generation": 1, "created": True}
            return self._command(db, request_id, caller, "hire", [member_id, role_id, display_name], apply)

    def grant_management(self, *, actor, member_id, enabled, request_id):
        member_id = _identity(member_id, "member_id")
        if type(enabled) is not bool:
            raise ValueError("invalid_permission")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            caller = self._actor(db, actor)
            self._primary_target(db, member_id)
            def apply():
                row = self._member(db, member_id)
                if row["status"] != "active":
                    raise ValueError("member_not_active")
                changed = bool(row["can_manage"]) != enabled
                if changed:
                    db.execute("UPDATE colleague_members SET can_manage=? WHERE profile_id=? AND member_id=?",
                               (int(enabled), self.profile_id, member_id))
                    self._event(db, "management_granted" if enabled else "management_revoked",
                                caller, member_id, row["generation"])
                return {"member_id": member_id, "can_manage": enabled, "changed": changed}
            return self._command(db, request_id, caller, "grant_management", [member_id, enabled], apply)

    def request_deactivation(self, *, actor, member_id, request_id):
        member_id = _identity(member_id, "member_id")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            # A manager may retire themselves. Once draining, their authority is
            # revoked, but the exact original self-retirement request remains
            # replayable without granting any new management action.
            if type(actor) is Actor and actor.profile_id == self.profile_id and actor.member_id == member_id:
                self._primary_target(db, member_id)
                row = self._member(db, member_id)
                if row["status"] != "active":
                    replay = self._replay(db, request_id, member_id, "request_deactivation", [member_id])
                    if replay is not None:
                        return replay
                    raise PermissionError("management_permission_required")
            caller = self._actor(db, actor)
            self._primary_target(db, member_id)
            def apply():
                row = self._member(db, member_id)
                if row["status"] == "active":
                    db.execute("UPDATE colleague_members SET status='draining',can_manage=0 WHERE profile_id=? AND member_id=?",
                               (self.profile_id, member_id))
                    self._event(db, "deactivation_requested", caller, member_id, row["generation"])
                    status = "draining"
                else:
                    status = row["status"]
                return {"member_id": member_id, "status": status, "generation": row["generation"]}
            return self._command(db, request_id, caller, "request_deactivation", [member_id], apply)

    def observe_runtime(self, *, supervisor, member_id, generation, state):
        self._host(supervisor)
        member_id = _identity(member_id, "member_id")
        if type(generation) is not int or generation < 1 or not isinstance(state, str) or state not in _RUNTIME:
            raise ValueError("invalid_runtime_observation")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._member(db, member_id)
            if row["generation"] != generation:
                raise ValueError("stale_member_generation")
            old = db.execute("SELECT state FROM colleague_runtime WHERE profile_id=? AND member_id=?",
                             (self.profile_id, member_id)).fetchone()[0]
            db.execute("UPDATE colleague_runtime SET state=?,generation=? WHERE profile_id=? AND member_id=?",
                       (state, generation, self.profile_id, member_id))
            if old != state:
                self._event(db, "runtime_observed", "host:supervisor", member_id, generation, {"state": state})
            return {"member_id": member_id, "generation": generation, "runtime_state": state}

    def reserve_work(self, *, supervisor, member_id, generation, work_id):
        self._host(supervisor)
        member_id, work_id = _identity(member_id, "member_id"), _request(work_id)
        if type(generation) is not int or generation < 1:
            raise ValueError("invalid_generation")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._member(db, member_id)
            if row["generation"] != generation:
                raise ValueError("stale_member_generation")
            exclusive = db.execute("SELECT work_id,status FROM colleague_exclusive_work WHERE profile_id=? AND member_id=?",
                                   (self.profile_id, member_id)).fetchone()
            if exclusive is not None and exclusive["status"] == "open":
                raise ValueError("member_has_exclusive_work")
            existing = db.execute("SELECT status FROM colleague_work WHERE profile_id=? AND member_id=? AND generation=? AND work_id=?",
                                  (self.profile_id, member_id, generation, work_id)).fetchone()
            if existing:
                if existing["status"] == "closed":
                    raise ValueError("settled_work_cannot_reopen")
                return {"work_id": work_id, "reserved": True, "created": False}
            if row["status"] != "active":
                raise ValueError("member_not_accepting_work")
            count = db.execute("SELECT count(*) FROM colleague_work WHERE profile_id=? AND member_id=? AND status='open'",
                               (self.profile_id, member_id)).fetchone()[0]
            if count >= _MAX_OPEN_WORK:
                raise ValueError("member_work_capacity")
            db.execute("INSERT INTO colleague_work VALUES (?,?,?,?,'open')", (self.profile_id, member_id, generation, work_id))
            self._event(db, "work_reserved", "host:supervisor", member_id, generation, {"work_id": work_id})
            return {"work_id": work_id, "reserved": True, "created": True}

    def reserve_work_exclusive(self, *, supervisor, member_id, generation, work_id):
        """Atomically reserve a member's sole open execution across host paths.

        Unlike reserve_work (which retains its historical capacity semantics), this
        entry point serializes Gateway and CLI runners through the Registry database.
        Repeating the exact open work_id is idempotent; every different open work
        blocks admission, regardless of which dispatcher-local lock it used.
        """
        self._host(supervisor)
        member_id, work_id = _identity(member_id, "member_id"), _request(work_id)
        if type(generation) is not int or generation < 1:
            raise ValueError("invalid_generation")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._member(db, member_id)
            if row["generation"] != generation:
                raise ValueError("stale_member_generation")
            fence = db.execute(
                "SELECT generation,work_id,status FROM colleague_exclusive_work WHERE profile_id=? AND member_id=?",
                (self.profile_id, member_id),
            ).fetchone()
            existing = db.execute(
                "SELECT status FROM colleague_work WHERE profile_id=? AND member_id=? AND generation=? AND work_id=?",
                (self.profile_id, member_id, generation, work_id),
            ).fetchone()
            if existing:
                if existing["status"] == "closed":
                    raise ValueError("settled_work_cannot_reopen")
                if fence is None:
                    # Pre-fence reservations cannot be proven to belong to this
                    # exclusive executor; keep them open for explicit reconciliation.
                    raise ValueError("exclusive_work_unfenced_reservation")
                if fence["status"] != "open" or (fence["generation"], fence["work_id"]) != (generation, work_id):
                    raise ValueError("exclusive_work_identity_mismatch")
                return {"work_id": work_id, "reserved": True, "created": False}
            if fence is not None and fence["status"] == "open":
                raise ValueError("member_has_exclusive_work")
            if row["status"] != "active":
                raise ValueError("member_not_accepting_work")
            open_row = db.execute(
                "SELECT work_id FROM colleague_work WHERE profile_id=? AND member_id=? AND status='open' LIMIT 1",
                (self.profile_id, member_id),
            ).fetchone()
            if open_row is not None:
                raise ValueError("member_has_unresolved_work")
            db.execute("INSERT INTO colleague_work VALUES (?,?,?,?,'open')",
                       (self.profile_id, member_id, generation, work_id))
            db.execute("""INSERT INTO colleague_exclusive_work(profile_id,member_id,generation,work_id,status)
                VALUES(?,?,?,?, 'open') ON CONFLICT(profile_id,member_id) DO UPDATE SET
                generation=excluded.generation,work_id=excluded.work_id,status='open'""",
                (self.profile_id, member_id, generation, work_id))
            self._event(db, "work_reserved", "host:supervisor", member_id, generation, {"work_id": work_id, "exclusive": True})
            return {"work_id": work_id, "reserved": True, "created": True}

    def settle_work(self, *, supervisor, member_id, generation, work_id):
        self._host(supervisor)
        member_id, work_id = _identity(member_id, "member_id"), _request(work_id)
        if type(generation) is not int or generation < 1:
            raise ValueError("invalid_generation")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._member(db, member_id)
            if row["generation"] != generation:
                raise ValueError("stale_member_generation")
            current = db.execute("SELECT status FROM colleague_work WHERE profile_id=? AND member_id=? AND generation=? AND work_id=?",
                                 (self.profile_id, member_id, generation, work_id)).fetchone()
            if current is None:
                raise ValueError("unknown_work")
            if current["status"] == "open":
                db.execute("UPDATE colleague_work SET status='closed' WHERE profile_id=? AND member_id=? AND generation=? AND work_id=?",
                           (self.profile_id, member_id, generation, work_id))
                db.execute("DELETE FROM colleague_exclusive_work WHERE profile_id=? AND member_id=? "
                           "AND generation=? AND work_id=? AND status='open'",
                           (self.profile_id, member_id, generation, work_id))
                self._event(db, "work_settled", "host:supervisor", member_id, generation, {"work_id": work_id})
            return {"work_id": work_id, "settled": True}

    def finalize_deactivation(self, *, supervisor, member_id, generation, external_idle):
        self._host(supervisor)
        member_id = _identity(member_id, "member_id")
        if type(generation) is not int or generation < 1 or external_idle is not True:
            raise ValueError("trusted_idle_observation_required")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._primary_target(db, member_id)
            row = self._member(db, member_id)
            if row["generation"] != generation:
                raise ValueError("stale_member_generation")
            if row["status"] == "inactive":
                return {"member_id": member_id, "status": "inactive", "generation": generation}
            if row["status"] != "draining":
                raise ValueError("member_not_draining")
            count = db.execute("SELECT count(*) FROM colleague_work WHERE profile_id=? AND member_id=? AND status='open'",
                               (self.profile_id, member_id)).fetchone()[0]
            if count:
                raise ValueError("active_work_remaining")
            db.execute("UPDATE colleague_members SET status='inactive',can_manage=0 WHERE profile_id=? AND member_id=?",
                       (self.profile_id, member_id))
            self._event(db, "deactivation_finalized", "host:supervisor", member_id, generation)
            return {"member_id": member_id, "status": "inactive", "generation": generation}

    def list_roles(self, *, actor, after="", limit=100):
        if not isinstance(after, str) or (after and not _ROLE_ID.fullmatch(after)) or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("invalid_role_window")
        with self._db() as db:
            self._reader(db, actor)
            rows = db.execute("SELECT role_id,label,duty,boundaries FROM colleague_roles WHERE profile_id=? AND role_id>? ORDER BY role_id LIMIT ?",
                              (self.profile_id, after, limit)).fetchall()
            return [{**dict(row), "boundaries": json.loads(row["boundaries"])} for row in rows]

    def role_for_member(self, *, supervisor, member_id, generation):
        """Let a trusted host build a role prompt; descriptions grant no access."""
        self._host(supervisor)
        member_id = _identity(member_id, "member_id")
        if type(generation) is not int or generation < 1:
            raise ValueError("invalid_generation")
        with self._db() as db:
            row = db.execute("""SELECT m.member_id,m.display_name,m.status,m.generation,
                r.role_id,r.label,r.duty,r.boundaries FROM colleague_members m JOIN colleague_roles r
                ON r.profile_id=m.profile_id AND r.role_id=m.role_id
                WHERE m.profile_id=? AND m.member_id=?""", (self.profile_id, member_id)).fetchone()
            if row is None:
                raise ValueError("unknown_member")
            if row["generation"] != generation:
                raise ValueError("stale_member_generation")
            return {**dict(row), "boundaries": json.loads(row["boundaries"])}

    def primary_member(self, *, supervisor):
        """Return the immutable primary ID to a trusted host supervisor."""
        self._host(supervisor)
        with self._db() as db:
            return self._primary(db)

    def is_primary_member(self, member_id: str) -> bool:
        """Check whether a given member ID is the protected primary leader bot."""
        with self._db() as db:
            return member_id == self._primary(db)

    def assert_primary_leadership(self, *, actor: Actor) -> None:
        """Assert that the authenticated actor is the primary leader bot; raise PermissionError otherwise."""
        with self._db() as db:
            self._reader(db, actor)
            if actor.member_id != self._primary(db):
                raise PermissionError("primary_leadership_required")

    def work_status(self, *, supervisor, member_id, generation):
        """Summarize registry reservations, not work outside this admission fence."""
        self._host(supervisor)
        member_id = _identity(member_id, "member_id")
        if type(generation) is not int or generation < 1:
            raise ValueError("invalid_generation")
        with self._db() as db:
            row = self._member(db, member_id)
            if row["generation"] != generation:
                raise ValueError("stale_member_generation")
            counts = {item["status"]: item["n"] for item in db.execute(
                "SELECT status,count(*) AS n FROM colleague_work WHERE profile_id=? AND member_id=? AND generation=? GROUP BY status",
                (self.profile_id, member_id, generation))}
            return {"member_id": member_id, "generation": generation, "membership_status": row["status"],
                    "tracked_open": counts.get("open", 0), "tracked_closed": counts.get("closed", 0),
                    "coverage": "registry_reservations_only"}

    def list_members(self, *, actor, after="", limit=100):
        if not isinstance(after, str) or (after and not _ID.fullmatch(after)) or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("invalid_member_window")
        with self._db() as db:
            self._reader(db, actor)
            rows = db.execute("""SELECT m.member_id,m.role_id,m.display_name,m.status,m.generation,m.can_manage,
                r.state AS runtime_state FROM colleague_members m JOIN colleague_runtime r
                ON r.profile_id=m.profile_id AND r.member_id=m.member_id
                WHERE m.profile_id=? AND m.member_id>? ORDER BY m.member_id LIMIT ?""",
                (self.profile_id, after, limit)).fetchall()
            return [{**dict(row), "can_manage": bool(row["can_manage"])} for row in rows]

    def history(self, *, actor, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("invalid_history_window")
        with self._db() as db:
            self._actor(db, actor)
            rows = db.execute("""SELECT sequence,action,actor_id,target_id,generation,detail,created_at
                FROM colleague_history WHERE profile_id=? AND sequence>? ORDER BY sequence LIMIT ?""",
                (self.profile_id, after, limit)).fetchall()
            return [dict(row) for row in rows]
