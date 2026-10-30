"""Verified, bounded archival of compression candidates. Never edits memory.

No expiry deletes old memory versions. Pending candidates can be retired from
the online queue only after a private archive is durable and verified; restore
brings back candidate files, never applies their contents to a live source.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re

from . import conservation as c


def checksum(data):return hashlib.sha256(data).hexdigest()


def key_path(root,kind,key):
    if not re.fullmatch('[0-9a-f]{64}',key):raise ValueError('invalid proposal id')
    path=root/kind/key
    if (root/kind).is_symlink() or path.is_symlink():raise ValueError('proposal path symlink')
    return path


def validate_proposal(key,values):
    if not {'before.md','candidate.md'}.issubset(values):raise ValueError('incomplete proposal')
    reports=[name for name in values if re.fullmatch('report-[0-9a-f]{64}\\.json',name)]
    if not reports or set(values)-{'before.md','candidate.md'}-set(reports):
        raise ValueError('unrecognized proposal payload')
    before=values['before.md'].decode();after=values['candidate.md'].decode()
    for name in reports:
        text=values[name].decode();report=json.loads(text)
        if name!='report-'+c.sha(text)+'.json':raise ValueError('report digest mismatch')
        if report.get('before_sha256')!=c.sha(before) or report.get('after_sha256')!=c.sha(after):
            raise ValueError('proposal revision mismatch')
        if c.proposal_key(report['source'],before,after)!=key:raise ValueError('proposal identity mismatch')


def read_archive(directory,key):
    names=c.payloads(directory)
    if 'manifest.json' not in names:raise ValueError('archive incomplete')
    raw=(directory/'manifest.json').read_bytes();manifest=json.loads(raw)
    if manifest.get('kind')!='memory-conservation-proposal' or manifest.get('id')!=key:
        raise ValueError('archive identity mismatch')
    files=manifest.get('files',{})
    if set(names)!=set(files)|{'manifest.json'}:raise ValueError('archive payload mismatch')
    values={}
    for name,meta in files.items():
        if Path(name).name!=name or name in ('.','..'):raise ValueError('archive path traversal')
        data=(directory/name).read_bytes()
        if len(data)!=meta['bytes'] or checksum(data)!=meta['sha256']:
            raise ValueError('archive checksum mismatch')
        values[name]=data
    validate_proposal(key,values)
    return values,checksum(raw)


def status(root):
    with c.queue_lock(root) as root:
        result={}
        for kind in ('pending','archive'):
            items=c.inventory(root/kind)
            result[kind]=dict(proposals=len(items),payload_bytes=sum(sum(v.values()) for v in items.values()))
        return result


def archive(root,key,*,max_proposals=512,max_bytes=128*1024*1024):
    with c.queue_lock(root) as root:
        pending=key_path(root,'pending',key);destination=key_path(root,'archive',key)
        current=c.payloads(pending)
        if (destination/'manifest.json').exists():
            values,manifest_sha=read_archive(destination,key)
            # A crash can leave some online files already retired. Every
            # survivor must match the complete archive before removing it.
            for name in current:
                if name not in values or (pending/name).read_bytes()!=values[name]:
                    raise ValueError('online candidate changed since archival')
        else:
            values={name:(pending/name).read_bytes() for name in current}
            validate_proposal(key,values)
            manifest=dict(kind='memory-conservation-proposal',id=key,
                files={name:dict(bytes=len(data),sha256=checksum(data)) for name,data in values.items()})
            raw=(json.dumps(manifest,sort_keys=True,indent=2)+'\n').encode()
            copied=dict(values,**{'manifest.json':raw})
            c.check_capacity(c.inventory(root/'archive'),key,copied,
                             max_proposals=max_proposals,max_bytes=max_bytes)
            destination.mkdir(parents=True,exist_ok=True,mode=0o700)
            for name,data in values.items():c.immutable_write(destination/name,data)
            c.immutable_write(destination/'manifest.json',raw)
            c.sync_dirs([destination,destination.parent,root,root.parent])
            _,manifest_sha=read_archive(destination,key)
        # No recursive removal or archive expiry: delete only the files which
        # were just compared to this verified archive, then the empty directory.
        for name in current:
            if (pending/name).read_bytes()!=values[name]:raise ValueError('online candidate changed')
        for name in current:(pending/name).unlink()
        if pending.exists():pending.rmdir();c.sync_dirs([pending.parent])
        return dict(id=key,archived=True,manifest_sha256=manifest_sha,files=len(values),
                    source_memory_untouched=True)


def restore(root,key,*,max_proposals=128,max_bytes=32*1024*1024):
    with c.queue_lock(root) as root:
        destination=key_path(root,'pending',key)
        values,manifest_sha=read_archive(key_path(root,'archive',key),key)
        c.check_capacity(c.inventory(root/'pending'),key,values,
                         max_proposals=max_proposals,max_bytes=max_bytes)
        existing=c.payloads(destination)
        if set(existing)-set(values):raise ValueError('restore destination contains unknown payload')
        for name in existing:
            if (destination/name).read_bytes()!=values[name]:raise ValueError('restore destination conflict')
        destination.mkdir(parents=True,exist_ok=True,mode=0o700)
        for name,data in values.items():c.immutable_write(destination/name,data)
        c.sync_dirs([destination,destination.parent,root,root.parent])
        if {name:(destination/name).read_bytes() for name in c.payloads(destination)}!=values:
            raise ValueError('restored proposal verification failed')
        return dict(id=key,restored=True,manifest_sha256=manifest_sha,files=len(values),
                    source_memory_untouched=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('status')
    for name in ('archive','restore'):
        command=sub.add_parser(name);command.add_argument('id')
    args=parser.parse_args()
    result=(status(args.root) if args.action=='status' else
            globals()[args.action](args.root,args.id))
    print(json.dumps(result,sort_keys=True))


if __name__=='__main__':main()
