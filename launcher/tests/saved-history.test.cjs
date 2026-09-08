const test = require("node:test");
const assert = require("node:assert/strict");
const { historyPath, fetchJson, readSavedHistory } = require("../electron/saved-history.cjs");
const id = "11111111-1111-1111-1111-111111111111";
const json = value => new Response(JSON.stringify(value), { headers: { "content-type": "application/json" } });

test("history allows bounded GET operations only", () => {
  assert.equal(historyPath({ operation: "list", offset: 20, limit: 10 }), "/backend-api/conversations?offset=20&limit=10&order=updated");
  assert.equal(historyPath({ operation: "conversation", id }), `/backend-api/conversation/${id}`);
  for (const req of [{operation:"delete",id}, {operation:"conversation",id:"../../api/auth/session"},
    {operation:"list",limit:51}, {operation:"list",offset:-1}, {operation:"list",url:"https://evil.test"}]) {
    assert.throws(() => historyPath(req));
  }
});

test("history rejects unbounded, redirected, and non-JSON responses without reflecting secrets", async () => {
  await assert.rejects(fetchJson({ fetch: async () => new Response("secret", {status:403}) }, "/api/auth/session"), /History HTTP 403/);
  await assert.rejects(fetchJson({ fetch: async () => new Response("secret") }, "/api/auth/session"), /non-JSON/);
  await assert.rejects(fetchJson({ fetch: async () => json({huge:"123456"}) }, "/api/auth/session", undefined, 4), /size limit/);
  await assert.rejects(fetchJson({ fetch: async () => { throw new Error("Bearer sensitive-secret"); } }, "/api/auth/session"), /^Error: History request failed or timed out$/);
});

test("DEV history uses the existing session; returns only hashed identity and requested data", async () => {
  const old = process.env.CODEX_WEB_GPT_HISTORY_ENABLED;
  process.env.CODEX_WEB_GPT_HISTORY_ENABLED = "1";
  const calls=[];
  const session={fetch:async (url, init) => {
    calls.push({url,init});
    return url.endsWith("/api/auth/session") ? json({user:{id:"private-user"},accessToken:"private-access-token"}) : json({conversation_id:id,mapping:{},current_node:null});
  }};
  const host={profile:"development",browserInteractionMode:()=>"automatic",view:{webContents:{session}}};
  try {
    const result=await readSavedHistory(host,{operation:"conversation",id});
    assert.match(result.accountKey,/^[a-f0-9]{64}$/);
    assert(!JSON.stringify(result).includes("private-user"));
    assert(!JSON.stringify(result).includes("private-access-token"));
    assert.equal(calls.length,2);
    assert(calls.every(c=>c.init.method==="GET" && c.init.redirect==="error" && c.init.credentials==="include"));
    assert.equal(calls[1].init.headers.authorization,"Bearer private-access-token");
    await assert.rejects(readSavedHistory({...host,profile:"production"},{operation:"list"}),/not enabled/);
    await assert.rejects(readSavedHistory({...host,browserInteractionMode:()=>"manual"},{operation:"list"}),/automatic mode/);
    delete process.env.CODEX_WEB_GPT_HISTORY_ENABLED;
    await assert.rejects(readSavedHistory(host,{operation:"list"}),/not enabled/);
  } finally {
    if(old===undefined)delete process.env.CODEX_WEB_GPT_HISTORY_ENABLED;else process.env.CODEX_WEB_GPT_HISTORY_ENABLED=old;
  }
});
