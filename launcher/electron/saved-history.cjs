// Read-only, DEV-only pilot. Session credentials never leave this process.
const { createHash } = require("node:crypto");

const ORIGIN = "https://chatgpt.com";
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const MAX_BYTES = 32 * 1024 * 1024;

function historyPath(request) {
  if (!request || typeof request !== "object" || Array.isArray(request)) throw new Error("Invalid history request");
  if (request.operation === "list") {
    const { offset = 0, limit = 20 } = request;
    if (!Number.isInteger(offset) || offset < 0 || offset > 1000000
      || !Number.isInteger(limit) || limit < 1 || limit > 50
      || Object.keys(request).some(key => !["operation", "offset", "limit"].includes(key))) {
      throw new Error("Invalid history page bounds");
    }
    return `/backend-api/conversations?offset=${offset}&limit=${limit}&order=updated`;
  }
  if (request.operation === "conversation" && typeof request.id === "string" && UUID.test(request.id)
    && Object.keys(request).every(key => ["operation", "id"].includes(key))) {
    return `/backend-api/conversation/${request.id.toLowerCase()}`;
  }
  throw new Error("Unsupported history operation");
}

async function fetchJson(session, path, token, maxBytes = MAX_BYTES) {
  // Every URL is constructed here, and redirects are rejected before forwarding credentials.
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30000);
  try {
    const response = await session.fetch(ORIGIN + path, {
      method: "GET", credentials: "include", cache: "no-store", redirect: "error",
      headers: { accept: "application/json", ...(token ? { authorization: `Bearer ${token}` } : {}) },
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`History HTTP ${response.status}`);
    if (!response.headers.get("content-type")?.includes("application/json")) throw new Error("History returned non-JSON content");
    const reader = response.body.getReader();
    const chunks = [];
    let bytes = 0;
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        bytes += value.byteLength;
        if (bytes > maxBytes) throw new Error("History response exceeds size limit");
        chunks.push(Buffer.from(value));
      }
    } finally { await reader.cancel().catch(() => {}); }
    const raw = Buffer.concat(chunks).toString("utf8");
    let data;
    try { data = JSON.parse(raw); } catch { throw new Error("History returned malformed JSON"); }
    return { data, raw };
  } catch (error) {
    // Never surface native networking errors, response bodies, or session payloads in logs.
    if (/^History (HTTP \d+|returned non-JSON content|returned malformed JSON|response exceeds size limit)$/.test(error?.message)) throw error;
    throw new Error("History request failed or timed out");
  } finally { clearTimeout(timeout); }
}

async function readSavedHistory(host, request) {
  if (host.profile !== "development" || process.env.CODEX_WEB_GPT_HISTORY_ENABLED !== "1") {
    throw new Error("Saved-history pilot is not enabled in this DEV launcher");
  }
  if (host.browserInteractionMode() !== "automatic") throw new Error("Saved-history reading requires automatic mode");
  const path = historyPath(request);
  const session = host.view?.webContents?.session;
  if (!session) throw new Error("History browser session is unavailable");
  const { data: auth } = await fetchJson(session, "/api/auth/session", undefined, 1024 * 1024);
  if (typeof auth?.user?.id !== "string" || !auth.user.id || typeof auth.accessToken !== "string" || !auth.accessToken
    || auth.error || (auth.expires && !(Date.parse(auth.expires) > Date.now()))) {
    throw new Error("History requires a signed-in ChatGPT session");
  }
  const accountKey = createHash("sha256").update(`chatgpt-history-v1:${auth.user.id}`).digest("hex");
  const startedAt = new Date().toISOString();
  const { data, raw } = await fetchJson(session, path, auth.accessToken);
  if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("Invalid history object");
  if (request.operation === "list" && !Array.isArray(data.items)) throw new Error("History list schema changed");
  if (request.operation === "conversation" && (!data.mapping || (data.conversation_id || data.id) !== request.id)) {
    throw new Error("History conversation identity or schema mismatch");
  }
  return { version: 1, accountKey, startedAt, finishedAt: new Date().toISOString(), path, raw };
}

module.exports = { historyPath, fetchJson, readSavedHistory };
