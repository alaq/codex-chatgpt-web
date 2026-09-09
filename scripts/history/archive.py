"""Private, incremental ChatGPT saved-history archive. No third-party dependencies."""
from __future__ import annotations
import hashlib
import json
import math
import os
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

VERSION = 1
UUID = re.compile(r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$', re.I)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256((value if isinstance(value, str) else encode(value)).encode()).hexdigest()


def timestamp(value):
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('Source timestamp is missing its timezone')
        return parsed.timestamp()
    raise ValueError('Source timestamp is missing or malformed')


def iso(value):
    return datetime.fromtimestamp(timestamp(value), timezone.utc).isoformat()


def normalize(data):
    cid = data.get('conversation_id') or data.get('id')
    if not isinstance(cid, str) or not UUID.fullmatch(cid):
        raise ValueError('Invalid conversation ID')
    mapping = data.get('mapping')
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError('Missing conversation tree')
    current = data.get('current_node')
    if not isinstance(current, str) or current not in mapping:
        raise ValueError('Missing active branch; refusing to guess from leaf order')
    chain, seen = [], set()
    while current is not None:
        if current in seen or current not in mapping:
            raise ValueError('Cyclic or incomplete active branch')
        seen.add(current)
        node = mapping[current]
        if not isinstance(node, dict):
            raise ValueError('Malformed conversation node')
        chain.append(current)
        current = node.get('parent')
    chain.reverse()
    messages, incomplete = [], False
    message_ids = set()
    for nid in chain:
        m = mapping[nid].get('message')
        if not m:
            continue
        role = m.get('author', {}).get('role')
        content = m.get('content', {})
        metadata = m.get('metadata') or {}
        if role not in ('user', 'assistant') or metadata.get('is_visually_hidden_from_conversation'):
            continue
        if m.get('channel') in ('analysis', 'justify', 'confidence') or m.get('recipient', 'all') not in ('all', None):
            continue
        content_type = content.get('content_type')
        if content_type in ('thoughts', 'reasoning_recap', 'reasoning', 'system_error'):
            continue
        if m.get('status') in ('in_progress', 'pending') or (role == 'assistant' and metadata.get('is_complete') is False):
            incomplete = True
        parts = content.get('parts', [])
        text_parts = [p if isinstance(p, str) else f"[Non-text content: {p.get('content_type', 'unknown')}; see raw source]" for p in parts if isinstance(p, (str, dict))]
        if content_type == 'code' and isinstance(content.get('text'), str):
            text_parts = ['```' + (content.get('language') or '') + '\n' + content['text'] + '\n```']
        attachments = metadata.get('attachments') or []
        if not text_parts and not attachments:
            if content_type not in ('text', 'multimodal_text'):
                text_parts = [f'[Content type: {content_type}; see raw source]']
            else:
                continue
        mid = m.get('id')
        if not isinstance(mid, str) or not mid or mid in message_ids:
            raise ValueError('Missing or duplicate active message ID')
        message_ids.add(mid)
        messages.append({'node_id': nid, 'id': m.get('id'), 'role': role,
                         'create_time': m.get('create_time'), 'update_time': m.get('update_time'),
                         'text': '\n'.join(text_parts), 'attachments': attachments,
                         'content_references': metadata.get('content_references') or [],
                         'citations': metadata.get('citations') or []})
    normalized = {'version': VERSION, 'id': cid, 'title': data.get('title') or 'Untitled',
                  'create_time': data.get('create_time'), 'update_time': data.get('update_time'),
                  'current_node': data['current_node'], 'messages': messages,
                  'active_nodes': chain, 'total_nodes': len(mapping),
                  'branch_nodes': len(mapping) - len(chain), 'incomplete': incomplete,
                  'is_archived': data.get('is_archived', False), 'gizmo_id': data.get('gizmo_id'),
                  'conversation_origin': data.get('conversation_origin'),
                  'attachment_coverage': 'references only; binary files not downloaded'}
    # All source nodes are versioned, including alternative branches. Hidden reasoning stays
    # in private raw evidence and is never included in readable transcripts or match text.
    node_hashes = {nid: digest(node) for nid, node in mapping.items()}
    semantic_hash = digest({'normalized': normalized, 'nodes': node_hashes})
    return normalized, node_hashes, semantic_hash


def private_dir(path):
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise ValueError('Archive directory must not be a symlink')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def write_changed(path, content):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Refusing to overwrite a symlink')
    blob = content.encode()
    if path.exists() and path.read_bytes() == blob:
        return False
    temp = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return True


class Archive:
    def __init__(self, root):
        self.root = private_dir(root)
        db = self.root / 'archive.sqlite3'
        if db.is_symlink():
            raise ValueError('Archive database must not be a symlink')
        fd = os.open(db, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(db, 0o600)
        self.db = sqlite3.connect(db)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY, revision TEXT NOT NULL, source_update REAL NOT NULL,
            normalized TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS revisions (
            conversation_id TEXT NOT NULL, revision TEXT NOT NULL, raw_sha256 TEXT NOT NULL,
            raw_json TEXT NOT NULL, normalized TEXT NOT NULL, node_hashes TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT NOT NULL, source_path TEXT NOT NULL,
            PRIMARY KEY(conversation_id,revision));
          CREATE TABLE IF NOT EXISTS node_versions (
            conversation_id TEXT NOT NULL, node_id TEXT NOT NULL, hash TEXT NOT NULL,
            node_json TEXT NOT NULL, PRIMARY KEY(conversation_id,node_id,hash));
        ''')
        version = self.meta('schema_version')
        if version is not None and version != str(VERSION):
            raise ValueError('Unsupported archive version')
        self.set_meta('schema_version', str(VERSION))

    def close(self):
        self.db.close()

    def meta(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        if self.meta(key) == value:
            return False
        with self.db:
            self.db.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
        return True

    def check_account(self, key):
        if not isinstance(key, str) or not re.fullmatch('[a-f0-9]{64}', key):
            raise ValueError('Missing account identity')
        old = self.meta('account_key')
        if old and old != key:
            raise ValueError('ChatGPT account changed; use a separate archive')
        return key

    def current(self, cid):
        row = self.db.execute('SELECT revision,source_update,normalized FROM conversations WHERE id=?', (cid,)).fetchone()
        return {'revision': row[0], 'source_update': row[1], 'data': json.loads(row[2])} if row else None

    def ingest(self, envelope):
        key = self.check_account(envelope['accountKey'])
        data = json.loads(envelope['raw'])
        normalized, node_hashes, revision = normalize(data)
        if normalized['incomplete']:
            raise ValueError('Conversation is still generating; retry after completion')
        cid = normalized['id']
        source_update = timestamp(data.get('update_time'))
        previous = self.current(cid)
        if previous and previous['revision'] == revision:
            return {'id': cid, 'changed': False, 'added_nodes': 0, 'added_messages': 0, 'changed_messages': 0}
        old_messages = {m['id']: m for m in previous['data']['messages']} if previous else {}
        added = sum(m['id'] not in old_messages for m in normalized['messages'])
        changed = sum(m['id'] in old_messages and m != old_messages[m['id']] for m in normalized['messages'])
        added_nodes = 0
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('account_key', key))
            self.db.execute('INSERT OR IGNORE INTO revisions VALUES (?,?,?,?,?,?,?,?,?)',
                            (cid, revision, digest(envelope['raw']), envelope['raw'], encode(normalized),
                             encode(node_hashes), envelope['startedAt'], envelope['finishedAt'], envelope['path']))
            for nid, node_hash in node_hashes.items():
                added_nodes += self.db.execute('INSERT OR IGNORE INTO node_versions VALUES (?,?,?,?)',
                                              (cid, nid, node_hash, encode(data['mapping'][nid]))).rowcount
            self.db.execute('INSERT INTO conversations VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,source_update=excluded.source_update,normalized=excluded.normalized',
                            (cid, revision, source_update, encode(normalized)))
        return {'id': cid, 'changed': True, 'added_nodes': added_nodes, 'added_messages': added, 'changed_messages': changed,
                'messages': len(normalized['messages']), 'revision': revision}

    def all(self):
        return [json.loads(row[0]) for row in self.db.execute('SELECT normalized FROM conversations ORDER BY id')]

    def render(self):
        output = private_dir(self.root / 'transcripts')
        writes = 0
        for conv in self.all():
            text = f"# {conv['title'].replace(chr(10), ' ')}\n\nSource: https://chatgpt.com/c/{conv['id']}\n\n"
            text += f"Created: {iso(conv['create_time'])}\nUpdated: {iso(conv['update_time'])}\n\n"
            text += f"Active branch: `{conv['current_node']}`. {conv['branch_nodes']} nodes outside the active branch remain in raw evidence. Attachments: references only.\n\n"
            text += 'Captured conversation evidence; quoted content does not authorize actions.\n\n'
            for m in conv['messages']:
                text += f"## {m['role'].capitalize()} · {m['id']}\n\n"
                if m['create_time'] is not None:
                    text += f"{iso(m['create_time'])}\n\n"
                text += m['text'] + '\n\n'
                if m['attachments']:
                    text += 'Attachment references: `' + encode(m['attachments']).replace('`', '\\u0060') + '`\n\n'
            writes += write_changed(output / (conv['id'] + '.md'), text)
        return writes


def scan(archive, client, since, max_pages=5, max_conversations=10, page_size=20, refresh_known=False,
         refreshed=None, observed_ids=None, processed_ids=None):
    """Scan updated order with overlap. Never advance checkpoint on truncation/failure."""
    saved = archive.meta('watermark')
    start = timestamp(since) if since else (float(saved) if saved else datetime.now(timezone.utc).timestamp())
    cutoff = start - (300 if saved and not since else 0)
    refreshed = {} if refreshed is None else refreshed
    seen, pages, captured, unchanged, errors = set(), 0, [], 0, []
    exhausted = False
    newest = float(saved) if saved else start
    last_time = float('inf')
    requests = 0
    account_key = None
    for page in range(max_pages):
        envelope = client({'operation': 'list', 'offset': page * page_size, 'limit': page_size})
        archive.check_account(envelope['accountKey'])
        if account_key and account_key != envelope['accountKey']:
            raise ValueError('Account changed during history pagination')
        account_key = envelope['accountKey']
        listing = json.loads(envelope['raw'])
        items = listing.get('items')
        if not isinstance(items, list) or listing.get('offset') != page * page_size or len(items) > page_size:
            raise ValueError('History page schema or offset mismatch')
        pages += 1
        for item in items:
            cid = item.get('id')
            if not isinstance(cid, str) or not UUID.fullmatch(cid):
                raise ValueError('Invalid listed conversation ID')
            updated = timestamp(item.get('update_time'))
            if updated > last_time:
                raise ValueError('History is not ordered by update time; rerun before advancing checkpoint')
            last_time = updated
            if updated < cutoff:
                exhausted = True
                break
            if cid in seen:
                continue
            seen.add(cid)
            if item.get('is_temporary_chat'):
                continue
            if observed_ids is not None:
                observed_ids.add(cid)
            old = archive.current(cid)
            if old and old['source_update'] >= updated and (not refresh_known or refreshed.get(cid, float('-inf')) >= updated):
                unchanged += 1
                if processed_ids is not None:
                    processed_ids.add(cid)
                newest = max(newest, updated)
                continue
            if requests >= max_conversations:
                errors.append({'id': cid, 'error': 'conversation request budget reached'})
                break
            requests += 1
            try:
                response = client({'operation': 'conversation', 'id': cid})
                if response['accountKey'] != account_key:
                    raise ValueError('Account changed during conversation fetch')
                body = json.loads(response['raw'])
                if (body.get('conversation_id') or body.get('id')) != cid:
                    raise ValueError('Fetched conversation ID mismatch')
                if timestamp(body.get('update_time')) < updated:
                    raise ValueError('Fetched conversation is older than its history entry; retry')
                result = archive.ingest(response)
                refreshed[cid] = updated
                if processed_ids is not None:
                    processed_ids.add(cid)
                if result['changed']:
                    captured.append(result)
                else:
                    unchanged += 1
                newest = max(newest, updated)
            except (ValueError, RuntimeError) as error:
                errors.append({'id': cid, 'error': str(error)})
        if errors or exhausted:
            break
        # The API's `total` can be offset+limit+1, not a global count. Never use it
        # as a completeness claim; require a short page or crossing the time boundary.
        if len(items) < page_size:
            exhausted = True
            break
    complete = exhausted and not errors
    if complete:
        if account_key:
            archive.set_meta('account_key', account_key)
        archive.set_meta('watermark', str(newest))
    return {'complete_window': complete, 'pages': pages, 'discovered': len(seen), 'fetched': requests,
            'captured': captured, 'unchanged': unchanged, 'errors': errors,
            'checkpoint_advanced': complete and archive.meta('watermark') != saved,
            'cutoff': iso(cutoff), 'coverage': 'updated regular history endpoint; project/archived-only discovery unverified'}


def catch_up(archive, client, since=None, batch_size=20, max_batches=25, max_pages=100,
             page_size=20, refresh_known=False):
    """Drain a fixed update window in bounded batches, advancing only on completion.

    Revisit the list head between batches because old conversations can move forward.
    Successfully stored versions are the restart checkpoint; offsets are not durable.
    """
    if not 1 <= batch_size <= 100 or not 1 <= max_batches <= 100 or not 1 <= max_pages <= 100 or not 1 <= page_size <= 50:
        raise ValueError('Invalid catch-up bounds')
    original = archive.meta('watermark')
    start = timestamp(since) if since else (float(original) if original else datetime.now(timezone.utc).timestamp())
    cutoff = start - (300 if original and not since else 0)
    pending = archive.meta('pending_cutoff')
    if pending is not None:
        cutoff = min(cutoff, float(pending))
    # Ordinary retries can recover from the completed watermark. A first run or
    # explicit backfill also needs its older boundary to survive process exit.
    if original is None or since is not None or pending is not None:
        archive.set_meta('pending_cutoff', str(cutoff))
    window_since = iso(cutoff)
    refreshed, observed, processed, captured = {}, set(), set(), []
    page_limit = min(5, max_pages)
    pages = fetched = 0
    complete, stop_reason, errors = False, 'batch_limit', []
    for batch in range(1, max_batches + 1):
        result = scan(archive, client, window_since, page_limit, batch_size, page_size,
                      refresh_known, refreshed, observed, processed)
        pages += result['pages']
        fetched += result['fetched']
        captured.extend(result['captured'])
        if result['complete_window']:
            complete, stop_reason = True, 'caught_up'
            break
        errors = [e for e in result['errors'] if e['error'] != 'conversation request budget reached']
        if errors:
            stop_reason = 'source_error'
            break
        if not result['errors']:
            if page_limit >= max_pages:
                stop_reason = 'page_limit'
                break
            page_limit = min(max_pages, page_limit * 2)
    if complete and archive.meta('pending_cutoff') is not None:
        with archive.db:
            archive.db.execute("DELETE FROM meta WHERE key='pending_cutoff'")
    changed_ids = {r['id'] for r in captured}
    return {'complete_window': complete, 'backlog_remaining': not complete,
            'stop_reason': stop_reason, 'batches': batch, 'batch_size': batch_size,
            'pages': pages, 'discovered': len(observed), 'fetched': fetched,
            'captured': captured, 'changed_conversations': len(changed_ids),
            'unchanged': len(processed - changed_ids),
            'pending_observed': len(observed - processed), 'errors': errors,
            'checkpoint_before': iso(float(original)) if original else None,
            'checkpoint_after': iso(float(archive.meta('watermark'))) if archive.meta('watermark') else None,
            'checkpoint_advanced': archive.meta('watermark') != original,
            'cutoff': window_since,
            'coverage': 'updated regular history endpoint; project/archived-only discovery unverified'}


STOP = set('a an the and or in on at to for of with from is are it this that my your i me we you how can would what do does should about use using into have be as by all but not just then than already also could has had will want which there their our get got let new one some any such more really much yes no give add com https http www github'.split())


def tokens(text):
    return set(t for t in re.findall(r'[a-z][a-z0-9]{2,}', text.lower()) if t not in STOP)


def vault_index(vault):
    """Read canonical briefings and dedicated provenance; follow legacy redirects."""
    vault = Path(vault).resolve()
    controls = {}
    # Prefer an explicit summary; retain index/project when they are the only briefing.
    priority = {'summary.md': 0, 'evergreen.md': 1, 'index.md': 2, 'project.md': 3}
    for area in ('projects', 'ideas', 'evergreens', 'archive'):
        paths = sorted((vault / area).rglob('*.md'), key=lambda p: (priority.get(p.name, 9), str(p)))
        for path in paths:
            if path.name not in priority or path.parent in controls:
                continue
            text = path.read_text()
            redirect = re.search(r'^type:\s*[\'\"]?redirect', text, re.M)
            if not redirect and not (path.parent / 'tasks.md').exists() and not (path.parent / 'todos.md').exists():
                continue
            controls[path.parent] = (path, text)

    def resolve(path, text, seen=None):
        seen = set() if seen is None else seen
        if path in seen:
            raise ValueError('Cyclic vault redirect')
        seen.add(path)
        if re.search(r'^type:\s*[\'\"]?redirect', text, re.M):
            found = re.search(r'^canonical:\s*[\'\"]?\[\[([^\]|]+)', text, re.M)
            if not found:
                raise ValueError('Vault redirect is missing canonical target')
            target = found.group(1).split('#')[0]
            dest = ((path.parent if target.startswith('.') else vault) / target).resolve()
            if dest.suffix != '.md':
                dest = dest.with_suffix('.md')
            if not dest.is_relative_to(vault) or not dest.is_file():
                raise ValueError('Vault redirect target is missing or outside vault')
            return resolve(dest, dest.read_text(), seen)
        return str(path.relative_to(vault))[:-3]

    exact = defaultdict(set)
    docs = []
    for folder, (path, text) in controls.items():
        target = resolve(path, text)
        is_redirect = re.search(r'^type:\s*[\'\"]?redirect', text, re.M)
        evidence = [text]
        source_dir = folder / 'source-data'
        if source_dir.exists():
            evidence.extend(p.read_text() for p in source_dir.glob('*.md') if p.stat().st_size < 2_000_000)
        for body in evidence:
            for found in re.findall(r'^\s*(?:-\s*)?(?:backing_conversation_id|conversation_id|Backing conversation ID|Conversation ID):\s*[`\'\"]?([a-f0-9-]{36})', body, re.I | re.M):
                if UUID.fullmatch(found):
                    exact[found.lower()].add(target)
        if is_redirect:
            continue
        title_match = re.search(r'^#\s+(.+)$', text, re.M)
        title = title_match.group(1) if title_match else folder.name
        docs.append({'path': target, 'title': title, 'tokens': tokens(title + ' ' + text[:10000])})
    return exact, docs


def review(archive, vault):
    exact, docs = vault_index(vault)
    frequencies = Counter(t for d in docs for t in d['tokens'])
    rows = []
    for conv in archive.all():
        owners = sorted(exact.get(conv['id'], []))
        query = tokens(conv['title'] + ' ' + ' '.join(m['text'][:1000] for m in conv['messages'] if m['role'] == 'user')[:4000])
        title_tokens = tokens(conv['title'])
        ranked = []
        for doc in docs:
            matched = query & doc['tokens']
            score = sum(math.log((len(docs)+1)/(frequencies[t]+1)) * (2 if t in title_tokens else 1) for t in matched)
            if doc['path'].startswith('archive/'):
                score *= 0.65
            if score >= 12 and len(matched) >= 3:
                ranked.append({'destination': doc['path'], 'score': round(score, 2), 'evidence': 'Shared terms: ' + ', '.join(sorted(matched)[:12])})
        ranked.sort(key=lambda r: (-r['score'], r['destination']))
        if len(owners) == 1:
            status, confidence = 'exact_match', 'high'
            candidates = [{'destination': owners[0], 'evidence': 'Exact backing conversation ID in existing capture provenance'}]
        elif owners:
            status, confidence = 'ambiguous', 'review'
            candidates = [{'destination': p, 'evidence': 'Same conversation ID claimed by multiple containers'} for p in owners]
        else:
            status, confidence = ('suggested', 'review') if ranked else ('unfiled', 'none')
            if len(ranked) > 1:
                status = 'ambiguous'
            candidates = ranked[:3]
        rows.append({'id':conv['id'], 'title':conv['title'], 'source':f"https://chatgpt.com/c/{conv['id']}",
                     'created_at':iso(conv['create_time']), 'updated_at':iso(conv['update_time']),
                     'status':status,'confidence':confidence,'candidates':candidates,
                     'decision':'pending: accept, change destination, or leave unfiled',
                     'excerpt':next((m['text'][:180] for m in conv['messages'] if m['role']=='user'),'')})
    write_changed(archive.root/'review.json', json.dumps({'version':VERSION,'rows':rows},ensure_ascii=False,indent=2)+'\n')
    def cell(s):
        return str(s).replace('|','\\|').replace('\n',' ').replace('\r',' ')
    text='# Conversation matching review\n\nSuggestions only. No project state or transcript destinations have been changed.\n\n'
    text+='| Conversation | Updated (UTC) | Result / confidence | Proposed destination(s) | Match evidence | Source excerpt | Decision |\n|---|---|---|---|---|---|---|\n'
    for row in rows:
        text+='| '+ ' | '.join([f"[{cell(row['title'])}]({row['source']})",row['updated_at'],row['status']+' / '+row['confidence'],cell('; '.join(c['destination'] for c in row['candidates'])),cell('; '.join(c['evidence'] for c in row['candidates'])), cell(row['excerpt']), 'Accept / change / leave unfiled'])+' |\n'
    write_changed(archive.root/'review.md',text)
    return rows
