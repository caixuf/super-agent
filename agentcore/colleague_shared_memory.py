"""Trusted per-team memory layout for colleague profiles.

Team members share one long-term fact database and user_id. Turn spools and
event queues stay profile-specific. This binds paths and identity; it is not an
OS sandbox.
"""
from __future__ import annotations
from dataclasses import dataclass
import fcntl, hashlib, json, os, re, signal, subprocess, tempfile, time
from pathlib import Path
from typing import Mapping
from agentcore import colleague_containment as containment
from agentcore.colleague_registry import Registry, Supervisor

_IDENT = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")

def _key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

@dataclass(frozen=True)
class TeamMemoryBinding:
    profile: str
    team_id: str
    member_id: str
    generation: int
    session_id: str
    data_dir: Path
    spool_dir: Path
    queue_path: Path
    @property
    def user_id(self): return self.team_id
    @property
    def agent_id(self): return self.member_id

class ColleagueMemoryBindingStore:
    """Resolve host-authenticated profile/session into a private team memory layout."""
    def __init__(self, *, registry: Registry, supervisor: Supervisor,
                 profile_members: Mapping[str, tuple[str,int]], state_dir: str|Path):
        if type(registry) is not Registry or type(supervisor) is not Supervisor:
            raise TypeError("real_registry_and_supervisor_required")
        if supervisor.profile_id != registry.profile_id:
            raise PermissionError("trusted_supervisor_required")
        if not isinstance(profile_members, Mapping) or not profile_members:
            raise ValueError("profile_member_map_required")
        root=Path(state_dir)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise ValueError("private_memory_state_directory_required")
        self._check_private(root)
        self.registry,self.supervisor=registry,supervisor
        self.team_id=registry.profile_id
        registry_path=Path(registry.path)
        if registry_path.is_symlink() or not registry_path.is_file():
            raise ValueError("trusted_registry_file_required")
        self.registry_path=registry_path.resolve(strict=True)
        self._check_private(self.registry_path.parent)
        if self.registry_path.stat().st_mode&0o077:
            raise ValueError("trusted_registry_file_required")
        if not isinstance(self.team_id,str) or not _IDENT.fullmatch(self.team_id):
            raise ValueError("invalid_team_identity")
        self.state_dir=root.resolve(strict=True)
        self._profiles={}
        seen=set()
        primary=registry.primary_member(supervisor=supervisor)
        for profile,ref in profile_members.items():
            if not isinstance(profile,str) or not _IDENT.fullmatch(profile):
                raise ValueError("invalid_profile_identity")
            if (not isinstance(ref,tuple) or len(ref)!=2 or not isinstance(ref[0],str)
                or type(ref[1]) is not int or ref[1]<0):
                raise ValueError("invalid_profile_member_binding")
            member,generation=ref
            if (member==primary) != (generation==0):
                raise ValueError("invalid_primary_memory_generation")
            if generation:
                row=registry.role_for_member(supervisor=supervisor,member_id=member,generation=generation)
                if row["status"]!="active": raise PermissionError("memory_member_not_active")
            if ref in seen: raise ValueError("member_profile_binding_reused")
            seen.add(ref); self._profiles[profile]=ref
        teams_root=self.state_dir/"teams"
        self._ensure_private_dir(teams_root,self.state_dir)
        self._team_root=teams_root/_key(self.team_id)
        self._ensure_private_dir(self._team_root,teams_root)
        self._data_dir=self._team_root/"memory"
        self._ensure_private_dir(self._data_dir,self._team_root)
        self._bindings_file=self._team_root/"profile-bindings.json"
        self._write_bindings()

    @staticmethod
    def _check_private(path):
        info=path.stat()
        if info.st_mode&0o077 or (hasattr(os,"getuid") and info.st_uid!=os.getuid()):
            raise ValueError("private_memory_state_directory_required")

    @staticmethod
    def _ensure_private_dir(path,parent):
        parent=Path(parent).resolve(strict=True)
        path=Path(path)
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_dir():
                raise ValueError("memory_path_symlink_or_not_directory")
        else:
            path.mkdir(mode=0o700)
        if path.resolve(strict=True).parent!=parent:
            raise ValueError("memory_path_outside_team_root")
        os.chmod(path,0o700,follow_symlinks=False)

    def _payload(self, profiles=None):
        profiles=self._profiles if profiles is None else profiles
        return {"version":1,"team_id":self.team_id,
                "primary_id":self.registry.primary_member(supervisor=self.supervisor),
                "registry_path":str(self.registry_path),"data_dir":str(self._data_dir),
                "profiles":{p:{"member_id":m,"generation":g}
                            for p,(m,g) in sorted(profiles.items())}}

    def _locked_binding_file(self):
        lock_path=self._team_root/".bindings.lock"
        flags=os.O_CREAT|os.O_RDWR|getattr(os,"O_NOFOLLOW",0)
        fd=os.open(lock_path,flags,0o600)
        os.fchmod(fd,0o600)
        fcntl.flock(fd,fcntl.LOCK_EX)
        return fd

    def _read_binding_bytes(self):
        if self._bindings_file.is_symlink() or not self._bindings_file.is_file():
            raise ValueError("memory_binding_file_symlink_or_invalid")
        info=self._bindings_file.stat()
        if info.st_mode&0o077 or (hasattr(os,"getuid") and info.st_uid!=os.getuid()):
            raise ValueError("memory_binding_file_not_private")
        fd=os.open(self._bindings_file,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
        with os.fdopen(fd,"rb") as stream:
            return stream.read()

    def _atomic_write_bindings(self,payload):
        fd,name=tempfile.mkstemp(prefix=".profile-bindings-",dir=self._team_root)
        temp=Path(name)
        try:
            os.fchmod(fd,0o600)
            with os.fdopen(fd,"w",encoding="utf-8") as stream:
                json.dump(payload,stream,ensure_ascii=False,sort_keys=True)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temp,self._bindings_file)
            dfd=os.open(self._team_root,os.O_RDONLY|os.O_DIRECTORY)
            try: os.fsync(dfd)
            finally: os.close(dfd)
        finally:
            if temp.exists(): temp.unlink()

    def _write_bindings(self):
        payload=self._payload()
        fd=self._locked_binding_file()
        try:
            if self._bindings_file.exists() or self._bindings_file.is_symlink():
                try: old=json.loads(self._read_binding_bytes())
                except Exception as exc: raise ValueError("memory_binding_file_invalid") from exc
                if old!=payload:
                    raise ValueError("shared_team_memory_binding_conflict")
            else:
                self._atomic_write_bindings(payload)
        finally:
            fcntl.flock(fd,fcntl.LOCK_UN); os.close(fd)

    @property
    def binding_revision(self):
        return hashlib.sha256(self._read_binding_bytes()).hexdigest()

    def refresh_profile_bindings(self, profile_members, *, expected_revision):
        """Explicit host-maintenance update; construction never mutates an existing map.

        Use for explicit hires only after Registry has the active generation. Existing
        profile identities are immutable and cannot be removed or rebound here. Retiring
        a member still requires the host to stop its worker and update service lifecycle.
        """
        if not isinstance(profile_members,Mapping) or not profile_members:
            raise ValueError("profile_member_map_required")
        candidate={}; seen=set(); primary=self.registry.primary_member(supervisor=self.supervisor)
        for profile,ref in profile_members.items():
            if not isinstance(profile,str) or not _IDENT.fullmatch(profile):
                raise ValueError("invalid_profile_identity")
            if (not isinstance(ref,tuple) or len(ref)!=2 or not isinstance(ref[0],str)
                    or type(ref[1]) is not int or ref[1]<0):
                raise ValueError("invalid_profile_member_binding")
            member,generation=ref
            if (member==primary)!=(generation==0):
                raise ValueError("invalid_primary_memory_generation")
            if generation:
                row=self.registry.role_for_member(supervisor=self.supervisor,
                                                  member_id=member,generation=generation)
                if row["status"]!="active":
                    raise PermissionError("memory_member_not_active")
            if ref in seen: raise ValueError("member_profile_binding_reused")
            seen.add(ref); candidate[profile]=ref
        payload=self._payload(candidate)
        if not isinstance(expected_revision,str) or not re.fullmatch(r"[0-9a-f]{64}",expected_revision):
            raise ValueError("expected_binding_revision_required")
        fd=self._locked_binding_file()
        try:
            current=self._read_binding_bytes()
            if hashlib.sha256(current).hexdigest()!=expected_revision:
                raise RuntimeError("stale_memory_binding_revision")
            old=json.loads(current)
            if (old.get("team_id"),old.get("registry_path"),old.get("data_dir")) != (
                    self.team_id,str(self.registry_path),str(self._data_dir)):
                raise PermissionError("memory_binding_identity_changed")
            existing=old.get("profiles",{})
            for profile,record in existing.items():
                expected=(record.get("member_id"),record.get("generation"))
                if candidate.get(profile)!=expected:
                    raise PermissionError("profile_rebind_or_removal_requires_new_profile")
            self._atomic_write_bindings(payload)
            self._profiles=candidate
            return hashlib.sha256(self._read_binding_bytes()).hexdigest()
        finally:
            fcntl.flock(fd,fcntl.LOCK_UN); os.close(fd)

    def _member_storage(self,profile,member,generation):
        members_root=self._team_root/"members"
        self._ensure_private_dir(members_root,self._team_root)
        base=members_root/_key(f"{profile}\\0{member}\\0{generation}")
        self._ensure_private_dir(base,members_root)
        spool=base/"spool"; queue=base/"queue"
        self._ensure_private_dir(spool,base); self._ensure_private_dir(queue,base)
        return spool,queue/"memory_turn_events.db"

    def resolve(self, *, profile, session_id):
        if not isinstance(profile,str) or not _IDENT.fullmatch(profile):
            raise PermissionError("trusted_profile_context_required")
        if not isinstance(session_id,str) or not session_id.strip() or len(session_id)>512:
            raise PermissionError("trusted_session_context_required")
        ref=self._profiles.get(profile)
        if ref is None: raise PermissionError("profile_not_bound_to_team_memory")
        member,generation=ref
        if generation:
            row=self.registry.role_for_member(supervisor=self.supervisor,member_id=member,generation=generation)
            if row["status"]!="active": raise PermissionError("memory_member_not_active")
        spool,queue=self._member_storage(profile,member,generation)
        return TeamMemoryBinding(profile,self.team_id,member,generation,session_id,self._data_dir,spool,queue)

    def worker_binding(self, *, profile):
        ref=self._profiles.get(profile)
        if ref is None: raise PermissionError("profile_not_bound_to_team_memory")
        member,generation=ref
        if generation:
            row=self.registry.role_for_member(supervisor=self.supervisor,member_id=member,generation=generation)
            if row["status"]!="active": raise PermissionError("memory_member_not_active")
        bound=self._member_storage(profile,member,generation)
        return {"profile":profile,"team_id":self.team_id,"user_id":self.team_id,"agent_id":member,
                "member_id":member,"generation":generation,"data_dir":str(self._data_dir),
                "spool_dir":str(bound[0]),"queue_path":str(bound[1]),
                "bootstrap":"agentcore.colleague_shared_memory:configure_worker_from_env",
                "environment":{"AGENTCORE_COLLEAGUE_MEMORY_BINDINGS":str(self._bindings_file),
                               "AGENTCORE_COLLEAGUE_MEMORY_PROFILE":profile}}

    def memory_bridge_settings(self, *, profile, engine_root):
        binding=self.worker_binding(profile=profile)
        root=Path(engine_root).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("trusted_engine_root_required")
        return {"enabled":True,"engine_root":str(root),
                "spool_dir":binding["spool_dir"],"user_id":binding["user_id"],
                "agent_id":binding["agent_id"]}

    def worker_command(self, *, profile, python, repo_root):
        binding=self.worker_binding(profile=profile)
        root=Path(repo_root).resolve(strict=True)
        if not isinstance(python,str) or not Path(python).is_absolute() or not root.is_dir():
            raise ValueError("trusted_runtime_paths_required")
        argv=[python,"-m","agentcore.memory.runtime_service","--bootstrap",binding["bootstrap"],
              "--spool",binding["spool_dir"],"--queue",binding["queue_path"],
              "--profile",binding["profile"],"--user",binding["user_id"],
              "--agent",binding["agent_id"]]
        return {"argv":argv,"cwd":str(root),"environment":binding["environment"]}

    def provider_recall_command(self, *, profile, python, repo_root):
        if profile not in self._profiles: raise PermissionError("profile_not_bound_to_team_memory")
        root=Path(repo_root).resolve(strict=True)
        if not root.is_dir() or not isinstance(python,str) or not Path(python).is_absolute():
            raise ValueError("trusted_runtime_paths_required")
        return [python,"-m","agentcore.colleague_memory_cli","--bindings",str(self._bindings_file),
                "--profile",profile,"{query}"]

    def provider_config(self, *, profile, python, repo_root):
        root=Path(repo_root).resolve(strict=True)
        return {"recall_cmd":self.provider_recall_command(profile=profile,python=python,repo_root=root),
                "cwd":str(root)}

    def runtime_supervisor(self, *, profile, python, repo_root):
        return MemoryRuntimeSupervisor(self, profile=profile, python=python, repo_root=repo_root)

    @property
    def binding_file(self): return self._bindings_file

class MemoryRuntimeSupervisor:
    """Host-owned lifecycle for one profile's memory runtime daemon.

    The store fixes the daemon's argv and environment; this class owns its
    process. The daemon is started outside the host's process group and carries
    an attempt token, so stopping it also ends anything it started, and the
    result says whether that was proven. Without this, a host restart leaves an
    ingestion worker writing a half-processed turn into a team queue.
    """

    def __init__(self, store, *, profile, python, repo_root):
        if type(store) is not ColleagueMemoryBindingStore:
            raise TypeError("trusted_memory_binding_store_required")
        self.store, self.profile = store, profile
        self._command = store.worker_command(profile=profile, python=python, repo_root=repo_root)
        self._runtime_dir = store.state_dir / "runtime"
        store._ensure_private_dir(self._runtime_dir, store.state_dir)
        self._proc = None
        self._token = None

    @property
    def log_path(self):
        return self._runtime_dir / (self.profile + ".log")

    def running(self):
        return self._proc is not None and self._proc.poll() is None

    def start(self):
        """Start the daemon once; a second live start is a host bug, not a retry."""
        if self.running():
            raise RuntimeError("memory_runtime_already_running")
        token = containment.new_token()
        env = dict(os.environ)
        env.update(self._command["environment"])
        env[containment.ENV] = token
        fd = os.open(self.log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND
                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
            log = os.fdopen(fd, "ab")
        except BaseException:
            os.close(fd)
            raise
        with log:
            proc = subprocess.Popen(self._command["argv"], cwd=self._command["cwd"], env=env,
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True, close_fds=True)
        self._proc, self._token = proc, token
        return {"profile": self.profile, "pid": proc.pid, "token": token,
                "log": str(self.log_path)}

    def _queue_path(self):
        argv = self._command["argv"]
        return Path(argv[argv.index("--queue") + 1])

    def _graceful_stop_installed(self):
        """SIGTERM is caught only once the daemon is in its run loop.

        Signalling before that kills it with the default handler, so readiness
        includes "this process can still shut down cleanly".
        """
        try:
            status = Path(f"/proc/{self._proc.pid}/status").read_text()
        except OSError:
            return False
        for line in status.splitlines():
            if line.startswith("SigCgt:"):
                caught = int(line.split(":", 1)[1].strip(), 16)
                return bool(caught & (1 << (signal.SIGTERM - 1)))
        return False

    def ready(self, *, timeout=20.0):
        """Ready means: it opened its queue and it can be asked to stop."""
        queue, deadline = self._queue_path(), time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.running():
                return False
            if queue.exists() and self._graceful_stop_installed():
                return True
            time.sleep(0.02)
        return False

    def stop(self, *, grace=10.0):
        """Stop the daemon and prove nothing it started is left running."""
        proc, token = self._proc, self._token
        if proc is None:
            raise RuntimeError("memory_runtime_not_started")
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            returncode = proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            returncode = proc.wait()
        evidence = containment.sweep(token)
        self._proc = self._token = None
        return {"profile": self.profile, "returncode": returncode,
                "quiescent": False,
                "quiescence_evidence_id": containment.evidence_id(evidence)}

def configure_memory_from_binding_file(binding_file,profile):
    path=Path(binding_file)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("trusted_memory_binding_file_required")
    ColleagueMemoryBindingStore._check_private(path.parent)
    if path.stat().st_mode&0o077: raise ValueError("trusted_memory_binding_file_required")
    raw=json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw,dict) or set(raw)!={"version","team_id","primary_id","registry_path","data_dir","profiles"} or raw["version"]!=1:
        raise ValueError("invalid_memory_binding_file")
    if not isinstance(profile,str) or profile not in raw["profiles"] or not _IDENT.fullmatch(profile):
        raise PermissionError("profile_not_bound_to_team_memory")
    team_id,primary_id,data_dir=raw["team_id"],raw["primary_id"],Path(raw["data_dir"])
    registry_path=Path(raw["registry_path"]); member=raw["profiles"][profile]
    if (not isinstance(team_id,str) or not _IDENT.fullmatch(team_id) or not isinstance(primary_id,str)
        or not _IDENT.fullmatch(primary_id) or not registry_path.is_absolute()
        or registry_path.is_symlink() or not registry_path.is_file() or not isinstance(member,dict)
        or set(member)!={"member_id","generation"} or not isinstance(member["member_id"],str)
        or not _IDENT.fullmatch(member["member_id"]) or type(member["generation"]) is not int
        or member["generation"]<0 or not data_dir.is_absolute() or data_dir.is_symlink()):
        raise ValueError("invalid_memory_binding_file")
    if (member["generation"]==0) != (member["member_id"]==primary_id):
        raise ValueError("invalid_primary_memory_generation")
    parent=path.parent.resolve(strict=True); data=data_dir.resolve(strict=True)
    if parent not in data.parents or not data.is_dir():
        raise PermissionError("memory_data_path_outside_team_root")
    ColleagueMemoryBindingStore._check_private(registry_path.parent)
    if registry_path.stat().st_mode&0o077:
        raise PermissionError("private_registry_required")
    # Recheck membership at every provider/worker startup so a retired or stale
    # generation cannot keep reading/writing from a leftover profile config.
    import sqlite3
    db=sqlite3.connect(registry_path.as_uri()+"?mode=ro",uri=True,timeout=5)
    try:
        primary=db.execute("SELECT primary_id FROM colleague_profiles WHERE profile_id=?",
                           (team_id,)).fetchone()
        row=db.execute("SELECT generation,status,role_id FROM colleague_members WHERE profile_id=? AND member_id=?",
                       (team_id,member["member_id"])).fetchone()
    finally:
        db.close()
    if primary is None or primary[0]!=primary_id or row is None or row[1]!="active":
        raise PermissionError("memory_member_not_active")
    if member["generation"]==0:
        if member["member_id"]!=primary_id or row[2]!="primary":
            raise PermissionError("invalid_primary_memory_generation")
    elif row[0]!=member["generation"] or row[2]=="primary":
        raise PermissionError("stale_member_generation")
    from agentcore.memory.config import MemoryConfig,configure
    configure(MemoryConfig(data_dir=data,default_user=team_id,default_agent=member["member_id"]))
    return {"profile":profile,"team_id":team_id,"user_id":team_id,"member_id":member["member_id"],
            "agent_id":member["member_id"],"generation":member["generation"],"data_dir":str(data)}

def configure_worker_from_env():
    binding=os.environ.get("AGENTCORE_COLLEAGUE_MEMORY_BINDINGS","")
    profile=os.environ.get("AGENTCORE_COLLEAGUE_MEMORY_PROFILE","")
    if not binding or not profile: raise ValueError("trusted_memory_worker_binding_required")
    return configure_memory_from_binding_file(binding,profile)
