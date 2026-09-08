import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from archive import Archive, digest, normalize, scan, review, vault_index

ID='11111111-1111-1111-1111-111111111111'
ID2='22222222-2222-2222-2222-222222222222'
KEY='a'*64

def node(id,role,text,parent=None,**kw):
    return {'id':id,'parent':parent,'children':[], 'message':{'id':id,'author':{'role':role},'content':{'content_type':'text','parts':[text]},'metadata':{},'status':'finished_successfully','create_time':1000,'update_time':1000,**kw}}

def conversation():
    return {'conversation_id':ID,'title':'test','create_time':1000,'update_time':1100,'current_node':'a',
            'mapping':{'root':{'parent':None,'message':None},'u':node('u','user','First question','root'),'a':node('a','assistant','First answer','u')}}

def envelope(data,key=KEY):
    return {'version':1,'accountKey':key,'raw':json.dumps(data),'startedAt':'2026-09-08T22:00:00Z','finishedAt':'2026-09-08T22:00:01Z','path':'/backend-api/conversation/'+ID}

def fingerprint(root):
    return {str(p.relative_to(root)):(hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_mtime_ns) for p in Path(root).rglob('*') if p.is_file()}

class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.archive=Archive(self.root)
    def tearDown(self):
        self.archive.close();self.temp.cleanup()
    def test_live_style_repeat_has_no_file_changes(self):
        self.archive.ingest(envelope(conversation()));self.archive.render();before=fingerprint(self.root)
        result=self.archive.ingest(envelope(conversation()))
        self.assertFalse(result['changed']);self.assertEqual(self.archive.render(),0)
        self.assertEqual(before,fingerprint(self.root))
    def test_continuation_adds_only_new_message_versions(self):
        data=conversation();self.archive.ingest(envelope(data))
        data['mapping']['u2']=node('u2','user','Follow-up','a');data['mapping']['a2']=node('a2','assistant','Second answer','u2')
        data['current_node']='a2';data['update_time']=1200
        result=self.archive.ingest(envelope(data))
        self.assertEqual(result['added_nodes'],2);self.assertEqual(result['added_messages'],2)
        self.assertEqual(self.archive.db.execute('SELECT count(*) FROM revisions').fetchone()[0],2)
        self.assertEqual(self.archive.db.execute('SELECT count(*) FROM node_versions').fetchone()[0],5)
    def test_edit_and_branch_switch_preserve_prior_evidence(self):
        data=conversation();self.archive.ingest(envelope(data))
        data['mapping']['branch']=node('branch','assistant','Alternate answer','u');data['current_node']='branch';data['update_time']=1200
        self.archive.ingest(envelope(data));current=self.archive.current(ID)['data']
        self.assertEqual(current['messages'][-1]['text'],'Alternate answer');self.assertEqual(current['branch_nodes'],1)
        data['mapping']['branch']['message']['content']['parts']=['Edited answer'];data['update_time']=1300
        result=self.archive.ingest(envelope(data));self.assertEqual(result['changed_messages'],1)
        self.assertEqual(self.archive.db.execute('SELECT count(*) FROM revisions').fetchone()[0],3)
        self.assertEqual(self.archive.db.execute('SELECT count(*) FROM node_versions WHERE node_id=?',('branch',)).fetchone()[0],2)
    def test_hidden_reasoning_and_tool_calls_do_not_become_transcript(self):
        data=conversation();data['mapping']['hidden']=node('hidden','assistant','secret reasoning','u',channel='analysis');data['mapping']['a']['parent']='hidden'
        data['mapping']['tool']=node('tool','assistant','tool call','hidden',recipient='python');data['mapping']['a']['parent']='tool'
        data['mapping']['u']['message']['metadata']['attachments']=[{'id':'file-1','name':'input.png'}]
        value,_,_=normalize(data)
        self.assertEqual(len(value['messages']),2);self.assertEqual(value['messages'][0]['attachments'][0]['id'],'file-1')
        self.assertNotIn('secret reasoning',json.dumps(value));self.assertNotIn('tool call',json.dumps(value))
    def test_incomplete_tree_and_active_generation_fail_without_checkpoint(self):
        for mutate in [lambda d:d.update(current_node='missing'),lambda d:d['mapping']['u'].update(parent='a'),lambda d:d['mapping']['a']['message'].update(status='in_progress')]:
            data=conversation();mutate(data)
            with self.assertRaises(ValueError):self.archive.ingest(envelope(data))
        self.assertEqual(self.archive.all(),[])
    def test_account_switch_fails_before_archive_mutation(self):
        self.archive.ingest(envelope(conversation()));before=fingerprint(self.root)
        with self.assertRaisesRegex(ValueError,'account changed'):self.archive.ingest(envelope(conversation(),'b'*64))
        self.assertEqual(before,fingerprint(self.root))
    def test_raw_hash_and_capture_times_are_retained(self):
        event=envelope(conversation());self.archive.ingest(event)
        row=self.archive.db.execute('SELECT raw_json,raw_sha256,started_at,finished_at FROM revisions').fetchone()
        self.assertEqual(row,(event['raw'],digest(event['raw']),event['startedAt'],event['finishedAt']))
    def test_pagination_ignores_estimated_total_and_repeat_is_noop(self):
        data=conversation();other=copy.deepcopy(data);other.update(conversation_id=ID2,update_time=1050)
        calls=[]
        def client(req):
            calls.append(req)
            if req['operation']=='list':
                items=[{'id':ID,'update_time':1100},{'id':ID2,'update_time':1050}][req['offset']:req['offset']+req['limit']]
                return envelope({'items':items,'offset':req['offset'],'total':req['offset']+len(items)+1})
            return envelope(data if req['id']==ID else other)
        result=scan(self.archive,client,'1970-01-01T00:16:40Z',page_size=1)
        self.assertTrue(result['complete_window']);self.assertEqual(result['pages'],3);self.assertEqual(result['fetched'],2)
        before=fingerprint(self.root);result=scan(self.archive,client,None,page_size=1)
        self.assertTrue(result['complete_window']);self.assertEqual(result['fetched'],0);self.assertEqual(before,fingerprint(self.root))
    def test_truncation_and_fetch_failure_never_advance_watermark(self):
        def client(req):
            if req['operation']=='list':return envelope({'items':[{'id':ID,'update_time':1100}],'offset':req['offset'],'total':2})
            return envelope(conversation())
        result=scan(self.archive,client,'1970-01-01T00:16:40Z',max_pages=1,page_size=1)
        self.assertFalse(result['complete_window']);self.assertIsNone(self.archive.meta('watermark'))
        def fail(req):
            if req['operation']=='conversation':raise RuntimeError('retrieval failed')
            return client(req)
        result=scan(self.archive,fail,'1970-01-01T00:16:40Z',refresh_known=True)
        self.assertFalse(result['complete_window']);self.assertIsNone(self.archive.meta('watermark'))
    def test_budget_restart_reuses_successful_conversations(self):
        data=conversation();other=copy.deepcopy(data);other.update(conversation_id=ID2,update_time=1050)
        def client(req):
            if req['operation']=='list':return envelope({'items':[{'id':ID,'update_time':1100},{'id':ID2,'update_time':1050}],'offset':0,'total':2})
            return envelope(data if req['id']==ID else other)
        first=scan(self.archive,client,'1970-01-01T00:16:40Z',max_conversations=1)
        self.assertFalse(first['complete_window']);self.assertEqual(len(first['captured']),1)
        second=scan(self.archive,client,'1970-01-01T00:16:40Z',max_conversations=1)
        self.assertTrue(second['complete_window']);self.assertEqual(second['unchanged'],1);self.assertEqual(second['fetched'],1)
    def test_exact_match_uses_provenance_not_incidental_mentions(self):
        vault=self.root/'vault';actual=vault/'projects/real';incidental=vault/'projects/other'
        for folder in [actual,incidental]:folder.mkdir(parents=True);(folder/'tasks.md').write_text('')
        (actual/'summary.md').write_text('# Real\nBacking conversation ID: `'+ID+'`')
        (incidental/'summary.md').write_text('# Other\nRelated URL https://chatgpt.com/c/'+ID)
        self.archive.ingest(envelope(conversation()));rows=review(self.archive,vault)
        self.assertEqual(rows[0]['status'],'exact_match');self.assertEqual(rows[0]['candidates'][0]['destination'],'projects/real/summary')
        self.assertNotIn(ID,dict(vault_index(vault)[0]).get('projects/other/summary',{}))
    def test_conflicting_exact_owners_require_review(self):
        vault=self.root/'vault'
        for area in ['projects/first','ideas/second']:
            folder=vault/area;folder.mkdir(parents=True);(folder/'tasks.md').write_text('');(folder/'summary.md').write_text('# Owner\nconversation_id: '+ID)
        self.archive.ingest(envelope(conversation()));self.assertEqual(review(self.archive,vault)[0]['status'],'ambiguous')

    def test_redirect_provenance_resolves_to_canonical_container(self):
        vault=self.root/'vault';canonical=vault/'projects/current';legacy=vault/'ideas/old'
        for folder in [canonical,legacy]:folder.mkdir(parents=True)
        (canonical/'tasks.md').write_text('');(canonical/'summary.md').write_text('# Current')
        (legacy/'summary.md').write_text('---\ntype: redirect\ncanonical: "[[projects/current/summary]]"\n---\nconversation_id: '+ID)
        self.archive.ingest(envelope(conversation()));rows=review(self.archive,vault)
        self.assertEqual(rows[0]['candidates'][0]['destination'],'projects/current/summary')
    def test_share_id_is_not_mistaken_for_backing_conversation_id(self):
        vault=self.root/'vault';folder=vault/'ideas/other';folder.mkdir(parents=True)
        (folder/'tasks.md').write_text('');(folder/'summary.md').write_text('# Other\nshared_conversation_id: '+ID)
        self.assertNotIn(ID,vault_index(vault)[0])
    def test_explicit_backfill_cannot_regress_watermark(self):
        self.archive.set_meta('watermark','1200.0')
        def client(req):return envelope({'items':[],'offset':0,'total':0})
        result=scan(self.archive,client,'1970-01-01T00:16:40Z')
        self.assertTrue(result['complete_window']);self.assertEqual(self.archive.meta('watermark'),'1200.0')
    def test_reordered_pages_do_not_advance_checkpoint(self):
        def client(req):
            if req['operation']=='list':return envelope({'items':[{'id':ID,'update_time':1100},{'id':ID2,'update_time':1200}],'offset':0})
            return envelope(conversation())
        with self.assertRaisesRegex(ValueError,'not ordered'):scan(self.archive,client,'1970-01-01T00:16:40Z')
        self.assertIsNone(self.archive.meta('watermark'))

    def test_missing_and_duplicate_message_identity_fail_closed(self):
        for value in [None, 'u']:
            data=conversation();data['mapping']['a']['message']['id']=value
            with self.assertRaisesRegex(ValueError,'message ID'):self.archive.ingest(envelope(data))

if __name__=='__main__':unittest.main()
