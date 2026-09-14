#!/usr/bin/env python3
"""On-demand saved ChatGPT collection through the isolated DEV launcher's read-only API."""
import argparse
import fcntl
import json
import os
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from archive import Archive, private_dir, review, catch_up
from bridge_feed import read_feed


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise RuntimeError('Local history control redirect rejected')


class Client:
    def __init__(self, descriptor, progress_root=None):
        self.progress_root=progress_root
        path = Path(descriptor).expanduser()
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('DEV descriptor must be an owner-only regular file')
        if info.st_size > 65536:
            raise ValueError('DEV descriptor is too large')
        d = json.loads(path.read_text())
        if d.get('version') != 3 or d.get('kind') != 'codex-web-gpt-launcher' or d.get('profile') != 'development' or d.get('partition') != 'persist:codex-web-gpt-dev-chatgpt':
            raise ValueError('History collection requires the isolated DEV profile')
        os.kill(d['pid'], 0)
        self.endpoint = d['control']['endpoint']
        url = urllib.parse.urlsplit(self.endpoint)
        if url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port or url.username or url.password or url.path not in ('', '/') or url.query or url.fragment:
            raise ValueError('DEV control must be a loopback-only endpoint')
        self.token = d['control']['token']
        if not isinstance(self.token, str) or len(self.token) < 40:
            raise ValueError('Invalid DEV control credential')
        self.opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))

    def __call__(self, body):
        req = urllib.request.Request(self.endpoint.rstrip('/')+'/v1/history/read', data=json.dumps(body).encode(),
                                     headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        try:
            with self.opener.open(req, timeout=65) as response:
                blob = response.read(40 * 1024 * 1024 + 1)
                if len(blob) > 40 * 1024 * 1024:
                    raise RuntimeError('Local history response exceeds size limit')
                result = json.loads(blob)
        except urllib.error.HTTPError as error:
            # No raw response bodies or authentication data in stdout/stderr.
            raise RuntimeError(f'Local history request rejected (HTTP {error.code}); check DEV login and collector enablement') from None
        except (urllib.error.URLError, TimeoutError):
            raise RuntimeError('Local history request failed or timed out') from None
        if result.get('version') != 1 or not isinstance(result.get('raw'), str):
            raise ValueError('Invalid history response envelope')
        if self.progress_root and body.get('operation')=='conversation':
            from bridge_progress import record_progress
            record_progress(self.progress_root,result)
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',default=str(Path.home()/'.local/share/chatgpt-conversation-archive'))
    parser.add_argument('--descriptor',default=str(Path.home()/'.local/share/codex-experiments/chatgpt-web/dev-profile/runtime/launcher-browser.json'))
    sub=parser.add_subparsers(dest='command',required=True)
    s=sub.add_parser('sync',help='Catch up on all new/updated saved chats in batches; first run starts now unless --since is given')
    s.add_argument('--since',help='ISO timestamp with timezone; optional historical start')
    s.add_argument('--max-pages',type=int,default=100,help='Safety cap on discovery depth per batch')
    s.add_argument('--batch-size','--max-conversations',dest='batch_size',type=int,default=20,help='Conversation fetches per batch (default 20); sync continues with further batches')
    s.add_argument('--max-batches',type=int,default=25,help='Safety cap; unfinished catch-up retains its previous checkpoint')
    s.add_argument('--page-size',type=int,default=20)
    s.add_argument('--refresh-known',action='store_true',help='Refetch known overlapping chats even when update timestamps match')
    s.add_argument('--bridge-progress',action='store_true',help='Stage completed visible messages from unfinished turns for the bridge; keep the archive checkpoint behind them')
    c=sub.add_parser('capture',help='Capture selected existing IDs without changing discovery checkpoint')
    c.add_argument('--id',action='append',required=True)
    sub.add_parser('status')
    sub.add_parser('feed', help='Read a versioned visible-message snapshot for local bridge consumers; no archive or network writes')
    r=sub.add_parser('review',help='Generate destination suggestions; never change vault state')
    r.add_argument('--vault',required=True)
    args=parser.parse_args()
    os.umask(0o077)
    if args.command == 'feed':
        print(json.dumps(read_feed(args.archive), ensure_ascii=False, allow_nan=False))
        return 0
    root=private_dir(args.archive)
    lock_path=root/'.collector.lock'
    if lock_path.is_symlink():
        raise ValueError('Archive lock must not be a symlink')
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another collector owns this archive') from None
        archive=Archive(root)
        try:
            if args.command=='sync':
                result=catch_up(archive,Client(args.descriptor,root if args.bridge_progress else None),args.since,args.batch_size,args.max_batches,args.max_pages,args.page_size,args.refresh_known)
                result['transcripts_written']=archive.render()
            elif args.command=='capture':
                if len(args.id)>10:
                    raise ValueError('Capture accepts at most ten explicit conversation IDs')
                client=Client(args.descriptor)
                result={'captured':[archive.ingest(client({'operation':'conversation','id':cid})) for cid in dict.fromkeys(args.id)]}
                result['transcripts_written']=archive.render()
            elif args.command=='review':
                rows=review(archive,args.vault)
                result={'review':str(root/'review.md'),'conversations':len(rows),'exact_matches':sum(r['status']=='exact_match' for r in rows),'suggestions':sum(r['status']=='suggested' for r in rows),'ambiguous':sum(r['status']=='ambiguous' for r in rows),'unfiled':sum(r['status']=='unfiled' for r in rows)}
            else:
                result={'archive':str(root),'conversations':archive.db.execute('SELECT count(*) FROM conversations').fetchone()[0],
                        'revisions':archive.db.execute('SELECT count(*) FROM revisions').fetchone()[0],
                        'node_versions':archive.db.execute('SELECT count(*) FROM node_versions').fetchone()[0],
                        'watermark':archive.meta('watermark'),'account_bound':archive.meta('account_key') is not None}
            print(json.dumps(result,indent=2))
            if args.command=='sync' and args.bridge_progress and result.get('errors') and all(e['error']=='Conversation is still generating; retry after completion' for e in result['errors']):
                return 0  # Expected progress; the completed watermark remains unchanged.
            return 0 if result.get('complete_window',True) else 2
        finally:
            archive.close()


if __name__=='__main__':
    try:
        sys.exit(main())
    except (ValueError,RuntimeError,OSError) as error:
        print(str(error),file=sys.stderr)
        sys.exit(1)
