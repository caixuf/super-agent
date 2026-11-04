"""Host-owned colleague turns admitted through the Hermes Gateway API.

The host injects an authenticated, profile-bound connection. This module submits
and observes durable admissions; it never launches Hermes, cancels work, or sends
messages. Gateway terminal status alone does not prove background processes ended.
"""
from __future__ import annotations
from contextlib import contextmanager
import fcntl, hashlib, json, os, re, sqlite3, stat, time
from pathlib import Path
from agentcore.pool import normalize_origin
from agentcore.colleague_registry import Supervisor
from agentcore.colleague_hermes_runner import ProfileBinding

_MEMBER=re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_REQUEST=re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SESSION=re.compile(r"local-[0-9a-f]{64}\Z")
_STATES={"queued","started","terminal","unknown"}

def _identity(owner, member, generation, request, prompt):
    canonical=normalize_origin(owner)
    if canonical is None: raise PermissionError("authenticated_owner_required")
    owner=json.loads(canonical)
    if not isinstance(member,str) or not _MEMBER.fullmatch(member): raise ValueError("invalid_member_id")
    if type(generation) is not int or generation<1: raise ValueError("invalid_generation")
    if not isinstance(request,str) or not _REQUEST.fullmatch(request): raise ValueError("invalid_request_id")
    if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>12000: raise ValueError("invalid_prompt")
    return owner,member,generation,request,prompt.strip()

class GatewayColleagueDispatcher:
    """Persist identity/admission and submit through a trusted profile-bound Gateway."""
    def __init__(self, *, registry, supervisor:Supervisor, provisioner, bindings,
                 connection_for_profile, all_work_quiescent=None, completion_sink=None, state_dir, lock_timeout=30):
        if type(supervisor) is not Supervisor or supervisor.profile_id!=registry.profile_id:
            raise PermissionError("trusted_supervisor_required")
        if not callable(getattr(bindings,"resolve",None)): raise ValueError("persistent_session_binding_store_required")
        if not callable(getattr(provisioner,"status",None)): raise ValueError("provisioner_required")
        if not callable(connection_for_profile) or (all_work_quiescent is not None and not callable(all_work_quiescent)):
            raise ValueError("trusted_gateway_host_callbacks_required")
        root=Path(state_dir)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir(): raise ValueError("private_state_directory_required")
        info=root.stat()
        if info.st_mode&0o077 or (hasattr(os,"getuid") and info.st_uid!=os.getuid()):
            raise ValueError("private_state_directory_required")
        if isinstance(lock_timeout,bool) or not isinstance(lock_timeout,(int,float)) or lock_timeout<=0:
            raise ValueError("invalid_lock_timeout")
        self.registry,self.supervisor,self.provisioner=registry,supervisor,provisioner
        self.bindings,self.connection_for_profile=bindings,connection_for_profile
        if completion_sink is not None and not callable(getattr(completion_sink, "record_colleague_completion", None)):
            raise ValueError("trusted_completion_sink_required")
        self.completion_sink = completion_sink
        self.all_work_quiescent,self.state_dir,self.lock_timeout=all_work_quiescent,root,float(lock_timeout)
        self.db_path=root/"colleague_gateway_tasks.sqlite"
        if self.db_path.is_symlink(): raise ValueError("private_task_database_required")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS tasks(
                owner_profile TEXT NOT NULL,owner_session TEXT NOT NULL,request_id TEXT NOT NULL,
                member_id TEXT NOT NULL,generation INTEGER NOT NULL,prompt_sha256 TEXT NOT NULL,
                work_id TEXT NOT NULL UNIQUE,hermes_profile TEXT,hermes_session_id TEXT,
                input_id TEXT NOT NULL,admission_id TEXT,gateway_generation INTEGER,
                gateway_principal_id TEXT,gateway_profile_id TEXT,state TEXT NOT NULL,terminal_outcome TEXT,
                quiescence_evidence_id TEXT,settlement_state TEXT,
                created_at REAL NOT NULL,updated_at REAL NOT NULL,
                PRIMARY KEY(owner_profile,owner_session,request_id))""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
            if "owner_origin" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN owner_origin TEXT")
            if "delivery_job_id" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN delivery_job_id TEXT")
            os.chmod(self.db_path,0o600,follow_symlinks=False)

    @contextmanager
    def _db(self):
        db=sqlite3.connect(str(self.db_path),timeout=30); db.row_factory=sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def _ids(self,owner,member,generation,request):
        raw=json.dumps([self.registry.profile_id,owner["profile"],owner["session"],member,generation,request],
                       separators=(",",":")); digest=hashlib.sha256(raw.encode()).hexdigest()
        return digest[:32],"colleague-"+digest

    @contextmanager
    def _member_lock(self,member,generation,*,timeout=None):
        name=hashlib.sha256(json.dumps([self.registry.profile_id,member,generation],
                                       separators=(",",":")).encode()).hexdigest()
        fd=os.open(self.state_dir/(name+".gateway-member.lock"),
                   os.O_CREAT|os.O_RDWR|getattr(os,"O_NOFOLLOW",0),0o600)
        try:
            i=os.fstat(fd)
            if not stat.S_ISREG(i.st_mode) or i.st_mode&0o077 or i.st_nlink!=1 or (hasattr(os,"getuid") and i.st_uid!=os.getuid()):
                raise ValueError("private_member_lock_required")
            end=time.monotonic()+(self.lock_timeout if timeout is None else min(self.lock_timeout, timeout))
            while True:
                try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); break
                except BlockingIOError:
                    if time.monotonic()>=end: raise TimeoutError("colleague_member_busy") from None
                    time.sleep(.05)
            yield
        finally: os.close(fd)

    def _load(self,owner,request):
        with self._db() as db:
            return db.execute("SELECT * FROM tasks WHERE owner_profile=? AND owner_session=? AND request_id=?",
                              (owner["profile"],owner["session"],request)).fetchone()

    def _member_display(self, member_id, generation):
        """Use registry identity, never a name inferred from generated text."""
        member=self.registry.role_for_member(supervisor=self.supervisor,member_id=member_id,generation=generation)
        return {"member_id":member["member_id"],"generation":member["generation"],
                "display_name":member["display_name"],"role_id":member["role_id"],"role_label":member["label"]}

    def _intent(self,owner,member,generation,request,prompt,work,input_id):
        digest=hashlib.sha256(prompt.encode()).hexdigest(); now=time.time()
        with self._db() as db:
            row=db.execute("SELECT * FROM tasks WHERE owner_profile=? AND owner_session=? AND request_id=?",
                           (owner["profile"],owner["session"],request)).fetchone()
            if row:
                if (row["member_id"],row["generation"],row["prompt_sha256"],row["work_id"],row["input_id"])!=(member,generation,digest,work,input_id):
                    raise ValueError("request_id_conflict")
                return row
            db.execute("""INSERT INTO tasks(owner_profile,owner_session,request_id,member_id,generation,
                prompt_sha256,work_id,input_id,state,created_at,updated_at,owner_origin)
                VALUES(?,?,?,?,?,?,?,?, 'binding_pending',?,?,?)""",
                (owner["profile"],owner["session"],request,member,generation,digest,work,input_id,now,now,
                 normalize_origin(owner)))
        return self._load(owner,request)

    def _update(self,owner,request,**values):
        allowed={"hermes_profile","hermes_session_id","admission_id","gateway_generation",
                 "gateway_principal_id","gateway_profile_id","state","terminal_outcome","quiescence_evidence_id","settlement_state","delivery_job_id"}
        if not values or set(values)-allowed: raise ValueError("invalid_task_update")
        # Preserve a completed settlement even if a concurrent status reader
        # loaded the task before another reader finished settlement.
        assignments = []
        for key in values:
            if key == "state":
                assignments.append("state=CASE WHEN state='settled' AND settlement_state='settled' THEN state "
                                   "WHEN state='terminal' AND ? NOT IN ('terminal','settled') "
                                   "THEN state ELSE ? END")
            elif key == "terminal_outcome":
                assignments.append("terminal_outcome=CASE WHEN state IN ('terminal','settled') "
                                   "AND terminal_outcome IS NOT NULL THEN terminal_outcome ELSE ? END")
            elif key == "settlement_state":
                assignments.append("settlement_state=CASE WHEN settlement_state='settled' THEN settlement_state ELSE ? END")
            elif key in {"quiescence_evidence_id", "delivery_job_id"}:
                assignments.append(key + "=COALESCE(" + key + ", ?)")
            else:
                assignments.append(key + "=?")
        parameters = []
        for key, value in values.items():
            parameters.extend((value, value) if key == "state" else (value,))
        with self._db() as db:
            db.execute("UPDATE tasks SET "+",".join(assignments)+
                       ",updated_at=? WHERE owner_profile=? AND owner_session=? AND request_id=?",
                       (*parameters,time.time(),owner["profile"],owner["session"],request))

    def _reserve(self,member,generation,work):
        result=self.registry.reserve_work_exclusive(
            supervisor=self.supervisor,member_id=member,generation=generation,work_id=work)
        if result.get("reserved") is not True: raise RuntimeError("work_reservation_failed")
        status=self.registry.work_status(supervisor=self.supervisor, member_id=member, generation=generation)
        return max(0, status["tracked_open"]-1)

    def _connection(self,owner,member,generation):
        binding=self.bindings.resolve(member,generation,owner["profile"],owner["session"],allow_draining=True)
        if (not isinstance(binding,ProfileBinding) or
            (binding.member_id,binding.generation,binding.owner_profile,binding.owner_session)!=(member,generation,owner["profile"],owner["session"]) or
            not _SESSION.fullmatch(binding.hermes_session_id)):
            raise PermissionError("binding_owner_mismatch")
        conn=self.connection_for_profile(binding.hermes_profile)
        gateway_profile_id=getattr(conn,"gateway_profile_id",None)
        if (getattr(conn,"hermes_profile",None)!=binding.hermes_profile or
            not isinstance(gateway_profile_id,str) or not gateway_profile_id or
            not isinstance(getattr(conn,"principal_id",None),str) or not conn.principal_id or
            not callable(getattr(conn,"call",None))):
            raise PermissionError("profile_bound_gateway_connection_required")
        return binding,conn

    @staticmethod
    def _resume(conn,sid):
        snap=conn.call("session.resume",session_id=sid)
        if not isinstance(snap,dict) or snap.get("stored_session_id")!=sid or type(snap.get("execution_generation")) is not int:
            raise RuntimeError("gateway_resume_identity_mismatch")
        return snap

    def submit(self,*,owner,member_id,generation,request_id,prompt):
        owner,member_id,generation,request_id,prompt=_identity(owner,member_id,generation,request_id,prompt)
        with self._member_lock(member_id,generation):
            work,input_id=self._ids(owner,member_id,generation,request_id)
            row=self._load(owner,request_id)
            if row is None:
                member=self.registry.role_for_member(supervisor=self.supervisor,member_id=member_id,generation=generation)
                if member["status"]!="active": raise PermissionError("member_not_accepting_work")
                ready=self.provisioner.status(member_id,generation)
                # A host startup may have found this member temporarily offline.
                # Retry provisioning only before creating a new task intent; never
                # replay an admitted task to recover a missing worker.
                if (isinstance(ready,dict) and ready.get("ready") is not True
                    and ready.get("membership_status")=="active" and ready.get("phase")=="starting"):
                    ready=self.provisioner.provision(member_id,generation)
                if not isinstance(ready,dict) or ready.get("ready") is not True: raise RuntimeError("member_not_ready")
            row=self._intent(owner,member_id,generation,request_id,prompt,work,input_id)
            if row["admission_id"]:
                return self.inspect(owner=owner,request_id=request_id)
            ready=self.provisioner.status(member_id,generation)
            if not isinstance(ready,dict) or ready.get("ready") is not True:
                raise RuntimeError("member_not_ready")
            prior_unconfirmed=self._reserve(member_id,generation,work)
            binding,conn=self._connection(owner,member_id,generation)
            saved=(row["hermes_profile"],row["hermes_session_id"],row["gateway_profile_id"],row["gateway_principal_id"])
            current=(binding.hermes_profile,binding.hermes_session_id,conn.gateway_profile_id,conn.principal_id)
            if any(value is not None for value in saved) and saved!=current:
                raise PermissionError("gateway_task_binding_changed")
            self._update(owner,request_id,hermes_profile=binding.hermes_profile,hermes_session_id=binding.hermes_session_id,
                         gateway_principal_id=conn.principal_id,gateway_profile_id=conn.gateway_profile_id,state="submit_pending")
            self._resume(conn,binding.hermes_session_id)
            member=self.registry.role_for_member(supervisor=self.supervisor,member_id=member_id,generation=generation)
            text=(f"Host assignment: member_id={member_id}; generation={generation}. Registered duty: {member['duty']}. "
                  "This duty is context and does not restrict general Hermes capabilities or tools.\n\nTask:\n"+prompt)
            try:
                receipt=conn.call("prompt.submit",session_id=binding.hermes_session_id,input_id=input_id,text=text)
            except Exception:
                return {"request_id":request_id,"member":self._member_display(member_id,generation),
                        "state":"unknown","admission_id":row["admission_id"],"retry_input_id":input_id,
                        "prior_background_unconfirmed":prior_unconfirmed}
            result=self._record(owner,request_id,receipt)
            result["prior_background_unconfirmed"]=prior_unconfirmed
            return result

    def _record(self,owner,request,receipt):
        row=self._load(owner,request)
        ref=receipt.get("ref") if isinstance(receipt,dict) else None
        if (not isinstance(receipt,dict) or not isinstance(receipt.get("admission_id"),str)
                or not isinstance(ref,dict) or ref.get("profile_id")!=row["gateway_profile_id"]
                or ref.get("session_id")!=row["hermes_session_id"]
                or receipt.get("execution_generation") is not None and type(receipt.get("execution_generation")) is not int):
            self._update(owner,request,state="unknown")
            return {"request_id":request,"member":self._member_display(row["member_id"],row["generation"]),
                    "state":"unknown","admission_id":None}
        state=receipt.get("status")
        if state not in _STATES:
            self._update(owner,request,state="unknown")
            return {"request_id":request,"member":self._member_display(row["member_id"],row["generation"]),
                    "state":"unknown","admission_id":receipt["admission_id"]}
        self._update(owner,request,admission_id=receipt["admission_id"],gateway_generation=receipt.get("execution_generation"),
                     state=state,terminal_outcome=receipt.get("outcome"))
        return {"request_id":request,"member":self._member_display(row["member_id"],row["generation"]),
                "state":state,"admission_id":receipt["admission_id"],
                "execution_generation":receipt.get("execution_generation"),"outcome":receipt.get("outcome")}


    def refresh_pending(self, *, limit=32, should_stop=None):
        """Refresh durable admissions from their bound Gateway; never submit work."""
        if type(limit) is not int or not 1 <= limit <= 256:
            raise ValueError("invalid_refresh_limit")
        if should_stop is not None and not callable(should_stop):
            raise ValueError("invalid_refresh_stop_signal")
        delivery_clause = (" OR (state='settled' AND owner_origin IS NOT NULL AND delivery_job_id IS NULL "
                           "AND instr(owner_origin, '\"route\"') > 0 "
                           "AND quiescence_evidence_id NOT LIKE 'quiescence-%')") if self.completion_sink is not None else ""
        with self._db() as db:
            rows = db.execute(
                "SELECT owner_profile,owner_session,request_id,member_id,generation "
                "FROM tasks WHERE admission_id IS NOT NULL AND "
                "((state IN ('queued','started','unknown','terminal') AND COALESCE(settlement_state,'') != 'settled')"
                + delivery_clause + ") ORDER BY updated_at LIMIT ?", (limit,)
            ).fetchall()
        refreshed = failed = 0
        examined = 0
        for row in rows:
            if should_stop is not None and should_stop():
                break
            examined += 1
            owner = {"profile": row["owner_profile"], "session": row["owner_session"]}
            try:
                with self._member_lock(row["member_id"], row["generation"], timeout=1.0):
                    observation = self.inspect(owner=owner, request_id=row["request_id"])
                if isinstance(observation, dict) and observation.get("state") in {"queued", "started", "terminal"}:
                    refreshed += 1
                else:
                    # Unknown is a valid result, but not a successful refresh.
                    failed += 1
            except Exception:
                # An inaccessible Gateway is not evidence of failure or
                # permission to replay the task.
                failed += 1
            finally:
                # Also rotate when inspect returns an unknown observation
                # without writing, so a dead profile cannot starve the rest.
                with self._db() as db:
                    db.execute("UPDATE tasks SET updated_at=? WHERE owner_profile=? AND owner_session=? AND request_id=?",
                               (time.time(), owner["profile"], owner["session"], row["request_id"]))
        return {"examined": examined, "refreshed": refreshed, "failed": failed}

    @staticmethod
    def _public_result(receipt, bound_session_id, response_offset=0, response_revision=None):
        raw=receipt.get("result") if isinstance(receipt,dict) else None
        if not isinstance(raw,dict) or raw.get("session_id")!=bound_session_id:
            return None,"withheld_unverified_session_identity"
        public={}
        final=raw.get("final_response")
        if isinstance(final,str):
            revision=hashlib.sha256(final.encode("utf-8")).hexdigest()
            if response_revision is not None and response_revision!=revision:
                raise ValueError("response_revision_changed")
            if response_offset > len(final):
                raise ValueError("response_offset_out_of_range")
            end=min(response_offset+12000,len(final))
            public["final_response"]=final[response_offset:end]
            public["response_offset"]=response_offset
            public["response_revision"]=revision
            public["response_next_offset"]=end if end<len(final) else None
            public["response_truncated"]=end<len(final)
        for key in ("completed","failed","interrupted"):
            if type(raw.get(key)) is bool: public[key]=raw[key]
        return public,"verified"

    def inspect(self,*,owner,request_id,response_offset=0,response_revision=None):
        if type(response_offset) is not int or response_offset<0:
            raise ValueError("invalid_response_offset")
        if (response_revision is not None and
                (not isinstance(response_revision,str) or not re.fullmatch(r"[0-9a-f]{64}",response_revision))):
            raise ValueError("invalid_response_revision")
        if response_offset and response_revision is None:
            raise ValueError("response_revision_required")
        canonical=normalize_origin(owner)
        if canonical is None: raise PermissionError("authenticated_owner_required")
        owner=json.loads(canonical)
        if not isinstance(request_id,str) or not _REQUEST.fullmatch(request_id): raise ValueError("invalid_request_id")
        row=self._load(owner,request_id)
        if row is None: return None
        member=self._member_display(row["member_id"],row["generation"])
        if not row["admission_id"]:
            return {"request_id":request_id,"member":member,"state":"unknown","admission_id":None,"retry_input_id":row["input_id"]}
        binding=ProfileBinding(row["member_id"],row["generation"],owner["profile"],owner["session"],
                               row["hermes_profile"],row["hermes_session_id"])
        conn=self.connection_for_profile(binding.hermes_profile)
        if (getattr(conn,"hermes_profile",None)!=row["hermes_profile"] or
            getattr(conn,"gateway_profile_id",None)!=row["gateway_profile_id"] or
            getattr(conn,"principal_id",None)!=row["gateway_principal_id"] or not callable(getattr(conn,"call",None))):
            raise PermissionError("gateway_principal_binding_changed")
        try:
            self._resume(conn,binding.hermes_session_id)
            receipt=conn.call("prompt.receipt",session_id=binding.hermes_session_id,admission_id=row["admission_id"],include_result=True)
        except Exception:
            return {"request_id":request_id,"member":member,"state":"unknown","admission_id":row["admission_id"],"outcome":"unknown"}
        ref=receipt.get("ref") if isinstance(receipt,dict) else None
        generation=receipt.get("execution_generation") if isinstance(receipt,dict) else None
        if (not isinstance(receipt,dict) or receipt.get("admission_id")!=row["admission_id"] or
            not isinstance(ref,dict) or ref.get("profile_id")!=row["gateway_profile_id"] or
            ref.get("session_id")!=row["hermes_session_id"] or receipt.get("status") not in _STATES or
            generation is not None and type(generation) is not int or
            row["gateway_generation"] is not None and generation!=row["gateway_generation"]):
            return {"request_id":request_id,"member":member,"state":"unknown","admission_id":row["admission_id"],"outcome":"unknown"}
        state=receipt["status"]
        if (row["state"] in {"terminal", "settled"} and
                (state != "terminal" or (row["terminal_outcome"] is not None and
                                         receipt.get("outcome") != row["terminal_outcome"]))):
            return {"request_id":request_id,"member":member,"state":"unknown",
                    "admission_id":row["admission_id"],"outcome":"unknown"}
        # A later status read must not move a settled task back to terminal.
        # The external receipt describes execution, while settlement is a
        # durable local transition and remains authoritative once complete.
        persisted_state = "settled" if row["settlement_state"] == "settled" else state
        updates={"state":persisted_state,"terminal_outcome":receipt.get("outcome")}
        if generation is not None and row["gateway_generation"] is None:
            updates["gateway_generation"]=generation
            row=dict(row); row["gateway_generation"]=generation
        self._update(owner,request_id,**updates)
        reservation="open"
        if state=="terminal":
            # A durable evidence intent precedes Registry settlement. If the host
            # crashes on either side of settle_work, the same evidence_id is replayed
            # idempotently; no new process inference or Gateway task replay occurs.
            row=self._load(owner,request_id)
            if (row["settlement_state"]=="pending" and row["quiescence_evidence_id"]
                    and row["quiescence_evidence_id"].startswith("quiescence-")):
                # eaa466e generated these IDs from a cwd scan, which was not
                # whole-attempt evidence. Do not replay the pending settlement.
                reservation="legacy_quiescence_evidence_unverified"
            elif row["settlement_state"]=="pending" and row["quiescence_evidence_id"]:
                self.registry.settle_work(supervisor=self.supervisor,member_id=row["member_id"],
                                          generation=row["generation"],work_id=row["work_id"])
                self._update(owner,request_id,state="settled",settlement_state="settled")
                reservation="closed"
            elif row["settlement_state"]=="settled":
                reservation="closed"
            else:
                evidence=self.all_work_quiescent(row["member_id"],row["generation"],row["hermes_profile"],
                                                  row["hermes_session_id"],row["admission_id"]) if self.all_work_quiescent else None
                evidence_ok=(isinstance(evidence,dict) and evidence.get("all_owned_processes_terminal") is True and
                    isinstance(evidence.get("evidence_id"),str) and evidence["evidence_id"] and
                    evidence.get("member_id")==row["member_id"] and evidence.get("generation")==row["generation"] and
                    evidence.get("hermes_profile")==row["hermes_profile"] and
                    evidence.get("hermes_session_id")==row["hermes_session_id"] and
                    evidence.get("admission_id")==row["admission_id"])
                if evidence_ok:
                    self._update(owner,request_id,quiescence_evidence_id=evidence["evidence_id"],settlement_state="pending")
                    self.registry.settle_work(supervisor=self.supervisor,member_id=row["member_id"],
                                              generation=row["generation"],work_id=row["work_id"])
                    self._update(owner,request_id,state="settled",settlement_state="settled")
                    reservation="closed"
                else: reservation="awaiting_quiescence_evidence"
        public_result,result_visibility=self._public_result(receipt,row["hermes_session_id"],response_offset,response_revision)
        # A native Hermes turn may finish while a delegated business task keeps
        # running. Release the conversational slot only; keep colleague_work
        # open, so retirement and whole-work settlement remain fenced.
        if (state == "terminal" and receipt.get("outcome") == "completed" and
                reservation == "awaiting_quiescence_evidence" and row["owner_origin"] is not None and
                type(row["gateway_generation"]) is int and result_visibility == "verified" and
                isinstance(public_result, dict) and public_result.get("completed") is True and
                public_result.get("failed") is False and public_result.get("interrupted") is False):
            self.registry.release_gateway_turn(supervisor=self.supervisor, member_id=row["member_id"],
                generation=row["generation"], work_id=row["work_id"], admission_id=row["admission_id"],
                execution_generation=row["gateway_generation"])
            reservation = "turn_released_background_unconfirmed"
        if self.completion_sink is not None and state=="terminal" and response_offset==0:
            outcome=receipt.get("outcome")
            delivery_status=None
            text=None
            if (outcome=="completed" and result_visibility=="verified" and isinstance(public_result,dict)
                    and public_result.get("completed") is True and public_result.get("failed") is False
                    and public_result.get("interrupted") is False):
                text=public_result.get("final_response")
                if isinstance(text,str) and text.strip():
                    delivery_status="completed"
                    if public_result.get("response_truncated") is True:
                        text=text[:11850]+"\n（内容较长，请查询同事任务获取完整答复。）"
                    # Stable across settlement races and retries; this describes
                    # the turn without claiming external work has stopped.
                    text=text[:11900]+"\n（本轮回复已完成；后台工作状态请查询同事任务。）"
            elif outcome=="failed":
                delivery_status,text="failed","同事本轮执行失败；后台工作状态请查询同事任务。"
            elif outcome in {"interrupted","cancelled"} and reservation=="closed":
                delivery_status,text="cancelled","同事任务已中断，请查询任务状态。"
            current=self._load(owner,request_id)
            evidence=current["quiescence_evidence_id"]
            verified_settlement = isinstance(evidence,str) and bool(evidence) and not evidence.startswith("quiescence-")
            deliverable = ((delivery_status in {"completed", "failed"} and reservation != "closed") or
                           (reservation == "closed" and verified_settlement))
            if (delivery_status and deliverable and current["owner_origin"] and current["delivery_job_id"] is None):
                try:
                    origin=json.loads(current["owner_origin"])
                    if isinstance(origin,dict) and isinstance(origin.get("route"),str):
                        job=self.completion_sink.record_colleague_completion(
                            source_id=current["work_id"],origin=origin,created=current["created_at"],
                            status=delivery_status,summary=text,
                            member={"role_id":member["role_id"],"display_name":member["display_name"],"role_label":member["role_label"]})
                        self._update(owner,request_id,delivery_job_id=job)
                except (ValueError,TypeError,RuntimeError,sqlite3.Error):
                    # Leave the handoff pending; retry uses a deterministic ID.
                    pass
        return {"request_id":request_id,"member":member,"state":state,"admission_id":row["admission_id"],
                "execution_generation":row["gateway_generation"],"outcome":receipt.get("outcome"),
                "result":public_result,"result_visibility":result_visibility,"reservation":reservation}
