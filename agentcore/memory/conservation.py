"""Conservative compression accounting. Unreviewed rewrites remain proposals.

Literal coverage proves retention, not factual truth. Nonliteral equivalence
requires an administrator-reviewed, revision-pinned evidence manifest; model
suggestions alone never authorize dropping an old clause.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata


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


def stage(root,source,before,after,report):
    root=Path(root)
    if not root.is_absolute():raise ValueError('absolute conservation queue required')
    key=proposal_key(source,before,after);directory=root/'pending'/key
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    serialized=json.dumps(dict(report,source=str(Path(source).resolve())),ensure_ascii=False,sort_keys=True,indent=2)
    report_name='report-'+sha(serialized)+'.json'
    values={'before.md':before,'candidate.md':after,report_name:serialized}
    for name,value in values.items():
        path=directory/name
        fd,temporary=tempfile.mkstemp(dir=directory,prefix='.conservation-')
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                stream.write(value);stream.flush();os.fsync(stream.fileno())
            try:os.link(temporary,path)
            except FileExistsError:
                if path.read_text(encoding='utf-8')!=value:raise ValueError('conservation proposal conflict')
        finally:os.unlink(temporary)
    for path in [directory,directory.parent,root,root.parent]:
        fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    return dict(id=key,path=str(directory),report_path=str(directory/report_name),unresolved_count=len(report['unresolved']))
