const test = require('node:test');
const assert = require('node:assert/strict');
const {visibleAttachments, safeDownloadURL} = require('../electron/saved-media.cjs');
test('attachments must belong to a visible message on the active branch', () => {
  const visible = {id:'visible',author:{role:'user'},metadata:{attachments:[{id:'file_123456789'}]}};
  const data = {current_node:'a',mapping:{a:{parent:null,message:visible},hidden:{parent:null,message:{...visible,id:'hidden'}}}};
  assert.equal(visibleAttachments(data,'visible').length,1);
  assert.deepEqual(visibleAttachments(data,'hidden'),[]);
  visible.channel='analysis'; assert.deepEqual(visibleAttachments(data,'visible'),[]);
  delete visible.channel; visible.metadata.is_visually_hidden_from_conversation=true;
  assert.deepEqual(visibleAttachments(data,'visible'),[]);
});
test('signed file downloads cannot forward credentials or use arbitrary hosts', () => {
  assert.equal(safeDownloadURL('https://files.oaiusercontent.com/file?sig=example'),true);
  for(const url of ['http://files.oaiusercontent.com/a','https://example.com/a','https://oaiusercontent.com.evil.invalid/a','https://user:pass@files.oaiusercontent.com/a','https://files.oaiusercontent.com:8443/a']) assert.equal(safeDownloadURL(url),false,url);
});

test('authenticated source lookup downloads verified bytes without forwarding auth to storage', async t => {
  const {downloadSavedMedia}=require('../electron/saved-media.cjs');const {createHash}=require('node:crypto');
  const old=process.env.CODEX_WEB_GPT_HISTORY_ENABLED;process.env.CODEX_WEB_GPT_HISTORY_ENABLED='1';t.after(()=>{if(old===undefined)delete process.env.CODEX_WEB_GPT_HISTORY_ENABLED;else process.env.CODEX_WEB_GPT_HISTORY_ENABLED=old;});
  const cid='11111111-1111-1111-1111-111111111111',file='file_123456789',bytes=Buffer.from('synthetic file'),requests=[];
  const accountKey=createHash('sha256').update('chatgpt-history-v1:test-user').digest('hex');
  const session={fetch:async(url,options)=>{
    requests.push({url,options});let body;
    if(url.endsWith('/api/auth/session'))body={user:{id:'test-user'},accessToken:'synthetic-token'};
    else if(url.includes('/backend-api/conversation/'))body={conversation_id:cid,current_node:'user',mapping:{user:{parent:null,message:{id:'user',author:{role:'user'},metadata:{attachments:[{id:file,name:'test.txt',mime_type:'text/plain',size:bytes.length}]}}}}};
    else if(url.includes('/backend-api/files/download/'))body={download_url:'https://files.oaiusercontent.com/test?signature=private'};
    else {assert.equal(url,'https://files.oaiusercontent.com/test?signature=private');assert.equal(options.headers,undefined);assert.equal(options.credentials,'omit');assert.equal(options.redirect,'error');return new Response(bytes,{headers:{'content-type':'text/plain'}});}
    return new Response(JSON.stringify(body),{headers:{'content-type':'application/json'}});
  }};
  const result=await downloadSavedMedia({profile:'development',browserInteractionMode:()=> 'automatic',view:{webContents:{session}}},{accountKey,conversationId:cid,messageId:'user',attachmentId:file});
  assert.equal(Buffer.from(result.data,'base64').toString(),bytes.toString());assert.equal(result.size,bytes.length);assert(!JSON.stringify(result).includes('signature'));
  assert(requests.some(r=>r.url===`https://chatgpt.com/backend-api/files/download/${file}?conversation_id=${cid}`));
});

test('generated downloads must be visible links and cannot traverse the sandbox',()=>{
 const m={id:'answer',author:{role:'assistant'},content:{parts:['[Download](sandbox:/mnt/data/report.pdf) [bad](sandbox:/mnt/data/../private)']}};
 const items=visibleAttachments({current_node:'answer',mapping:{answer:{parent:null,message:m}}},'answer');
 assert.deepEqual(items.map(a=>a.id),['sandbox:/mnt/data/report.pdf']);
});
