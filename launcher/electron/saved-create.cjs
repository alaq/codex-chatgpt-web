const path = require('node:path');
const {createHash} = require('node:crypto');
const {readSavedHistory} = require('./saved-history.cjs');
const {openSavedConversation, privateDirectory, readRecord, writeRecord} = require('./saved-send.cjs');
const HASH = /^[a-f0-9]{64}$/;
const UUID = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/;
const hash = data => createHash('sha256').update(data).digest('hex');
const wait = ms => new Promise(resolve=>setTimeout(resolve,ms));
function fail(code) {const e = new Error(code); e.code=code; return e;}

class SavedCreator {
  constructor({directory,list,history,open,delay=wait,attempts=20}) {Object.assign(this,{directory,list,history,open,delay,attempts});this.busy=false;}
  async create(request) {
    if (!request || request.version!==1 || !HASH.test(request.accountKey||'') || !HASH.test(request.transactionId||'')
      || typeof request.text!=='string' || !request.text.trim() || Buffer.byteLength(request.text)>12000 || request.text.includes('\0')
      || Object.keys(request).some(k=>!['version','accountKey','transactionId','text'].includes(k))) throw fail('saved_create_invalid_request');
    if(this.busy) throw fail('saved_create_busy');this.busy=true;
    let ui,accepted=false;
    try {
      if(!path.isAbsolute(this.directory)) throw fail('saved_create_unsafe_directory');
      privateDirectory(this.directory);
      const dir=path.join(this.directory,'new-'+request.accountKey);privateDirectory(dir);
      const file=path.join(dir,request.transactionId+'.json');let record=readRecord(file);
      const identity={version:1,accountKey:request.accountKey,transactionId:request.transactionId,textHash:hash(request.text)};
      if(record && Object.keys(identity).some(k=>record[k]!==identity[k])) throw fail('saved_create_transaction_conflict');
      const readList=async()=>{
        const response=await this.list();
        if(response.accountKey!==request.accountKey) throw fail('saved_create_account_mismatch');
        const data=JSON.parse(response.raw);if(!Array.isArray(data.items)) throw fail('saved_create_invalid_history');return data.items;
      };
      const settle=async()=>{
        if(record.status==='accepted') {if(!UUID.test(record.conversationId||'')||!UUID.test(record.userMessageId||''))throw fail('saved_create_invalid_record');return record;}
        if(record.status!=='submitting'||!Array.isArray(record.baseline))throw fail('saved_create_invalid_record');
        const candidates=record.conversationId?[{id:record.conversationId}]:(await readList()).filter(c=>!record.baseline.includes(c.id));
        const matches=[];
        for(const candidate of candidates.slice(0,50)) {
          if(!UUID.test(candidate.id||''))continue;
          const envelope=await this.history(candidate.id);
          if(envelope.accountKey!==request.accountKey)throw fail('saved_create_account_mismatch');
          const data=JSON.parse(envelope.raw);
          const created=typeof data.create_time==='number'?data.create_time:Date.parse(data.create_time)/1000;
          if((data.conversation_id||data.id)!==candidate.id||data.is_temporary||!data.mapping||!data.current_node||!Number.isFinite(created)||created<Date.parse(record.createdAt)/1000-5)continue;
          const users=Object.values(data.mapping).filter(n=>n.message?.author?.role==='user');
          const first=users.filter(n=>Array.isArray(n.message.content?.parts)&&n.message.content.parts.every(p=>typeof p==='string')&&hash(n.message.content.parts.join('\n'))===record.textHash);
          if(users.length===1&&first.length===1&&UUID.test(first[0].message.id||''))matches.push({conversationId:candidate.id,userMessageId:first[0].message.id});
        }
        if(matches.length!==1)throw fail('saved_create_uncertain');
        record={...record,...matches[0],status:'accepted'};writeRecord(file,record);return record;
      };
      if(record){await readList();const done=await settle();return{version:1,status:'accepted',conversationId:done.conversationId,userMessageId:done.userMessageId,replayed:true};}
      const baseline=(await readList()).map(c=>c.id);
      ui=await this.open(request.text);await ui.prepare(request.text);await readList();
      record={...identity,status:'submitting',baseline,createdAt:new Date().toISOString()};writeRecord(file,record);
      try{await ui.submit(request.text);}catch{/* reconcile without another click */}
      for(let i=0;i<this.attempts;i++){
        try{
          const cid=await ui.conversationId();if(UUID.test(cid||'')&&!baseline.includes(cid)){record={...record,conversationId:cid};writeRecord(file,record);}
          const done=await settle();accepted=true;return{version:1,status:'accepted',conversationId:done.conversationId,userMessageId:done.userMessageId,replayed:false};
        }catch(e){if(e.code==='saved_create_account_mismatch')throw e;if(i+1<this.attempts)await this.delay(1000);}
      }
      throw fail('saved_create_uncertain');
    }finally{
      const close=async()=>{try{await ui?.close();}finally{this.busy=false;}};
      if(accepted)void close().catch(()=>{});else await close();
    }
  }
}
let creator;
async function createSavedConversation(host,request){
  if(host.profile!=='development'||process.env.CODEX_WEB_GPT_SAVED_SEND_ENABLED!=='1'||!process.env.CODEX_WEB_GPT_SAVED_SEND_DIR||host.browserInteractionMode()!=='automatic')throw fail('saved_create_disabled');
  creator??=new SavedCreator({directory:process.env.CODEX_WEB_GPT_SAVED_SEND_DIR,list:()=>readSavedHistory(host,{operation:'list',offset:0,limit:50}),history:id=>readSavedHistory(host,{operation:'conversation',id}),open:text=>openSavedConversation(host,'new',text)});
  try{return await host.withManualOperation('create saved conversation',()=>creator.create(request));}
  catch(e){throw fail(/^saved_create_[a-z_]+$/.test(e?.code||'')?e.code:'saved_create_failed');}
}
module.exports={SavedCreator,createSavedConversation};
