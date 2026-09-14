// Separate from the task adapter: this continues an existing saved conversation.
// It never calls the Temporary Chat sender or forwards session credentials.
const fs = require("node:fs");
const path = require("node:path");
const { createHash, randomUUID } = require("node:crypto");
const { readSavedHistory } = require("./saved-history.cjs");

const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const HASH = /^[a-f0-9]{64}$/;
const hash = text => createHash("sha256").update(text).digest("hex");
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
function fail(code) { const e = new Error(code); e.code = code; return e; }

function attachmentManifest(attachments) {
  if (attachments == null) return [];
  if (!Array.isArray(attachments) || attachments.length > 1) throw fail('saved_send_invalid_attachment');
  return attachments.map(a => {
    if (!a || typeof a.name !== 'string' || !a.name || a.name.length > 240 || /[\\/\x00-\x1f\x7f]/.test(a.name)
      || typeof a.mimeType !== 'string' || !/^[\w.+-]+\/[\w.+-]+$/.test(a.mimeType)
      || typeof a.data !== 'string' || a.data.length > 28 * 1024 * 1024
      || !HASH.test(a.sha256 || '') || Object.keys(a).some(k => !['name','mimeType','data','sha256'].includes(k))) throw fail('saved_send_invalid_attachment');
    const bytes = Buffer.from(a.data, 'base64');
    if (!bytes.length || bytes.length > 20 * 1024 * 1024 || bytes.toString('base64') !== a.data || hash(bytes) !== a.sha256) throw fail('saved_send_invalid_attachment');
    return {name: a.name, mimeType: a.mimeType, size: bytes.length, sha256: a.sha256};
  });
}

function validateRequest(r) {
  if (!r || typeof r !== "object" || Array.isArray(r) || r.version !== 1
    || !UUID.test(r.conversationId || "") || !HASH.test(r.accountKey || "") || !HASH.test(r.transactionId || "")
    || typeof r.text !== "string" || (!r.text.trim() && !r.attachments?.length) || Buffer.byteLength(r.text) > 12000 || r.text.includes("\0")
    || Object.keys(r).some(k => !["version", "conversationId", "accountKey", "transactionId", "text", "attachments"].includes(k))) {
    throw fail("saved_send_invalid_request");
  }
  const attachments = attachmentManifest(r.attachments);
  return { version: 1, conversationId: r.conversationId, accountKey: r.accountKey, transactionId: r.transactionId, textHash: hash(r.text),
    ...(attachments.length ? {attachments, attachmentHash: hash(JSON.stringify(attachments))} : {}) };
}

function privateDirectory(dir) {
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const st = fs.lstatSync(dir);
  if (!st.isDirectory() || st.isSymbolicLink() || (st.mode & 0o077) || st.uid !== process.getuid()) throw fail("saved_send_unsafe_state_directory");
}
function readRecord(file) {
  let st;
  try { st = fs.lstatSync(file); } catch (e) { if (e.code === "ENOENT") return null; throw e; }
  if (!st.isFile() || st.isSymbolicLink() || st.nlink !== 1 || (st.mode & 0o077) || st.uid !== process.getuid() || st.size > 4096) throw fail("saved_send_unsafe_record");
  const fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  try { return JSON.parse(fs.readFileSync(fd, "utf8")); } finally { fs.closeSync(fd); }
}
function writeRecord(file, record) {
  const tmp = `${file}.${randomUUID()}.tmp`;
  const fd = fs.openSync(tmp, "wx", 0o600);
  try { fs.writeFileSync(fd, JSON.stringify(record)); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
  fs.renameSync(tmp, file);
  // The submitting record must survive a crash before the UI click is attempted.
  const dirFD = fs.openSync(path.dirname(file), "r");
  try { fs.fsyncSync(dirFD); } finally { fs.closeSync(dirFD); }
}

function checkedHistory(envelope, request) {
  if (envelope.accountKey !== request.accountKey) throw fail("saved_send_account_mismatch");
  const data = JSON.parse(envelope.raw);
  if ((data.conversation_id || data.id) !== request.conversationId || !data.mapping || !data.current_node || !data.mapping[data.current_node]) throw fail("saved_send_source_identity_changed");
  return data;
}
function acknowledgedMessage(data, record) {
  // The browser generates source IDs. Reconcile only one exact user child of the
  // saved pre-send head, including when a later branch is active. Never click again
  // merely because a response or process was lost.
  const matches = Object.values(data.mapping).filter(n => n?.parent === record.parentId
    && n.message?.author?.role === "user" && ['text', 'multimodal_text'].includes(n.message.content?.content_type)
    && Array.isArray(n.message.content.parts)
    && hash(n.message.content.parts.filter(p => typeof p === 'string').join("\n")) === record.textHash
    && (record.attachments || []).length === (n.message.metadata?.attachments || []).length
    && (record.attachments || []).every(a => (n.message.metadata?.attachments || []).some(b => b.name === a.name && b.size === a.size && b.mime_type === a.mimeType)));
  if (matches.length !== 1 || !UUID.test(matches[0].message.id || "")) return null;
  return matches[0].message.id;
}
function checkIdle(data) {
  const m = data.mapping[data.current_node]?.message;
  if (m && (m.status === "in_progress" || m.status === "pending" || m.metadata?.is_complete === false || m.author?.role === "user")) throw fail("saved_send_source_busy");
}

// Layout-derived innerText inserts extra blank lines between editor paragraphs.
// Read actual paragraph/line-break structure instead, preserving intentional blank
// lines and inline text. This function is also evaluated inside the saved page.
function readComposerText(editor) {
  if (!editor) return "";
  if (editor.tagName === "TEXTAREA" || editor.tagName === "INPUT") return editor.value;
  const text = node => {
    if (node.nodeType === 3) return node.textContent || "";
    if (node.tagName === "BR") return "\n";
    const children = Array.from(node.childNodes || []);
    if (children.length === 1 && children[0].tagName === "BR") return "";
    return children.map(text).join("");
  };
  const children = Array.from(editor.childNodes || []);
  return children.map((child, index) => {
    const boundary = index > 0 && (['P', 'DIV'].includes(child.tagName) || ['P', 'DIV'].includes(children[index - 1].tagName));
    return (boundary ? "\n" : "") + text(child);
  }).join("");
}

// Chromium may turn ordinary spaces into NBSPs when editing collapsible HTML.
// Permit repairing only that directional substitution in an existing draft.
// Submission and source acknowledgement still require the requested text itself.
function repairableComposerDraft(actual, expected) {
  const wanted = [...expected];
  return actual.length === expected.length && [...actual].every((char, i) =>
    char === wanted[i] || char === "\u00a0" && wanted[i] === " ");
}

// Evaluated in Chromium, including by the renderer regression test. Preserve
// whitespace during native editing rather than normalizing the submitted text.
function insertComposerText(editor, text) {
  editor.focus();
  if (document.activeElement !== editor) return false;
  // ChatGPT's empty placeholder paragraph has its own white-space: nowrap.
  // Changing only the outer editor leaves native insertion inside that paragraph
  // collapsible. Include descendants that can contain the selection/caret.
  const styles = [editor, ...editor.querySelectorAll('*')].map(element => ({
    element, value: element.style.getPropertyValue('white-space'),
    priority: element.style.getPropertyPriority('white-space'),
  }));
  try {
    for (const {element} of styles) element.style.setProperty('white-space', 'pre-wrap', 'important');
    if (editor.tagName === 'TEXTAREA' || editor.tagName === 'INPUT') {
      editor.setSelectionRange(0, editor.value.length);
    } else {
      const selection = window.getSelection(), range = document.createRange();
      range.selectNodeContents(editor);
      selection.removeAllRanges(); selection.addRange(range);
    }
    return document.execCommand('insertText', false, text);
  } finally {
    for (const {element, value, priority} of styles) {
      if (value) element.style.setProperty('white-space', value, priority);
      else element.style.removeProperty('white-space');
    }
  }
}

class SavedSender {
  constructor({ directory, readHistory, openConversation, wait = delay, attempts = 20, backgroundClose = false, onState = () => {} }) {
    Object.assign(this, { directory, readHistory, openConversation, wait, attempts, backgroundClose, onState });
    this.busy = false;
  }
  async send(request) {
    const identity = validateRequest(request);
    if (!path.isAbsolute(this.directory)) throw fail("saved_send_unsafe_state_directory");
    if (this.busy) throw fail("saved_send_busy");
    this.busy = true;
    let ui, accepted = false;
    this.onState(request, 'preparing');
    try {
      privateDirectory(this.directory);
      const dir = path.join(this.directory, hash(`${request.accountKey}:${request.conversationId}`));
      privateDirectory(dir);
      const file = path.join(dir, `${request.transactionId}.json`), activeFile = path.join(dir, "active.json");
      let data = checkedHistory(await this.readHistory(request.conversationId), request);
      const previous = readRecord(file);
      if (previous && Object.keys(identity).some(k => JSON.stringify(previous[k]) !== JSON.stringify(identity[k]))) throw fail("saved_send_transaction_conflict");
      const settle = record => {
        if (!record || record.version !== 1 || record.accountKey !== request.accountKey || record.conversationId !== request.conversationId || !HASH.test(record.transactionId || "") || !HASH.test(record.textHash || "") || !["submitting", "accepted"].includes(record.status)) throw fail("saved_send_invalid_record");
        if (record.status === "accepted") {
          if (!UUID.test(record.userMessageId || "")) throw fail("saved_send_invalid_record");
          return record;
        }
        const receipt = readRecord(path.join(dir, `${record.transactionId}.json`));
        if (receipt?.status === "accepted") {
          if (["version", "accountKey", "conversationId", "transactionId", "textHash", "parentId", "attachmentHash"].some(k => receipt[k] !== record[k]) || !UUID.test(receipt.userMessageId || "")) throw fail("saved_send_invalid_record");
          writeRecord(activeFile, receipt);
          return receipt;
        }
        const id = acknowledgedMessage(data, record);
        if (!id) throw fail("saved_send_uncertain");
        const sourceMessage = Object.values(data.mapping).find(n => n.message?.id === id)?.message;
        record = { ...record, status: "accepted", userMessageId: id, attachmentIDs: (sourceMessage?.metadata?.attachments || []).map(a => a.id) };
        writeRecord(path.join(dir, `${record.transactionId}.json`), record);
        writeRecord(activeFile, record);
        return record;
      };
      if (previous) {
        const done = settle(previous);
        accepted = true;
        return { version: 1, status: "accepted", userMessageId: done.userMessageId, attachmentIDs: done.attachmentIDs || [], replayed: true };
      }
      const active = readRecord(activeFile);
      if (active) settle(active); // An unresolved prior attempt blocks new sends to this chat.
      checkIdle(data);
      const parentId = data.current_node;
      ui = await this.openConversation(request.conversationId, request.text, request.attachments || []);
      await ui.prepare(request.text);
      data = checkedHistory(await this.readHistory(request.conversationId), request);
      checkIdle(data);
      if (data.current_node !== parentId) throw fail("saved_send_source_changed");
      const record = { ...identity, parentId, status: "submitting", createdAt: new Date().toISOString() };
      // Write active first: even a crash between these two writes prevents a new
      // transaction from racing a possibly submitted one.
      writeRecord(activeFile, record);
      writeRecord(file, record);
      try { await ui.submit(request.text); } catch { /* reconcile; no second click */ }
      for (let attempt = 0; attempt < this.attempts; attempt++) {
        try {
          data = checkedHistory(await this.readHistory(request.conversationId), request);
          const done = settle(record);
          accepted = true;
          return { version: 1, status: "accepted", userMessageId: done.userMessageId, attachmentIDs: done.attachmentIDs || [], replayed: false };
        } catch (error) {
          if (error.code === "saved_send_account_mismatch") throw fail("saved_send_uncertain");
          if (attempt + 1 < this.attempts) await this.wait(1000);
        }
      }
      throw fail("saved_send_uncertain");
    } finally {
      const close = async () => {
        try { await ui?.close(running => this.onState(request, running ? 'generating' : 'complete')); }
        finally { this.busy = false; if (!accepted) this.onState(request, 'needs_recovery'); }
      };
      if (accepted) this.onState(request, 'accepted');
      // The original receipt is durable. Return it now; keep the generation page
      // alive independently, and retain the sender lock until that page settles.
      if (accepted && ui && this.backgroundClose) void close().catch(() => this.onState(request, 'needs_recovery'));
      else await close();
    }
  }
}

async function openSavedConversation(host, id, expectedText, attachments = []) {
  const { BrowserWindow } = require("electron");
  const creating = id === 'new';
  const url = creating ? 'https://chatgpt.com/' : `https://chatgpt.com/c/${id}`;
  const win = new BrowserWindow({ show: false, width: 1000, height: 900, webPreferences: {
    session: host.view.webContents.session, nodeIntegration: false, contextIsolation: true, sandbox: true, backgroundThrottling: false,
  } });
  const contents = win.webContents;
  contents.setWindowOpenHandler(() => ({ action: "deny" }));
  contents.on("will-navigate", (event, target) => { if (target !== url && !(creating && /^https:\/\/chatgpt\.com\/c\/[a-f0-9-]{36}$/.test(target))) event.preventDefault(); });
  const inspect = `(() => {
    const editor = document.querySelector('#prompt-textarea');
    const stop = document.querySelector('[data-testid="stop-button"]');
    return {ready: !!editor && (editor.isContentEditable || editor.tagName === 'TEXTAREA') && !stop,
      temporary: !!document.querySelector('[data-testid="temporary-chat-indicator"], [data-testid="temporary-chat-banner"]'),
      text: (${readComposerText.toString()})(editor)};
  })()`;
  try {
    await contents.loadURL(url);
    let ready = false;
    for (let i = 0; i < 100; i++) {
      if (contents.getURL() !== url) throw fail("saved_send_wrong_page");
      const state = await contents.executeJavaScript(inspect);
      if (creating && state.temporary) throw fail('saved_create_temporary_mode');
      if (state.ready) {
        // A rejected pre-submit attempt can leave its exact draft in ChatGPT.
        // Reuse only that same requested text; never erase an unrelated draft.
        if (state.text.trim() && !repairableComposerDraft(state.text.trim(), expectedText.trim())) throw fail("saved_send_existing_draft");
        ready = true; break;
      }
      await delay(300);
    }
    if (!ready) throw fail("saved_send_composer_unavailable");
  } catch (error) {
    // Log structural diagnostics only: never draft text, cookies, or page HTML.
    let diagnostic = {};
    try {
      diagnostic = await contents.executeJavaScript(`(() => {
        const body = (document.body?.innerText || '').toLowerCase();
        return {urlMatches: location.href === ${JSON.stringify(url)}, readyState: document.readyState,
          composerCount: document.querySelectorAll('#prompt-textarea, [data-testid="prompt-textarea"]').length,
          editableCount: document.querySelectorAll('[contenteditable="true"],textarea').length,
          challenge: !!document.querySelector('#challenge-running, #challenge-stage') || body.includes('verify you are human'),
          loginRequired: !!document.querySelector('[data-testid="login-button"]'),
          rateLimited: body.includes('too many requests') || body.includes('rate limit')};
      })()`);
    } catch { /* renderer may be gone */ }
    host.logger?.warn('saved_send.composer_unavailable', diagnostic);
    win.destroy();
    throw fail(error.code === "saved_send_existing_draft" ? error.code : diagnostic.challenge ? 'saved_send_challenge' : diagnostic.loginRequired ? 'saved_send_login_required' : "saved_send_composer_unavailable");
  }
  return {
    async conversationId() { return contents.getURL().match(/^https:\/\/chatgpt\.com\/c\/([a-f0-9-]{36})$/)?.[1] || null; },
    async prepare(text) {
      if (contents.getURL() !== url) throw fail("saved_send_wrong_page");
      if (attachments.length) {
        const staged = await contents.executeJavaScript(`(() => {
          const input = document.querySelector('input[data-testid="upload-photos-input"]');
          if (!input || document.querySelector('form [role="group"][aria-label]')) return false;
          const transfer = new DataTransfer();
          for (const a of ${JSON.stringify(attachments)}) {
            const raw = atob(a.data), bytes = new Uint8Array(raw.length);
            for (let i=0;i<raw.length;i++) bytes[i] = raw.charCodeAt(i);
            transfer.items.add(new File([bytes], a.name, {type:a.mimeType}));
          }
          input.files = transfer.files; input.dispatchEvent(new Event('change', {bubbles:true})); return true;
        })()`);
        if (!staged) throw fail('saved_send_attachment_upload_failed');
        let ready = false;
        for (let i=0;i<120;i++) {
          ready = await contents.executeJavaScript(`(() => {
            const names = ${JSON.stringify(attachments.map(a => a.name))};
            const groups = Array.from(document.querySelectorAll('form [role="group"]')).map(e=>e.getAttribute('aria-label'));
            const button = document.querySelector('button[data-testid="send-button"]');
            return names.every(name=>groups.includes(name)) && !!button && !button.disabled;
          })()`);
          if (ready) break; await delay(500);
        }
        if (!ready) throw fail('saved_send_attachment_upload_failed');
      }
      const initial = await contents.executeJavaScript(inspect);
      if (initial.ready && initial.text.trim() === text.trim()) return;
      if (initial.text.trim() && !repairableComposerDraft(initial.text.trim(), text.trim())) throw fail("saved_send_existing_draft");
      // Match the main adapter's plain-text editing command. insertText is treated
      // as typing by Lexical and can activate Markdown shortcuts or alter newlines.
      const inserted = await contents.executeJavaScript(`(() => {
        const editor = document.querySelector('#prompt-textarea');
        return (${insertComposerText.toString()})(editor, ${JSON.stringify(text)});
      })()`);
      if (!inserted) throw fail("saved_send_draft_mismatch");
      for (let i = 0; i < 40; i++) {
        const state = await contents.executeJavaScript(inspect);
        if (state.ready && state.text.trim() === text.trim()) return;
        await delay(100);
      }
      throw fail("saved_send_draft_mismatch");
    },
    async submit(text) {
      const result = await contents.executeJavaScript(`(() => {
        if (location.href !== ${JSON.stringify(url)}) return false;
        const editor = document.querySelector('#prompt-textarea');
        const button = document.querySelector('button[data-testid="send-button"]');
        if (!editor || (${readComposerText.toString()})(editor).trim() !== ${JSON.stringify(text.trim())} || !button || button.disabled) return false;
        button.click(); return true;
      })()`);
      if (!result) throw fail("saved_send_submit_unavailable");
    },
    async close(onGeneration = () => {}) {
      // Keep the page alive while ChatGPT streams. The user-message receipt is
      // already durable, so even a timeout here must never trigger another click.
      try {
        let completed = false;
        for (let i = 0; i < 300 && !win.isDestroyed(); i++) {
          const running = await contents.executeJavaScript(`!!document.querySelector('[data-testid="stop-button"]')`);
          onGeneration(running);
          if (!running) {completed=true;break;}
          await delay(2000);
        }
        if(!completed) throw fail('saved_send_generation_unobserved');
      } finally { if (!win.isDestroyed()) win.destroy(); }
    },
  };
}

let sender;
const sendStates = new Map();
function recordSendState(request, phase) {
  sendStates.set(`${request.accountKey}:${request.conversationId}`, {version: 1, phase, updatedAt: Date.now()});
  for (const [key, value] of sendStates) if (Date.now() - value.updatedAt > 10 * 60 * 1000) sendStates.delete(key);
}
function savedSendStatus(host, request) {
  if (host.profile !== 'development' || process.env.CODEX_WEB_GPT_SAVED_SEND_ENABLED !== '1') throw fail('saved_send_disabled');
  if (!request || !HASH.test(request.accountKey || '') || !UUID.test(request.conversationId || '') || Object.keys(request).some(k => !['accountKey', 'conversationId'].includes(k))) throw fail('saved_send_invalid_request');
  const state = sendStates.get(`${request.accountKey}:${request.conversationId}`);
  return state && Date.now() - state.updatedAt < 300000 ? state : {version: 1, phase: 'idle', updatedAt: Date.now()};
}
async function sendSavedConversation(host, request) {
  if (host.profile !== "development" || process.env.CODEX_WEB_GPT_SAVED_SEND_ENABLED !== "1" || !process.env.CODEX_WEB_GPT_SAVED_SEND_DIR || host.browserInteractionMode() !== "automatic") throw fail("saved_send_disabled");
  validateRequest(request);
  sender ??= new SavedSender({ directory: process.env.CODEX_WEB_GPT_SAVED_SEND_DIR,
    backgroundClose: true, onState: recordSendState,
    readHistory: id => readSavedHistory(host, { operation: "conversation", id }),
    openConversation: (id, text, attachments) => openSavedConversation(host, id, text, attachments) });
  try { return await host.withManualOperation("saved conversation send", () => sender.send(request)); }
  catch (e) { throw fail(/^saved_send_[a-z_]+$/.test(e?.code || "") ? e.code : "saved_send_failed"); }
}

module.exports = { SavedSender, validateRequest, acknowledgedMessage, sendSavedConversation, savedSendStatus, readComposerText, repairableComposerDraft, insertComposerText,
  openSavedConversation, privateDirectory, readRecord, writeRecord, recordSendState };
