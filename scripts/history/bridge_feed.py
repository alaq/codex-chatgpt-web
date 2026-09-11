"""Versioned, read-only visible-conversation feed for local consumers.

This module exports no raw nodes, browser credentials, vault matches, or file paths.
The collector remains the sole archive writer. Consumers own their delivery state.
"""
import json
import os
import re
import sqlite3
import stat
from pathlib import Path
from urllib.parse import urlsplit

from archive import UUID, VERSION, timestamp


CITATION = re.compile('\ue200cite(?:\ue202[^\ue200-\ue2ff\\s]+)+\ue201')


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


def read_feed(root):
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
        rows = db.execute('SELECT id,revision,normalized FROM conversations ORDER BY id LIMIT 10001').fetchall()
        if len(rows) > 10000:
            raise ValueError('Bridge feed conversation limit exceeded')
        conversations = []
        for cid, revision, encoded in rows:
            data = json.loads(encoded)
            if not UUID.fullmatch(cid) or cid != data.get('id') or not re.fullmatch('[a-f0-9]{64}', revision):
                raise ValueError('Invalid archived conversation identity')
            if data.get('incomplete') or not isinstance(data.get('title'), str):
                raise ValueError('Invalid or incomplete archived conversation')
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
                    'citation_groups': citation_groups(m),
                })
            conversations.append({
                'id': cid, 'revision': revision, 'title': data['title'],
                'created_at': timestamp(data['create_time']),
                'updated_at': timestamp(data['update_time']),
                'url': 'https://chatgpt.com/c/' + cid,
                'messages': messages,
            })
        return {
            'version': 1, 'source': 'chatgpt', 'account_key': key,
            'completed_watermark': float(meta['watermark']) if meta.get('watermark') else None,
            'coverage': 'regular saved history; project-only and archived-only discovery unverified',
            'conversations': conversations,
        }
    finally:
        db.close()
