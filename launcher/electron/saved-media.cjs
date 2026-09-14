const { createHash } = require('node:crypto');
const { readSavedHistory, fetchJson } = require('./saved-history.cjs');
const HASH = /^[a-f0-9]{64}$/;
const UUID = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/;
const FILE = /^file[-_][A-Za-z0-9_-]{8,128}$/;
const SANDBOX = /^sandbox:\/mnt\/data\/(?:[A-Za-z0-9_. -]+\/)*[A-Za-z0-9_. -]+$/;
const MAX_MEDIA = 20 * 1024 * 1024;
function fail(code) { const e = new Error(code); e.code = code; return e; }

function visibleAttachments(data, messageId) {
  let current = data.current_node;
  const seen = new Set();
  while (current != null && !seen.has(current)) {
    seen.add(current);
    const node = data.mapping?.[current], m = node?.message;
    if (!node) break;
    if (m?.id === messageId && ['user', 'assistant'].includes(m.author?.role)
      && !m.metadata?.is_visually_hidden_from_conversation && !['analysis', 'justify', 'confidence'].includes(m.channel)
      && [undefined, null, 'all'].includes(m.recipient)) {
      const attachments=[...(m.metadata?.attachments || [])];
      const text=(m.content?.parts || []).filter(p=>typeof p==='string').join('\n');
      for(const match of text.matchAll(/\]\((sandbox:\/mnt\/data\/[^)\n]+)\)/g)) {
        if(SANDBOX.test(match[1])&&!match[1].split('/').includes('..')) attachments.push({id:match[1],name:match[1].split('/').pop(),mime_type:'application/octet-stream',size:0});
      }
      for(const part of m.content?.parts || []) {
        const id=typeof part?.asset_pointer==='string'?part.asset_pointer.replace(/^file-service:\/\//,''):'';
        if(part?.content_type==='image_asset_pointer'&&FILE.test(id)&&!attachments.some(a=>a.id===id)) attachments.push({id,name:'image.png',mime_type:'image/png',size:0});
      }
      return attachments;
    }
    current = node.parent;
  }
  return [];
}

function safeDownloadURL(raw) {
  try {
    const u = new URL(raw);
    return u.protocol === 'https:' && !u.username && !u.password && !u.port &&
      (u.hostname === 'chatgpt.com' || u.hostname.endsWith('.oaiusercontent.com') || u.hostname.endsWith('.blob.core.windows.net'));
  } catch { return false; }
}

async function downloadSavedMedia(host, request) {
  if (!request || !HASH.test(request.accountKey || '') || !UUID.test(request.conversationId || '')
    || typeof request.messageId !== 'string' || !(FILE.test(request.attachmentId || '') || SANDBOX.test(request.attachmentId || '') && !request.attachmentId.split('/').includes('..'))
    || Object.keys(request).some(k => !['accountKey', 'conversationId', 'messageId', 'attachmentId'].includes(k))) throw fail('saved_media_invalid_request');
  const envelope = await readSavedHistory(host, {operation: 'conversation', id: request.conversationId});
  if (envelope.accountKey !== request.accountKey) throw fail('saved_media_account_mismatch');
  const attachment = visibleAttachments(JSON.parse(envelope.raw), request.messageId).find(a => a.id === request.attachmentId);
  if (!attachment || !Number.isSafeInteger(attachment.size) || attachment.size < 0 || attachment.size > MAX_MEDIA) throw fail('saved_media_unavailable');
  const session = host.view.webContents.session;
  const {data: auth} = await fetchJson(session, '/api/auth/session', undefined, 1024 * 1024);
  if (typeof auth?.user?.id !== 'string' || typeof auth.accessToken !== 'string'
    || createHash('sha256').update(`chatgpt-history-v1:${auth.user.id}`).digest('hex') !== request.accountKey) throw fail('saved_media_account_mismatch');
  const endpoint=request.attachmentId.startsWith('sandbox:')
    ? `/backend-api/conversation/${request.conversationId}/interpreter/download?message_id=${encodeURIComponent(request.messageId)}&sandbox_path=${encodeURIComponent(request.attachmentId.slice(8))}`
    : `/backend-api/files/download/${request.attachmentId}?conversation_id=${request.conversationId}`;
  const {data: link} = await fetchJson(session, endpoint, auth.accessToken, 64 * 1024);
  if (!safeDownloadURL(link.download_url)) throw fail('saved_media_invalid_download');
  // The signed file URL is used only here. Never send the session bearer token
  // to file storage, forward redirects, or export the signed URL to consumers.
  const response = await session.fetch(link.download_url, {method: 'GET', credentials: 'omit', redirect: 'error', signal: AbortSignal.timeout(60000)});
  if (!response.ok) throw fail('saved_media_download_failed');
  const chunks = []; let size = 0;
  for await (const chunk of response.body) { size += chunk.length; if (size > MAX_MEDIA) throw fail('saved_media_too_large'); chunks.push(Buffer.from(chunk)); }
  const bytes = Buffer.concat(chunks);
  if (attachment.size && bytes.length !== attachment.size) throw fail('saved_media_size_mismatch');
  const name = String(attachment.name || 'attachment').split(/[/\\]/).pop().replace(/[\x00-\x1f\x7f]/g, '_').slice(0, 240) || 'attachment';
  const contentType=response.headers.get('content-type')?.split(';')[0];
  const mimeType=contentType && /^[\w.+-]+\/[\w.+-]+$/.test(contentType) ? contentType : attachment.mime_type || 'application/octet-stream';
  return {version: 1, attachmentId: request.attachmentId, name, mimeType, size,
    sha256: createHash('sha256').update(bytes).digest('hex'), data: bytes.toString('base64')};
}
module.exports = {downloadSavedMedia, visibleAttachments, safeDownloadURL};
