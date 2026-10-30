"""Conservative compression accounting. Unreviewed rewrites remain proposals.

Literal coverage proves retention, not factual truth. Nonliteral equivalence
requires an administrator-reviewed, revision-pinned evidence manifest; model
suggestions alone never authorize dropping an old clause.
"""
import hashlib
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import stat
import unicodedata


class CapacityExceeded(ValueError):
    """A known bounded-store refusal; safe to expose as a structured code."""


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def normalize(text):
    value=unicodedata.normalize('NFKC',text).replace('≥','>=').replace('≤','<=')
    def whitespace(match):
        left=value[match.start()-1:match.start()];right=value[match.end():match.end()+1]
        return ' ' if re.fullmatch('[A-Za-z0-9_]',left) and re.fullmatch('[A-Za-z0-9_]',right) else ''
    return re.sub(r'\s+',whitespace,value).strip()


def clauses(text):
    if not isinstance(text,str) or len(text.encode('utf-8'))>512*1024:
        raise ValueError('conservation input budget')
    pieces=[]
    for match in re.finditer(r'【[^】\n]+】|[^【；。\n]+',text):
        quote=match.group().strip()
        if not quote or quote=='§':continue
        # Even a short name or number can be an important fact.
        pieces.append(dict(id=sha(str(match.start())+'\n'+quote),quote=quote))
    return pieces


def protected(text):
    value=normalize(text)
    # A leading list ordinal is formatting, not a retained numeric threshold.
    # Decimal literals (such as 3.0) must not be treated as list ordinals.
    value=re.sub(r'^\d+(?:[)）、]|\.(?!\d))\s*','',value)
    numbers=set(re.findall(r'(?<![A-Za-z0-9_])[-+]?\d+(?:\.\d+)*(?:ms|Nm|s|%)?(?![A-Za-z0-9_])',value))
    operators=set(re.findall(r'>=|<=|==|!=|>|<',value))
    code=set(re.findall(r'`([^`]+)`',value))
    return numbers,operators,code


def reference_fingerprint(ref):
    return sha(json.dumps(dict(text=ref['text'],pointer=ref['pointer'],authority=ref.get('authority') is True),
                          ensure_ascii=False,sort_keys=True))


def evaluate(before,after,*,references=None,review=None):
    references=references or {};review=review or {}
    if not isinstance(after,str) or len(after.encode('utf-8'))>512*1024:
        raise ValueError('conservation input budget')
    before_sha,after_sha=sha(before),sha(after)
    normalized_after=normalize(after)
    sources={'candidate':dict(text=after,authority=False)}
    for name,ref in references.items():
        if name=='candidate' or not isinstance(ref,dict) or not normalize(ref.get('pointer','')):
            raise ValueError('invalid conservation reference')
        # A referenced skill is counted only if the proposed prompt points to it.
        if normalize(ref['pointer']) in normalized_after:sources[name]=ref
    normalized_sources={name:normalize(ref['text']) for name,ref in sources.items()}
    reference_hashes={name:reference_fingerprint(ref) for name,ref in references.items()}
    trusted=(review.get('before_sha256')==before_sha and review.get('after_sha256')==after_sha
             and review.get('references',{})==reference_hashes and bool(review.get('reviewer')))
    decisions={}
    if trusted:
        for decision in review.get('decisions',[]):
            if not isinstance(decision,dict) or decision.get('id') in decisions:
                raise ValueError('invalid conservation review')
            decisions[decision['id']]=decision
    rows=[]
    for atom in clauses(before):
        quote=normalize(atom['quote']);state='unresolved';evidence=None
        for name,ref in sources.items():
            if quote in normalized_sources[name]:
                state='literal' if name=='candidate' else 'referenced_literal';evidence=name;break
        if state=='unresolved' and atom['id'] in decisions:
            decision=decisions[atom['id']];ref=sources.get(decision.get('source'))
            excerpt=decision.get('quote','');kind=decision.get('decision')
            if ref and excerpt and excerpt in ref['text'] and decision.get('reason'):
                if kind=='equivalent':
                    old_terms=protected(atom['quote']);new_terms=protected(excerpt)
                    if all(old.issubset(new) for old,new in zip(old_terms,new_terms)):
                        state='reviewed_equivalent';evidence=decision['source']
                elif kind=='retired' and decision.get('source')!='candidate' and ref.get('authority') is True:
                    state='reviewed_retired';evidence=decision['source']
        rows.append(dict(atom,state=state,evidence_source=evidence))
    unresolved=[r['id'] for r in rows if r['state']=='unresolved']
    return dict(before_sha256=before_sha,after_sha256=after_sha,references=reference_hashes,
                clauses=rows,unresolved=unresolved,review_accepted=trusted,passed=not unresolved)


def proposal_key(source,before,after):
    return sha(str(Path(source).resolve())+'\n'+sha(before)+'\n'+sha(after))


@contextmanager
def queue_lock(root):
    root=Path(root)
    if not root.is_absolute():raise ValueError('absolute conservation queue required')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    if root.is_symlink():raise ValueError('conservation queue must not be a symlink')
    fd=os.open(root/'.queue.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        yield root
    finally:os.close(fd)


def payloads(directory):
    """Bounded stores contain only direct regular files; never follow links."""
    if directory.is_symlink():raise ValueError('proposal directory symlink')
    result={}
    if directory.exists():
        for path in directory.iterdir():
            info=path.lstat()
            if not stat.S_ISREG(info.st_mode):raise ValueError('proposal contains nonregular payload')
            result[path.name]=info.st_size
    return result


def inventory(directory):
    if directory.is_symlink():raise ValueError('proposal store symlink')
    result={}
    if directory.exists():
        for path in directory.iterdir():
            if not re.fullmatch('[0-9a-f]{64}',path.name) or not path.is_dir():
                raise ValueError('unexpected proposal store entry')
            result[path.name]=payloads(path)
    return result


def check_capacity(items,key,values,*,max_proposals,max_bytes):
    if (type(max_proposals) is not int or type(max_bytes) is not int
            or max_proposals<1 or max_bytes<1):raise ValueError('positive proposal budgets required')
    count=len(items)+(key not in items)
    size=sum(sum(files.values()) for files in items.values())
    size+=sum(len(value) for name,value in values.items() if name not in items.get(key,{}))
    if count>max_proposals or size>max_bytes:raise CapacityExceeded('proposal_capacity_exceeded')


def immutable_write(path,data):
    fd,temporary=tempfile.mkstemp(dir=path.parent,prefix='.conservation-')
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        try:os.link(temporary,path)
        except FileExistsError:
            if path.read_bytes()!=data:raise ValueError('conservation proposal conflict')
    finally:os.unlink(temporary)


def sync_dirs(paths):
    for path in paths:
        fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)


def stage(root,source,before,after,report,*,max_proposals=128,max_bytes=32*1024*1024):
    with queue_lock(root) as root:
        return _stage_locked(root,source,before,after,report,max_proposals,max_bytes)


def _stage_locked(root,source,before,after,report,max_proposals,max_bytes):
    key=proposal_key(source,before,after);directory=root/'pending'/key
    serialized=json.dumps(dict(report,source=str(Path(source).resolve())),ensure_ascii=False,sort_keys=True,indent=2)
    report_name='report-'+sha(serialized)+'.json'
    values={name:value.encode('utf-8') for name,value in
            {'before.md':before,'candidate.md':after,report_name:serialized}.items()}
    items=inventory(root/'pending')
    check_capacity(items,key,values,max_proposals=max_proposals,max_bytes=max_bytes)
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    for name,value in values.items():immutable_write(directory/name,value)
    sync_dirs([directory,directory.parent,root,root.parent])
    return dict(id=key,path=str(directory),report_path=str(directory/report_name),unresolved_count=len(report['unresolved']))
