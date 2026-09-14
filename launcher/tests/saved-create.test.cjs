const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {SavedCreator}=require('../electron/saved-create.cjs');
const cid='11111111-1111-1111-1111-111111111111',uid='22222222-2222-2222-2222-222222222222';
const request={version:1,accountKey:'a'.repeat(64),transactionId:'b'.repeat(64),text:'Synthetic new conversation'};
function fixture(t) {
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'saved-create-'));t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
 const data={conversation_id:cid,create_time:Date.now()/1000,current_node:uid,mapping:{[uid]:{parent:null,message:{id:uid,author:{role:'user'},content:{parts:[request.text]}}}}};
 let clicked=0,items=[],lost=false,accepted=true,account=request.accountKey;
 const opts={directory,attempts:2,delay:async()=>{},list:async()=>({accountKey:account,raw:JSON.stringify({items})}),history:async()=>({accountKey:account,raw:JSON.stringify(data)}),
 open:async()=>({prepare:async()=>{},submit:async()=>{clicked++;if(accepted)items=[{id:cid}];if(lost)throw Error('lost response');},conversationId:async()=>null,close:async()=>{}})};
 return {opts,data,clicks:()=>clicked,lost:()=>lost=true,noAccept:()=>accepted=false,changeAccount:()=>account='c'.repeat(64)};
}
test('new saved conversation reconciles a lost response and restart without another click',async t=>{
 const f=fixture(t);f.lost();const r=await new SavedCreator(f.opts).create(request);assert.equal(r.conversationId,cid);assert.equal(r.userMessageId,uid);
 const replay=await new SavedCreator(f.opts).create(request);assert.equal(replay.replayed,true);assert.equal(f.clicks(),1);
 await assert.rejects(new SavedCreator(f.opts).create({...request,text:'different'}),/transaction_conflict/);
});
test('unknown creation remains uncertain and cannot click twice',async t=>{
 const f=fixture(t);f.noAccept();for(let i=0;i<2;i++)await assert.rejects(new SavedCreator(f.opts).create(request),/uncertain/);assert.equal(f.clicks(),1);
});
test('creation rejects wrong account before submission and temporary source after submission',async t=>{
 const f=fixture(t);f.changeAccount();await assert.rejects(new SavedCreator(f.opts).create(request),/account_mismatch/);assert.equal(f.clicks(),0);
 const g=fixture(t);g.data.is_temporary=true;await assert.rejects(new SavedCreator(g.opts).create(request),/uncertain/);assert.equal(g.clicks(),1);
});
test('creation journal contains hashes and identity, never the prompt',async t=>{
 const f=fixture(t);await new SavedCreator(f.opts).create(request);
 const file=path.join(f.opts.directory,'new-'+request.accountKey,request.transactionId+'.json');assert.equal(fs.statSync(file).mode&0o777,0o600);assert(!fs.readFileSync(file,'utf8').includes(request.text));
});

test('creation rejects old or malformed creation times even with matching text',async t=>{
 for(const value of ['2020-01-01T00:00:00Z','not-a-date',null]){
  const f=fixture(t);f.data.create_time=value;await assert.rejects(new SavedCreator(f.opts).create(request),/uncertain/);assert.equal(f.clicks(),1);
 }
 const f=fixture(t);f.data.create_time=new Date().toISOString();assert.equal((await new SavedCreator(f.opts).create(request)).conversationId,cid);
});
