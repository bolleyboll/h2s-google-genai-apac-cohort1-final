import { ref } from 'vue';
import { apiGet, apiSend, apiDelete } from '../api.js';

/** Deep equality check using JSON serialization */
const deepEqual = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const mcpServers = ref([]);   // configured short prefixes from the backend
const chatGrantedMcp = ref([]); // grants for the active chat

const discover = async () => {
  try {
    const data = await apiGet('/ui-api/mcp/servers');
    const newServers = data?.servers || [];
    if (!deepEqual(mcpServers.value, newServers)) {
      mcpServers.value = newServers;
    }
  } catch (e) {
    console.error('mcp discovery failed', e);
  }
};

const refreshGrants = async (chatId) => {
  if (!chatId) {
    chatGrantedMcp.value = [];
    return;
  }
  try {
    const data = await apiGet(`/ui-api/chats/${chatId}/mcp`);
    const newGrants = data?.granted || [];
    if (!deepEqual(chatGrantedMcp.value, newGrants)) {
      chatGrantedMcp.value = newGrants;
    }
  } catch (e) {
    console.error('mcp grants fetch failed', e);
  }
};

const grant = async (chatId, prefix) => {
  await apiSend(`/ui-api/chats/${chatId}/mcp`, 'POST', { mcp_prefix: prefix });
  await refreshGrants(chatId);
};

const revoke = async (chatId, prefix) => {
  await apiDelete(`/ui-api/chats/${chatId}/mcp/${encodeURIComponent(prefix)}`);
  await refreshGrants(chatId);
};

export const useMcp = () => ({
  mcpServers,
  chatGrantedMcp,
  discover,
  refreshGrants,
  grant,
  revoke,
});
