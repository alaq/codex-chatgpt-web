"""Private bridge-only progress snapshots. The archive keeps completed revisions.

Only completed visible messages are staged. Partial answers, reasoning, tool traces,
session data and project matches never enter this feed. Checkpoints stay behind an
unfinished turn so a later poll must revisit it before advancing.
"""
import json
import os
from pathlib import Path
import stat
import sqlite3
import tempfile
import time
from archive import normalize, digest, UUID

def record_progress(root, envelope):
    data=json.loads(envelope['raw'])
    if not isinstance(data.get('mapping'),dict):return
    normalized,_,revision=normalize(data)
    directory=Path(root)/'bridge-progress'
    directory.mkdir(mode=0o700,exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid!=os.getuid() or directory.stat().st_mode&0o077:raise ValueError('Unsafe bridge progress directory')
    path=directory/(normalized['id']+'.json')
    if not normalized['incomplete']:
        path.unlink(missing_ok=True);return
    unfinished={n.get('message',{}).get('id') for n in data['mapping'].values() if n.get('message') and
        (n['message'].get('status') in ('in_progress','pending') or n['message'].get('metadata',{}).get('is_complete') is False)}
    normalized['messages']=[m for m in normalized['messages'] if m['id'] not in unfinished]
    # These structural fields and source references are not needed for mirroring.
    for field in ('active_nodes','current_node','gizmo_id'):normalized.pop(field,None)
    with sqlite3.connect((Path(root)/'archive.sqlite3').absolute().as_uri()+'?mode=ro',uri=True) as db:
        row=db.execute('SELECT revision FROM conversations WHERE id=?',(normalized['id'],)).fetchone()
    result={'account_key':envelope['accountKey'],'data':normalized,'revision':revision,'base_revision':row[0] if row else None,'observed_at':time.time(),'running':True}
    fd,name=tempfile.mkstemp(prefix='.progress-',dir=directory)
    try:
        with os.fdopen(fd,'w') as out:json.dump(result,out);out.flush();os.fsync(out.fileno())
        os.replace(name,path)
    finally:
        Path(name).unlink(missing_ok=True)

def read_progress(root,account):
    directory=Path(root)/'bridge-progress'
    if not directory.exists():return []
    info=directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:raise ValueError('Unsafe progress directory')
    entries=[]
    for file in sorted(directory.glob('*.json'))[:100]:
        info=file.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_size>32*1024*1024:raise ValueError('Unsafe progress record')
        entry=json.loads(file.read_text());cid=entry['data']['id']
        if entry['account_key']!=account or not UUID.fullmatch(cid) or file.stem!=cid:raise ValueError('Progress account or conversation mismatch')
        entry['running']=time.time()-entry['observed_at']<300
        entries.append(entry)
    return entries
