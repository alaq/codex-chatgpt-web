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

function validateRequest(r) {
  if (!r || typeof r !== "object" || Array.isArray(r) || r.version !== 1
    || !UUID.test(r.conversationId || "") || !HASH.test(r.accountKey || "") || !HASH.test(r.transactionId || "")
    || typeof r.text !== "string" || !r.text.trim() || Buffer.byteLength(r.text) > 12000 || r.text.includes("\0")
    || Object.keys(r).some(k => !["version", "conversationId", "accountKey", "transactionId", "text"].includes(k))) {
    throw fail("saved_send_invalid_request");
  }
  return { version: 1, conversationId: r.conversationId, accountKey: r.accountKey, transactionId: r.transactionId, textHash: hash(r.text) };
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
    && n.message?.author?.role === "user" && n.message.content?.content_type === "text"
    && Array.isArray(n.message.content.parts) && n.message.content.parts.every(p => typeof p === "string")
    && hash(n.message.content.parts.join("\n")) === record.textHash);
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
  const value = editor.style.getPropertyValue('white-space');
  const priority = editor.style.getPropertyPriority('white-space');
  try {
    editor.style.setProperty('white-space', 'pre-wrap', 'important');
    if (editor.tagName === 'TEXTAREA' || editor.tagName === 'INPUT') {
      editor.setSelectionRange(0, editor.value.length);
    } else {
      const selection = window.getSelection(), range = document.createRange();
      range.selectNodeContents(editor);
      selection.removeAllRanges(); selection.addRange(range);
    }
    return document.execCommand('insertText', false, text);
  } finally {
    if (value) editor.style.setProperty('white-space', value, priority);
    else editor.style.removeProperty('white-space');
  }
}

class SavedSender {
  constructor({ directory, readHistory, openConversation, wait = delay, attempts = 20 }) {
    Object.assign(this, { directory, readHistory, openConversation, wait, attempts });
    this.busy = false;
  }
  async send(request) {
    const identity = validateRequest(request);
    if (!path.isAbsolute(this.directory)) throw fail("saved_send_unsafe_state_directory");
    if (this.busy) throw fail("saved_send_busy");
    this.busy = true;
    let ui;
    try {
      privateDirectory(this.directory);
      const dir = path.join(this.directory, hash(`${request.accountKey}:${request.conversationId}`));
      privateDirectory(dir);
      const file = path.join(dir, `${request.transactionId}.json`), activeFile = path.join(dir, "active.json");
      let data = checkedHistory(await this.readHistory(request.conversationId), request);
      const previous = readRecord(file);
      if (previous && Object.keys(identity).some(k => previous[k] !== identity[k])) throw fail("saved_send_transaction_conflict");
      const settle = record => {
        if (!record || record.version !== 1 || record.accountKey !== request.accountKey || record.conversationId !== request.conversationId || !HASH.test(record.transactionId || "") || !HASH.test(record.textHash || "") || !["submitting", "accepted"].includes(record.status)) throw fail("saved_send_invalid_record");
        if (record.status === "accepted") {
          if (!UUID.test(record.userMessageId || "")) throw fail("saved_send_invalid_record");
          return record;
        }
        const receipt = readRecord(path.join(dir, `${record.transactionId}.json`));
        if (receipt?.status === "accepted") {
          if (["version", "accountKey", "conversationId", "transactionId", "textHash", "parentId"].some(k => receipt[k] !== record[k]) || !UUID.test(receipt.userMessageId || "")) throw fail("saved_send_invalid_record");
          writeRecord(activeFile, receipt);
          return receipt;
        }
        const id = acknowledgedMessage(data, record);
        if (!id) throw fail("saved_send_uncertain");
        record = { ...record, status: "accepted", userMessageId: id };
        writeRecord(path.join(dir, `${record.transactionId}.json`), record);
        writeRecord(activeFile, record);
        return record;
      };
      if (previous) {
        const done = settle(previous);
        return { version: 1, status: "accepted", userMessageId: done.userMessageId, replayed: true };
      }
      const active = readRecord(activeFile);
      if (active) settle(active); // An unresolved prior attempt blocks new sends to this chat.
      checkIdle(data);
      const parentId = data.current_node;
      ui = await this.openConversation(request.conversationId, request.text);
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
          return { version: 1, status: "accepted", userMessageId: done.userMessageId, replayed: false };
        } catch (error) {
          if (error.code === "saved_send_account_mismatch") throw fail("saved_send_uncertain");
          if (attempt + 1 < this.attempts) await this.wait(1000);
        }
      }
      throw fail("saved_send_uncertain");
    } finally {
      try { await ui?.close(); } finally { this.busy = false; }
    }
  }
}

async function openSavedConversation(host, id, expectedText) {
  const { BrowserWindow } = require("electron");
  const url = `https://chatgpt.com/c/${id}`;
  const win = new BrowserWindow({ show: false, width: 1000, height: 900, webPreferences: {
    session: host.view.webContents.session, nodeIntegration: false, contextIsolation: true, sandbox: true, backgroundThrottling: false,
  } });
  const contents = win.webContents;
  contents.setWindowOpenHandler(() => ({ action: "deny" }));
  contents.on("will-navigate", (event, target) => { if (target !== url) event.preventDefault(); });
  const inspect = `(() => {
    const editor = document.querySelector('#prompt-textarea');
    const stop = document.querySelector('[data-testid="stop-button"]');
    return {ready: !!editor && (editor.isContentEditable || editor.tagName === 'TEXTAREA') && !stop,
      text: (${readComposerText.toString()})(editor)};
  })()`;
  try {
    await contents.loadURL(url);
    let ready = false;
    for (let i = 0; i < 100; i++) {
      if (contents.getURL() !== url) throw fail("saved_send_wrong_page");
      const state = await contents.executeJavaScript(inspect);
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
    async prepare(text) {
      if (contents.getURL() !== url) throw fail("saved_send_wrong_page");
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
    async close() {
      // Keep the page alive while ChatGPT streams. The user-message receipt is
      // already durable, so even a timeout here must never trigger another click.
      try {
        for (let i = 0; i < 90 && !win.isDestroyed(); i++) {
          const running = await contents.executeJavaScript(`!!document.querySelector('[data-testid="stop-button"]')`);
          if (!running) break;
          await delay(2000);
        }
      } finally { if (!win.isDestroyed()) win.destroy(); }
    },
  };
}

let sender;
async function sendSavedConversation(host, request) {
  if (host.profile !== "development" || process.env.CODEX_WEB_GPT_SAVED_SEND_ENABLED !== "1" || !process.env.CODEX_WEB_GPT_SAVED_SEND_DIR || host.browserInteractionMode() !== "automatic") throw fail("saved_send_disabled");
  validateRequest(request);
  sender ??= new SavedSender({ directory: process.env.CODEX_WEB_GPT_SAVED_SEND_DIR,
    readHistory: id => readSavedHistory(host, { operation: "conversation", id }),
    openConversation: (id, text) => openSavedConversation(host, id, text) });
  try { return await host.withManualOperation("saved conversation send", () => sender.send(request)); }
  catch (e) { throw fail(/^saved_send_[a-z_]+$/.test(e?.code || "") ? e.code : "saved_send_failed"); }
}

module.exports = { SavedSender, validateRequest, acknowledgedMessage, sendSavedConversation, readComposerText, repairableComposerDraft, insertComposerText };
