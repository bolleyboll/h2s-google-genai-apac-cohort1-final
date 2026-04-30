import { computed, ref } from 'vue';
import { apiGet, apiSend, apiDelete } from '../api.js';

/** Deep equality check using JSON serialization */
const deepEqual = (a, b) => JSON.stringify(a) === JSON.stringify(b);

/** App-wide chat list + active chat. Exposed as a singleton. */
const chats = ref([]);
const activeChatId = ref(null);
const view = ref('chat'); // 'chat' | 'central'

const refresh = async () => {
  const data = await apiGet('/ui-api/chats');
  const newChats = data?.chats || [];
  if (!deepEqual(chats.value, newChats)) {
    chats.value = newChats;
  }
};

const create = async (title = 'New chat') => {
  const created = await apiSend('/ui-api/chats', 'POST', { title });
  await refresh();
  return created;
};

const rename = async (id, title) => {
  await apiSend(`/ui-api/chats/${id}`, 'PATCH', { title });
  await refresh();
};

const remove = async (id) => {
  await apiDelete(`/ui-api/chats/${id}`);
  await refresh();
};

const setActive = (id) => {
  activeChatId.value = id;
  view.value = 'chat';
};

const goCentral = () => {
  view.value = 'central';
  activeChatId.value = null;
};

const activeChat = computed(
  () => chats.value.find((c) => c.id === activeChatId.value) || null,
);

export const useChats = () => ({
  chats,
  activeChatId,
  activeChat,
  view,
  refresh,
  create,
  rename,
  remove,
  setActive,
  goCentral,
});
