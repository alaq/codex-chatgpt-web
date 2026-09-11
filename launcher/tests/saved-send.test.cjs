const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { SavedSender, validateRequest, sendSavedConversation } = require("../electron/saved-send.cjs");

const id = "11111111-1111-1111-1111-111111111111", userId = "22222222-2222-2222-2222-222222222222";
const request = { version: 1, conversationId: id, accountKey: "a".repeat(64), transactionId: "b".repeat(64), text: "Synthetic bridge test" };
function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "saved-send-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const data = { conversation_id: id, current_node: "head", mapping: { head: { message: { author: { role: "assistant" }, status: "finished_successfully" } } } };
  let clicks = 0, prepare = () => {}, submit = () => {
    data.mapping[userId] = { parent: "head", message: { id: userId, author: { role: "user" }, content: { content_type: "text", parts: [request.text] } } };
    data.current_node = userId;
  };
  const opts = { directory, readHistory: async () => ({ accountKey: request.accountKey, raw: JSON.stringify(data) }),
    openConversation: async () => ({ prepare: async () => prepare(), submit: async () => { clicks++; submit(); }, close: async () => {} }), wait: async () => {}, attempts: 2 };
  return { data, opts, sender: new SavedSender(opts), clicks: () => clicks, prepare: fn => prepare = fn, submit: fn => submit = fn };
}
test("saved send rejects malformed/oversized input and disabled launcher", async () => {
  for (const change of [{ version: 2 }, { conversationId: "../x" }, { accountKey: "bad" }, { transactionId: "bad" }, { text: " " }, { text: "é".repeat(6001) }, { url: "https://elsewhere.invalid" }]) assert.throws(() => validateRequest({ ...request, ...change }));
  await assert.rejects(sendSavedConversation({profile:"production"}, request), /saved_send_disabled/);
});
test("source acknowledgement survives restart and duplicate request without another click", async t => {
  const f = fixture(t);
  const first = await f.sender.send(request);
  assert.equal(first.userMessageId, userId);
  assert.equal(first.replayed, false);
  const retry = await new SavedSender(f.opts).send(request);
  assert.equal(retry.replayed, true); assert.equal(f.clicks(), 1);
  await assert.rejects(f.sender.send({ ...request, text: "changed" }), /transaction_conflict/);
});
test("lost click response reconciles the saved user node", async t => {
  const f = fixture(t);
  f.submit(() => {
    f.data.mapping[userId] = { parent: "head", message: { id: userId, author: { role: "user" }, content: { content_type: "text", parts: [request.text] } } };
    throw new Error("lost browser transport response");
  });
  assert.equal((await f.sender.send(request)).status, "accepted");
  assert.equal(f.clicks(), 1);
});
test("unobserved attempt blocks retries and later transactions even after restart", async t => {
  const f = fixture(t); f.submit(() => { throw new Error("uncertain"); });
  await assert.rejects(f.sender.send(request), /saved_send_uncertain/);
  const next = new SavedSender(f.opts);
  await assert.rejects(next.send(request), /saved_send_uncertain/);
  await assert.rejects(next.send({ ...request, transactionId: "c".repeat(64) }), /saved_send_uncertain/);
  assert.equal(f.clicks(), 1);
});
test("account mismatch, active generation, and changed head prevent a click", async t => {
  const f = fixture(t);
  await assert.rejects(f.sender.send({ ...request, accountKey: "c".repeat(64) }), /account_mismatch/);
  f.data.mapping.head.message.status = "in_progress";
  await assert.rejects(f.sender.send(request), /source_busy/);
  f.data.mapping.head.message.status = "finished_successfully";
  f.prepare(() => { f.data.mapping.other = f.data.mapping.head; f.data.current_node = "other"; });
  await assert.rejects(f.sender.send(request), /source_changed/);
  assert.equal(f.clicks(), 0);
});
test("same text under a different parent or multiple candidates never resolves an uncertain send", async t => {
  const f = fixture(t);
  f.submit(() => {
    f.data.mapping[userId] = { parent: "different", message: { id: userId, author: { role: "user" }, content: { content_type: "text", parts: [request.text] } } };
  });
  await assert.rejects(f.sender.send(request), /uncertain/);
  f.data.mapping[userId].parent = "head";
  f.data.mapping.other = f.data.mapping[userId];
  await assert.rejects(f.sender.send(request), /uncertain/);
  delete f.data.mapping.other;
  assert.equal((await f.sender.send(request)).userMessageId, userId);
  assert.equal(f.clicks(), 1);
});
test("private state rejects symlinks and persists no prompt text", async t => {
  const f = fixture(t);
  await f.sender.send(request);
  const dir = path.join(f.opts.directory, fs.readdirSync(f.opts.directory)[0]);
  const file = path.join(dir, request.transactionId + ".json");
  assert.equal(fs.statSync(file).mode & 0o777, 0o600);
  assert(!fs.readFileSync(file, "utf8").includes(request.text));
  fs.renameSync(file, file + ".original"); fs.symlinkSync(file + ".original", file);
  await assert.rejects(f.sender.send(request), /unsafe_record/);
});

test("composer readback preserves multiline drafts without layout blank lines", () => {
  const { readComposerText } = require('../electron/saved-send.cjs');
  const text = value => ({nodeType:3,textContent:value});
  const el = (tagName,...childNodes) => ({nodeType:1,tagName,childNodes});
  const editor = el('DIV', el('P',text('First line')),el('P',text('Second line')));
  editor.innerText = 'First line\n\nSecond line';
  assert.equal(readComposerText(editor),'First line\nSecond line');
  assert.equal(readComposerText(el('DIV',el('P',text('First')),el('P',el('BR')),el('P',text('Third')))), 'First\n\nThird');
  assert.equal(readComposerText(el('DIV',el('P',text('A'),el('BR'),text('B')))), 'A\nB');
  assert.equal(readComposerText(el('DIV',el('P',text('**literal**  '),el('SPAN',text('code'))))), '**literal**  code');
  assert.equal(readComposerText({tagName:'TEXTAREA',value:'A\n\nB'}),'A\n\nB');
});

test("multiline saved request is acknowledged exactly and replayed without duplication", async t => {
  const f=fixture(t);
  const multiline={...request,text:'First line\nSecond line\n\n**literal markdown**'};
  f.submit(() => {
    f.data.mapping[userId]={parent:'head',message:{id:userId,author:{role:'user'},content:{content_type:'text',parts:[multiline.text]}}};
    f.data.current_node=userId;
  });
  assert.equal((await f.sender.send(multiline)).userMessageId,userId);
  assert.equal((await new SavedSender(f.opts).send(multiline)).replayed,true);
  assert.equal(f.clicks(),1);
});
