"""Versioned, read-only visible-conversation feed for local consumers.

This module exports no raw nodes, browser credentials, vault matches, or file paths.
The collector remains the sole archive writer. Consumers own their delivery state.
"""
import json
import os
import re
import sqlite3
import stat
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

from archive import UUID, VERSION, timestamp, digest


CITATION = re.compile('\ue200cite(?:\ue202[^\ue200-\ue2ff\\s]+)+\ue201')


def delivery_fingerprint(kind, revision):
    return digest(['bridge-delivery-v1', kind, revision])


def citation_groups(message):
    """Export only visible citation markers and their web source titles/URLs.

    Do not forward raw reference objects: they can contain snippets, tool context,
    attachment references and other private metadata. Existing archive rows already
    preserve these references, so this additive feed field needs no archive rewrite.
    """
    groups = []
    refs = message.get('content_references')
    if not isinstance(refs, list):
        return groups
    for ref in refs[:1024]:
        if not isinstance(ref, dict) or ref.get('type') != 'grouped_webpages':
            continue
        marker = ref.get('matched_text')
        if not isinstance(marker, str) or len(marker) > 4096 or not CITATION.fullmatch(marker) or marker not in message['text']:
            continue
        sources, seen = [], set()
        items = ref.get('items')
        if not isinstance(items, list):
            continue
        for item in items[:64]:
            if not isinstance(item, dict):
                continue
            url = item.get('url')
            if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 33 or ord(c) == 127 for c in url):
                continue
            try:
                parsed = urlsplit(url)
                if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username is not None or parsed.password is not None:
                    continue
            except ValueError:
                continue
            if url in seen:
                continue
            seen.add(url)
            title = item.get('attribution') or item.get('title') or parsed.hostname
            if not isinstance(title, str):
                title = parsed.hostname
            sources.append({'title': ' '.join(title.split())[:300], 'url': url})
        if sources:
            groups.append({'marker': marker, 'sources': sources})
    return groups


def export_attachments(message):
    result=[{'id':a['id'],'name':str(a.get('name') or 'attachment'),'mime_type':str(a.get('mime_type') or 'application/octet-stream'),'size':a['size']}
        for a in (message.get('attachments') or []) if isinstance(a,dict) and isinstance(a.get('id'),str)
        and re.fullmatch(r'file[-_][A-Za-z0-9_-]{8,128}',a['id']) and isinstance(a.get('size'),int) and 0<=a['size']<=20*1024*1024]
    for match in re.finditer(r'\]\((sandbox:/mnt/data/[^)\n]+)\)',message['text']):
        asset=match[1]
        if not re.fullmatch(r'sandbox:/mnt/data/(?:[A-Za-z0-9_. -]+/)*[A-Za-z0-9_. -]+',asset) or '..' in asset.split('/'):continue
        if not any(a['id']==asset for a in result):result.append({'id':asset,'name':asset.rsplit('/',1)[1],'mime_type':mimetypes.guess_type(asset)[0] or 'application/octet-stream','size':0})
    return result

def export_conversation(cid, revision, data):
    messages, seen = [], set()
    for m in data['messages']:
        mid = m.get('id')
        if not isinstance(mid, str) or not mid or mid in seen or m.get('role') not in ('user', 'assistant') or not isinstance(m.get('text'), str):
            raise ValueError('Invalid visible message in archive')
        seen.add(mid)
        messages.append({
            'id': mid, 'role': m['role'], 'text': m['text'],
            'created_at': timestamp(m['create_time']) if m.get('create_time') is not None else None,
            'attachment_count': len(m.get('attachments') or []),
            'attachments': export_attachments(m),
            'citation_groups': citation_groups(m),
        })
    return {
        'kind': 'work' if data.get('conversation_origin') == 'tpp' else 'chatgpt',
        'id': cid, 'revision': revision, 'title': data['title'],
        'created_at': timestamp(data['create_time']),
        'updated_at': timestamp(data['update_time']),
        'url': 'https://chatgpt.com/c/' + cid,
        'messages': messages,
    }


def read_feed(root, max_conversations=10, allowed_conversations=None, known_fingerprints=None):
    if not isinstance(max_conversations, int) or isinstance(max_conversations, bool) or not 1 <= max_conversations <= 100:
        raise ValueError('Bridge feed conversation limit must be between 1 and 100')
    allowed = [item for item in (allowed_conversations or []) if isinstance(item, str) and UUID.fullmatch(item)]
    root = Path(root).expanduser().absolute()
    path = root / 'archive.sqlite3'
    for item in (root, path):
        info = item.lstat()
        if item.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Bridge feed requires an owner-only archive')
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('Bridge feed requires a regular archive database')
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        meta = dict(db.execute('SELECT key,value FROM meta'))
        if meta.get('schema_version') != str(VERSION):
            raise ValueError('Unsupported archive schema')
        key = meta.get('account_key', '')
        if not re.fullmatch('[a-f0-9]{64}', key):
            raise ValueError('Archive has no verified account; run collection first')
        metadata = db.execute('''SELECT id,revision,source_update FROM (
            SELECT id,revision,source_update FROM conversations
            WHERE (?=0 OR id IN (SELECT value FROM json_each(?)))
            ORDER BY source_update DESC,id DESC LIMIT ?
          ) ORDER BY source_update,id''',
          (int(bool(allowed_conversations)), json.dumps(allowed), max_conversations)).fetchall()
        from bridge_progress import progress_candidates, progress_delivery_fingerprint, read_progress
        progress_meta = progress_candidates(root, allowed if allowed_conversations else None)
        candidates = {cid: (False, updated, cid) for cid, _revision, updated in metadata}
        for entry in progress_meta:
            candidates[entry['id']] = (entry['running'], entry['observed_at'], entry['id'])
        selected = {item[2] for item in sorted(candidates.values())[-max_conversations:]}
        progress_by_id = {entry['id']: entry for entry in progress_meta if entry['id'] in selected}
        rows = db.execute('''SELECT id,revision,source_update FROM conversations
            WHERE id IN (SELECT value FROM json_each(?)) ORDER BY source_update,id''',
            (json.dumps(sorted(selected)),)).fetchall()
        changed_archive_ids = []
        conversations = []
        for cid, revision, source_update in rows:
            progress_candidate = progress_by_id.get(cid)
            if progress_candidate and (known_fingerprints or {}).get(cid) == progress_candidate['delivery_fingerprint'] and progress_candidate['delivery_fingerprint'] == progress_delivery_fingerprint(progress_candidate['path'].lstat()):
                continue
            fingerprint = delivery_fingerprint('chatgpt', revision)
            if (known_fingerprints or {}).get(cid) == fingerprint:
                conversations.append({'id': cid, 'delivery_fingerprint': fingerprint,
                                      'unchanged': True, 'updated_at': source_update,
                                      'messages': []})
                continue
            changed_archive_ids.append(cid)
        payloads = {} if not changed_archive_ids else dict(db.execute('''SELECT id,normalized FROM conversations
            WHERE id IN (SELECT value FROM json_each(?))''',
            (json.dumps(changed_archive_ids),)).fetchall())
        for cid, revision, source_update in rows:
            if cid not in changed_archive_ids:
                continue
            encoded = payloads[cid]
            fingerprint = delivery_fingerprint('chatgpt', revision)
            data = json.loads(encoded)
            if not UUID.fullmatch(cid) or cid != data.get('id') or not re.fullmatch('[a-f0-9]{64}', revision):
                raise ValueError('Invalid archived conversation identity')
            if data.get('incomplete') or not isinstance(data.get('title'), str):
                raise ValueError('Invalid or incomplete archived conversation')
            item = export_conversation(cid, revision, data)
            item['delivery_fingerprint'] = fingerprint
            conversations.append(item)
        selected_progress = list(progress_by_id.values())
        unchanged_progress_ids = {entry['id'] for entry in selected_progress
                                  if (known_fingerprints or {}).get(entry['id']) == entry['delivery_fingerprint']
                                  and entry['delivery_fingerprint'] == progress_delivery_fingerprint(entry['path'].lstat())}
        changed_progress = [entry for entry in selected_progress if entry['id'] not in unchanged_progress_ids]
        progress = read_progress(root, key, candidates=changed_progress)
        revisions={cid:revision for cid,revision,_ in rows}
        for entry in progress:
            cid = entry['data']['id']
            if cid not in revisions:
                row = db.execute('SELECT revision FROM conversations WHERE id=?', (cid,)).fetchone()
                if row:
                    revisions[cid] = row[0]
        for entry in progress:
            if revisions.get(entry['data']['id'])!=entry.get('base_revision'):continue
            conversations = [c for c in conversations if c["id"] != entry["data"]["id"]]
            item = export_conversation(entry["data"]["id"], entry["revision"], entry["data"])
            item["running"] = entry["running"]
            candidate = next(candidate for candidate in selected_progress if candidate['id'] == entry['data']['id'])
            if candidate['delivery_fingerprint'] == progress_delivery_fingerprint(candidate['path'].lstat()):
                item["delivery_fingerprint"] = candidate['delivery_fingerprint']
            conversations.append(item)
        opened_progress_ids = {entry['id'] for entry in changed_progress}
        for candidate in selected_progress:
            if candidate['id'] in opened_progress_ids:
                continue
            conversations = [c for c in conversations if c['id'] != candidate['id']]
            conversations.append({'id': candidate['id'], 'delivery_fingerprint': candidate['delivery_fingerprint'],
                                  'unchanged': True, 'running': candidate['running'], 'running_known': True,
                                  'updated_at': candidate['observed_at'], 'messages': []})
        conversations.sort(key=lambda item: (bool(item.get('running')), item['updated_at'], item['id']))
        conversations = conversations[-max_conversations:]
        conversations.sort(key=lambda item: (item['updated_at'], item['id']))
        return {
            'version': 1, 'source': 'chatgpt', 'account_key': key,
            'completed_watermark': float(meta['watermark']) if meta.get('watermark') else None,
            'coverage': 'regular saved history; project-only and archived-only discovery unverified',
            'conversations': conversations,
        }
    finally:
        db.close()
