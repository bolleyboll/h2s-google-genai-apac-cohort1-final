import { ref } from 'vue';
import { apiGet, apiRaw } from '../api.js';

const APP_NAME = 'sidekick';
const PLACEHOLDER_USER = 'web-ui';

/** Deep equality check using JSON serialization */
const deepEqual = (a, b) => JSON.stringify(a) === JSON.stringify(b);

/**
 * Parse the `/api/run` events payload, returning the assistant text plus any
 * structured access-denied payloads we can surface to the user.
 */
const parseEvents = (events) => {
  if (!Array.isArray(events)) return { text: JSON.stringify(events, null, 2) };
  const parts = [];
  let denial = null;
  let mcpDenial = null;

  const inspect = (resp) => {
    if (!resp) return;
    const candidates = [];
    if (typeof resp.result === 'string') {
      try { candidates.push(JSON.parse(resp.result)); } catch {}
    }
    if (resp && typeof resp === 'object') {
      candidates.push(resp);
      if (resp.result && typeof resp.result === 'object') candidates.push(resp.result);
    }
    for (const obj of candidates) {
      if (!obj || typeof obj !== 'object') continue;
      if (obj.error === 'cross_chat_access_denied') denial = obj;
      if (obj.error === 'mcp_access_denied') mcpDenial = obj;
    }
  };

  for (const ev of events) {
    if (!ev || ev.author === 'user') continue;
    const plist = (ev.content && ev.content.parts) || [];
    for (const p of plist) {
      if (p.text) parts.push(p.text);
      if (p.functionResponse) inspect(p.functionResponse.response);
    }
  }

  return {
    text: parts.join('\n').trim() || '(No assistant text in response.)',
    denial,
    mcpDenial,
  };
};

export const useMessages = () => {
  const messages = ref([]);
  const busy = ref(false);

  const load = async (chatId) => {
    if (!chatId) {
      messages.value = [];
      return;
    }
    try {
      const data = await apiGet(`/ui-api/chats/${chatId}/messages`);
      const newMessages = (data.messages || []).map((m) => ({
        role: m.role === 'assistant' ? 'assistant'
          : m.role === 'user' ? 'user' : 'system',
        text: m.text,
      }));
      if (!deepEqual(messages.value, newMessages)) {
        messages.value = newMessages;
      }
    } catch (e) {
      console.error('history load failed', e);
    }
  };

  const send = async ({ text, chat }) => {
    const t = (text || '').trim();
    if (!t || busy.value || !chat) return null;
    messages.value.push({ role: 'user', text: t });
    busy.value = true;
    try {
      const res = await apiRaw('/api/run', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Sidekick-Chat-Id': String(chat.id),
        },
        body: JSON.stringify({
          app_name: APP_NAME,
          user_id: PLACEHOLDER_USER,
          session_id: chat.agent_session_id,
          new_message: { role: 'user', parts: [{ text: t }] },
          streaming: false,
        }),
      });
      const raw = await res.text();
      if (res.status === 401) {
        messages.value.push({ role: 'error', text: 'Unauthorized — please sign in again.' });
        return { unauthorized: true };
      }
      if (!res.ok) {
        messages.value.push({ role: 'error', text: `Request failed (${res.status}): ${raw}` });
        return null;
      }
      let events;
      try { events = JSON.parse(raw); }
      catch {
        messages.value.push({ role: 'assistant', text: raw });
        return null;
      }
      const parsed = parseEvents(events);
      messages.value.push({ role: 'assistant', text: parsed.text });
      if (parsed.denial && parsed.denial.resource_id) {
        // Render an actionable inline card instead of redundant prose. The
        // agent has already verbally proposed the grant; the card gives the
        // user a one-click way to accept.
        messages.value.push({
          role: 'system',
          kind: 'grant-prompt',
          denial: {
            home_chat_title: parsed.denial.home_chat_title || null,
            home_chat_is_orphan: !!parsed.denial.home_chat_is_orphan,
            resource_type: parsed.denial.resource_type,
            resource_id: parsed.denial.resource_id,
          },
          resolved: null,
        });
      }
      if (parsed.mcpDenial) {
        const prefix = parsed.mcpDenial.mcp_prefix || 'this MCP server';
        messages.value.push({
          role: 'system',
          text:
            `This chat doesn’t have access to the “${prefix}” MCP server. ` +
            'Click the gear icon in the chat header to grant access, then retry.',
        });
      }
      return parsed;
    } catch (e) {
      messages.value.push({ role: 'error', text: String(e) });
      return null;
    } finally {
      busy.value = false;
    }
  };

  return { messages, busy, load, send };
};
