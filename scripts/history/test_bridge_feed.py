import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from archive import Archive
from bridge_feed import read_feed, citation_groups
from test_archive import conversation, envelope, node, fingerprint, ID, KEY


class BridgeFeedTests(unittest.TestCase):
    def test_progress_has_only_finished_messages_and_completed_archive_wins(self):
        from bridge_progress import record_progress
        data=conversation();data['conversation_origin']='tpp';self.archive.ingest(envelope(data))
        data['mapping']['u2']=node('u2','user','New request','a')
        data['mapping']['partial']=node('partial','assistant','UNFINISHED DO NOT MIRROR','u2')
        data['mapping']['partial']['message']['status']='in_progress'
        data['current_node']='partial';data['update_time']=3000
        record_progress(self.root,envelope(data))
        feed=read_feed(self.root)['conversations'][0]
        self.assertEqual(feed['kind'],'work');self.assertTrue(feed['running']);self.assertEqual(len(feed['messages']),3)
        self.assertNotIn('UNFINISHED',json.dumps(feed))
        self.assertEqual(len(self.archive.current(ID)['data']['messages']),2)
        data['mapping']['partial']['message']['status']='finished_successfully';data['mapping']['partial']['message']['content']['parts']=['Finished answer']
        self.archive.ingest(envelope(data))  # A separate non-bridge collector may finish it.
        completed=read_feed(self.root)['conversations'][0]
        self.assertFalse(completed.get('running',False));self.assertEqual(completed['messages'][-1]['text'],'Finished answer')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.archive = Archive(self.root)

    def tearDown(self):
        self.archive.close()
        self.temp.cleanup()

    def test_read_only_stable_snapshot_with_no_raw_or_credentials(self):
        data = conversation()
        data['mapping']['a']['message']['metadata']['attachments'] = [{'secret_url': 'do-not-export'}]
        self.archive.ingest(envelope(data))
        before = fingerprint(self.root)
        feed = read_feed(self.root)
        self.assertEqual(feed, read_feed(self.root))
        self.assertEqual(before, fingerprint(self.root))
        self.assertEqual(feed['account_key'], KEY)
        self.assertEqual(feed['conversations'][0]['messages'][1]['attachment_count'], 1)
        text = json.dumps(feed)
        self.assertNotIn('do-not-export', text)
        self.assertNotIn('mapping', text)
        self.assertNotIn(str(self.root), text)

    def test_feed_exports_only_the_ten_most_recent_conversations(self):
        for index in range(12):
            data = conversation()
            data['conversation_id'] = f'00000000-0000-4000-8000-{index:012d}'
            data['update_time'] = 1000 + index
            self.archive.ingest(envelope(data))
        ids = [item['id'] for item in read_feed(self.root, max_conversations=10)['conversations']]
        self.assertEqual(ids, [f'00000000-0000-4000-8000-{index:012d}' for index in range(2, 12)])

    def test_allowlist_is_applied_before_the_feed_limit(self):
        for index in range(12):
            data = conversation()
            data['conversation_id'] = f'00000000-0000-4000-8000-{index:012d}'
            data['update_time'] = 1000 + index
            self.archive.ingest(envelope(data))
        allowed = ['00000000-0000-4000-8000-000000000000']
        ids = [item['id'] for item in read_feed(self.root, 10, allowed)['conversations']]
        self.assertEqual(ids, allowed)

    def test_progress_limit_is_applied_before_transcript_decode(self):
        from bridge_progress import record_progress
        paths = []
        for index in range(12):
            data = conversation()
            cid = f'00000000-0000-4000-8000-{index:012d}'
            data['conversation_id'] = cid
            data['update_time'] = 1000 + index
            self.archive.ingest(envelope(data))
            data['mapping']['partial'] = node('partial', 'assistant', 'unfinished', 'a')
            data['mapping']['partial']['message']['status'] = 'in_progress'
            data['current_node'] = 'partial'
            record_progress(self.root, envelope(data))
            path = self.root / 'bridge-progress' / f'{cid}.json'
            os.utime(path, (1000 + index, 1000 + index))
            paths.append(path)
        paths[0].write_text('excluded malformed progress')
        paths[1].write_text('excluded malformed progress')
        os.utime(paths[0], (1000, 1000))
        os.utime(paths[1], (1001, 1001))
        feed = read_feed(self.root, max_conversations=10)['conversations']
        self.assertEqual(len(feed), 10)

    def test_progress_allowlist_is_applied_before_transcript_decode(self):
        from bridge_progress import record_progress
        allowed = '00000000-0000-4000-8000-000000000000'
        for index in range(12):
            data = conversation()
            cid = f'00000000-0000-4000-8000-{index:012d}'
            data['conversation_id'] = cid
            data['update_time'] = 1000 + index
            self.archive.ingest(envelope(data))
            data['mapping']['partial'] = node('partial', 'assistant', 'unfinished', 'a')
            data['mapping']['partial']['message']['status'] = 'in_progress'
            data['current_node'] = 'partial'
            record_progress(self.root, envelope(data))
            if cid != allowed:
                (self.root / 'bridge-progress' / f'{cid}.json').write_text('excluded malformed progress')
        feed = read_feed(self.root, 10, [allowed])['conversations']
        self.assertEqual([item['id'] for item in feed], [allowed])

    def test_running_progress_can_displace_an_older_completed_candidate(self):
        from bridge_progress import record_progress
        active = conversation()
        active['update_time'] = 1
        self.archive.ingest(envelope(active))
        for index in range(10):
            data = conversation()
            data['conversation_id'] = f'00000000-0000-4000-8000-{index:012d}'
            data['update_time'] = 1000 + index
            self.archive.ingest(envelope(data))
        active['mapping']['partial'] = node('partial', 'assistant', 'unfinished', 'a')
        active['mapping']['partial']['message']['status'] = 'in_progress'
        active['current_node'] = 'partial'
        active['update_time'] = 2
        record_progress(self.root, envelope(active))
        feed = read_feed(self.root, max_conversations=10)['conversations']
        self.assertEqual(len(feed), 10)
        self.assertIn(ID, [item['id'] for item in feed])
        self.assertTrue(next(item for item in feed if item['id'] == ID)['running'])

    def test_continued_old_chat_keeps_ids_and_adds_visible_turn(self):
        data = conversation()
        self.archive.ingest(envelope(data))
        first = read_feed(self.root)['conversations'][0]
        data['mapping']['u2'] = node('u2', 'user', 'continued', 'a')
        data['current_node'] = 'u2'; data['update_time'] = 2000
        self.archive.ingest(envelope(data))
        second = read_feed(self.root)['conversations'][0]
        self.assertEqual(second['id'], first['id'])
        self.assertEqual(second['messages'][:2], first['messages'])
        self.assertNotEqual(second['revision'], first['revision'])

    def test_hidden_and_alternate_branch_content_is_not_exported(self):
        data = conversation()
        data['mapping']['hidden'] = node('hidden', 'assistant', 'hidden-reasoning', 'a', channel='analysis')
        data['mapping']['alternate'] = node('alternate', 'assistant', 'unused-branch', 'u')
        data['current_node'] = 'hidden'
        self.archive.ingest(envelope(data))
        text = json.dumps(read_feed(self.root))
        self.assertNotIn('hidden-reasoning', text)
        self.assertNotIn('unused-branch', text)

    def test_unbound_missing_or_shared_archive_is_rejected_without_creation(self):
        with self.assertRaisesRegex(ValueError, 'verified account'):
            read_feed(self.root)
        missing = self.root / 'missing'
        with self.assertRaises(FileNotFoundError): read_feed(missing)
        self.assertFalse(missing.exists())
        self.archive.ingest(envelope(conversation()))
        os.chmod(self.root / 'archive.sqlite3', 0o644)
        with self.assertRaisesRegex(ValueError, 'owner-only'): read_feed(self.root)

    def test_identity_or_schema_corruption_is_rejected(self):
        self.archive.ingest(envelope(conversation()))
        self.archive.set_meta('schema_version', '2')
        with self.assertRaisesRegex(ValueError, 'schema'): read_feed(self.root)
        self.archive.set_meta('schema_version', '1')
        self.archive.set_meta('account_key', 'invalid')
        with self.assertRaisesRegex(ValueError, 'verified account'): read_feed(self.root)

    def test_cli_feed_is_json_and_does_not_take_writer_lock(self):
        self.archive.ingest(envelope(conversation()))
        before = fingerprint(self.root)
        cli = Path(__file__).with_name('cli.py')
        result = subprocess.run([sys.executable, str(cli), '--archive', str(self.root), 'feed'], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)['conversations'][0]['id'], ID)
        self.assertEqual(before, fingerprint(self.root))

    def test_citations_are_whitelisted_from_existing_visible_archive(self):
        marker = '\ue200cite\ue202turn123view0\ue201'
        data = conversation()
        message = data['mapping']['a']['message']
        message['content']['parts'] = ['Answer ' + marker]
        message['metadata']['content_references'] = [{
            'type': 'grouped_webpages', 'matched_text': marker,
            'prompt_text': 'do-not-export', 'alt': 'do-not-export',
            'items': [{'url': 'https://example.com/docs', 'attribution': 'Example',
                       'snippet': 'do-not-export', 'refs': ['do-not-export']},
                      {'url': 'javascript:alert(1)'}, {'url': 'file:///private/file'},
                      {'url': 'https://secret@example.com/'},
                      {'url': 'https://example.com/\nsecret'},
                      {'url': 'https://example.com/docs', 'title': 'duplicate'}],
        }]
        self.archive.ingest(envelope(data))
        before = fingerprint(self.root)
        feed = read_feed(self.root)
        self.assertEqual(feed['conversations'][0]['messages'][1]['citation_groups'], [{
            'marker': marker, 'sources': [{'title': 'Example', 'url': 'https://example.com/docs'}],
        }])
        encoded = json.dumps(feed)
        for excluded in ['do-not-export', 'javascript:', 'file://', 'secret@']:
            self.assertNotIn(excluded, encoded)
        self.assertEqual(before, fingerprint(self.root))

    def test_missing_malformed_and_non_web_references_are_ignored(self):
        marker = '\ue200cite\ue202turn1view0\ue201'
        self.assertEqual(citation_groups({'text': marker}), [])
        for ref in [None, 'bad', {'type': 'file', 'matched_text': marker},
                    {'type': 'grouped_webpages', 'matched_text': 'ordinary words'},
                    {'type': 'grouped_webpages', 'matched_text': marker, 'items': None},
                    {'type': 'grouped_webpages', 'matched_text': marker, 'items': [None, {'url': 'https://['}]}]:
            self.assertEqual(citation_groups({'text': marker, 'content_references': [ref]}), [])
        self.assertEqual(citation_groups({'text': 'other branch', 'content_references': [{
            'type': 'grouped_webpages', 'matched_text': marker,
            'items': [{'url': 'https://example.com'}]}]}), [])


if __name__ == '__main__':
    unittest.main()
