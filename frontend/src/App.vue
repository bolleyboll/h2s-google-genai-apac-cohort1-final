<script setup>
import { computed, onBeforeUnmount, onMounted, provide, ref } from 'vue';
import { apiSend, apiDelete } from './api.js';

import { useAuth } from './composables/useAuth.js';
import { useTheme } from './composables/useTheme.js';
import { useChats } from './composables/useChats.js';
import { useMessages } from './composables/useMessages.js';
import { useResources } from './composables/useResources.js';
import { useMcp } from './composables/useMcp.js';
import { useAutoRefresh } from './composables/useAutoRefresh.js';
import { useTelemetry } from './composables/useTelemetry.js';

import SignInScreen from './components/SignInScreen.vue';
import SidebarNav from './components/SidebarNav.vue';
import MainHeader from './components/MainHeader.vue';
import ChatView from './components/ChatView.vue';
import CentralView from './components/CentralView.vue';
import PrivacyPolicyView from './components/PrivacyPolicyView.vue';
import TermsOfServiceView from './components/TermsOfServiceView.vue';
import RenameChatModal from './components/RenameChatModal.vue';
import DeleteChatModal from './components/DeleteChatModal.vue';
import EditResourceModal from './components/EditResourceModal.vue';
import DeleteResourceModal from './components/DeleteResourceModal.vue';
import ShareResourceModal from './components/ShareResourceModal.vue';
import McpAccessModal from './components/McpAccessModal.vue';

const { authReady, oauthEnabled, signedIn, userEmail, initialize } = useAuth();
const { theme, toggleTheme } = useTheme();
const {
  chats, activeChatId, activeChat, view,
  refresh: refreshChats, create: createChat, rename: renameChat,
  remove: removeChat, setActive: setActiveChat, goCentral,
} = useChats();
const { messages, busy, load: loadMessages, send: sendMessage } = useMessages();
const { runs: telemetryRuns, loading: telemetryLoading, error: telemetryError, load: loadTelemetry } = useTelemetry();
const {
  KINDS, inv, invLoading, invError, resKind,
  reload: reloadInventory, reloadAll: reloadAllInventory,
  anyLoading, editRow, deleteRow,
} = useResources();
const { mcpServers, chatGrantedMcp, discover: discoverMcp,
        refreshGrants: refreshMcp, grant: grantMcp, revoke: revokeMcp } = useMcp();

const sidebarCollapsed = ref(false);

// ----- client-side routing for the two legal pages -----
// Standalone HTML pages were ported into Vue but the URL contract didn't
// change. Read the path on mount, react to browser back/forward via
// `popstate`, and expose `navigate(path)` to children via provide/inject.
const legalView = ref(null); // 'privacy' | 'terms' | null
const LEGAL_BY_PATH = {
  '/privacy-policy': 'privacy',
  '/terms-and-conditions': 'terms',
};
const syncLegalFromPath = () => {
  legalView.value = LEGAL_BY_PATH[window.location.pathname] || null;
};
const navigate = (path) => {
  if (window.location.pathname === path) return;
  window.history.pushState({}, '', path);
  syncLegalFromPath();
};
provide('navigate', navigate);

// Modal state — null when closed; populated objects when open.
const renameTarget = ref(null);
const deleteTarget = ref(null);
const editTarget = ref(null);
const deleteResourceTarget = ref(null);
const shareTarget = ref(null);
const mcpModalOpen = ref(false);

const anyModalOpen = computed(() =>
  !!renameTarget.value || !!deleteTarget.value || !!editTarget.value
  || !!deleteResourceTarget.value || !!shareTarget.value || mcpModalOpen.value
);

// ----- bootstrap -----
onMounted(async () => {
  // Path-sync first so legal pages render immediately, even before auth resolves.
  syncLegalFromPath();
  window.addEventListener('popstate', syncLegalFromPath);

  await initialize();
  if (oauthEnabled.value && !signedIn.value) return;

  await discoverMcp();
  await refreshChats();
  if (chats.value.length === 0) {
    const created = await createChat();
    if (created) await openChat(created.id);
  } else {
    await openChat(chats.value[0].id);
  }
});

onBeforeUnmount(() => {
  window.removeEventListener('popstate', syncLegalFromPath);
});

// ----- chat actions -----
const openChat = async (id) => {
  setActiveChat(id);
  await loadMessages(id);
  await refreshMcp(id);
  await loadTelemetry(id);
  // Stream all three rail kinds in parallel — don't block on them.
  for (const k of KINDS) reloadInventory(k, { chatId: id });
};

const onCreateChat = async () => {
  const created = await createChat();
  if (created) await openChat(created.id);
};

const onOpenCentral = () => {
  goCentral();
  reloadInventory(resKind.value, { scope: 'all' });
};

const onChangeKind = (kind) => {
  resKind.value = kind;
  reloadInventory(kind, { scope: 'all' });
};

const onRefreshActiveView = () => {
  if (view.value === 'chat' && activeChatId.value) {
    for (const k of KINDS) reloadInventory(k, { chatId: activeChatId.value });
    loadTelemetry(activeChatId.value);
  } else if (view.value === 'central') {
    reloadInventory(resKind.value, { scope: 'all' });
  }
};

const onRailRefresh = (kind) => reloadInventory(kind, { chatId: activeChatId.value });
const onRefreshTelemetry = () => {
  if (activeChatId.value) loadTelemetry(activeChatId.value);
};

// ----- send -----
const onSend = async (text) => {
  const result = await sendMessage({ text, chat: activeChat.value });
  if (result?.unauthorized) {
    signedIn.value = false;
    return;
  }
  if (activeChatId.value) await loadTelemetry(activeChatId.value);
  await refreshChats();
};

// ----- inline cross-chat access grant card -----
// When the agent's tool call returns cross_chat_access_denied, useMessages
// pushes a structured 'grant-prompt' card into the transcript. Clicking
// "Grant access" on that card hits the same /access endpoint the central
// Share modal uses, marks the card resolved, then auto-replies "Yes" so the
// agent retries the originally-denied operation.
const onInlineGrant = async ({ index, denial }) => {
  const msg = messages.value[index];
  if (!activeChatId.value || !msg) return;
  try {
    await apiSend(
      `/ui-api/chats/${activeChatId.value}/access`,
      'POST',
      {
        resource_type: denial.resource_type,
        resource_id: denial.resource_id,
      },
    );
    msg.resolved = 'granted';
    await onSend('Yes — please grant access and retry the previous action.');
  } catch (e) {
    msg.resolved = 'error';
    msg.errorText = e.message || String(e);
  }
};
const onDismissGrant = (index) => {
  const msg = messages.value[index];
  if (msg) msg.resolved = 'dismissed';
};

// ----- chat list mutations -----
const submitRename = async ({ id, title }) => {
  await renameChat(id, title);
  renameTarget.value = null;
};
const submitDeleteChat = async ({ id }) => {
  const wasActive = activeChatId.value === id;
  await removeChat(id);
  deleteTarget.value = null;
  if (wasActive) {
    if (chats.value.length > 0) await openChat(chats.value[0].id);
    else await onCreateChat();
  }
};

// ----- resource mutations -----
const submitEdit = async ({ kind, row, form }) => {
  await editRow(kind, row, form);
  editTarget.value = null;
  onRefreshActiveView();
};
const submitDeleteResource = async ({ kind, row }) => {
  await deleteRow(kind, row);
  deleteResourceTarget.value = null;
  onRefreshActiveView();
  await refreshChats();
};
const submitShare = async ({ targetChatId, resourceType, resourceId }) => {
  await apiSend(`/ui-api/chats/${targetChatId}/access`, 'POST', {
    resource_type: resourceType,
    resource_id: resourceId,
  });
  shareTarget.value = null;
};

// ----- MCP modal -----
const onMcpToggle = async ({ prefix, enable }) => {
  if (!activeChatId.value) return;
  if (enable) await grantMcp(activeChatId.value, prefix);
  else await revokeMcp(activeChatId.value, prefix);
};

// ----- auto-refresh tick -----
useAutoRefresh(onRefreshActiveView, {
  intervalMs: 5000,
  shouldSkip: () => anyModalOpen.value || anyLoading() || busy.value,
});
</script>

<template>
  <PrivacyPolicyView v-if="legalView === 'privacy'" />
  <TermsOfServiceView v-else-if="legalView === 'terms'" />

  <SignInScreen v-else-if="authReady && oauthEnabled && !signedIn" />

  <template v-else-if="authReady">
    <SidebarNav
      :collapsed="sidebarCollapsed"
      :chats="chats"
      :active-chat-id="activeChatId"
      :view="view"
      :oauth-enabled="oauthEnabled"
      :user-email="userEmail"
      :theme="theme"
      @toggle-collapse="sidebarCollapsed = $event"
      @open-chat="openChat"
      @create-chat="onCreateChat"
      @open-central="onOpenCentral"
      @rename-chat="renameTarget = $event"
      @delete-chat="deleteTarget = $event"
      @toggle-theme="toggleTheme"
    />
    <div v-if="!sidebarCollapsed" class="scrim" @click="sidebarCollapsed = true"></div>

    <main class="main">
      <MainHeader
        :view="view"
        :active-chat="activeChat"
        :sidebar-collapsed="sidebarCollapsed"
        :mcp-servers="mcpServers"
        :chat-granted-mcp="chatGrantedMcp"
        @toggle-collapse="sidebarCollapsed = $event"
        @open-mcp="mcpModalOpen = true"
      />

      <ChatView
        v-if="view === 'chat' && activeChat"
        :messages="messages"
        :busy="busy"
        :kinds="KINDS"
        :inv="inv"
        :inv-loading="invLoading"
        :inv-error="invError"
        :telemetry-runs="telemetryRuns"
        :telemetry-loading="telemetryLoading"
        :telemetry-error="telemetryError"
        :active-chat-id="activeChatId"
        @send="onSend"
        @refresh="onRailRefresh"
        @refresh-telemetry="onRefreshTelemetry"
        @edit="editTarget = $event"
        @delete="deleteResourceTarget = $event"
        @open-central="onOpenCentral"
        @grant="onInlineGrant"
        @dismiss-grant="onDismissGrant"
      />

      <CentralView
        v-else-if="view === 'central'"
        :res-kind="resKind"
        :inv="inv"
        :inv-loading="invLoading"
        :inv-error="invError"
        @change-kind="onChangeKind"
        @refresh="onRefreshActiveView"
        @edit="editTarget = $event"
        @delete="deleteResourceTarget = $event"
        @share="shareTarget = $event"
      />
    </main>
  </template>

  <RenameChatModal
    v-if="renameTarget"
    :chat="renameTarget"
    @close="renameTarget = null"
    @submit="submitRename"
  />
  <DeleteChatModal
    v-if="deleteTarget"
    :chat="deleteTarget"
    @close="deleteTarget = null"
    @submit="submitDeleteChat"
  />
  <EditResourceModal
    v-if="editTarget"
    :kind="editTarget.kind"
    :row="editTarget.row"
    @close="editTarget = null"
    @submit="submitEdit"
  />
  <DeleteResourceModal
    v-if="deleteResourceTarget"
    :kind="deleteResourceTarget.kind"
    :row="deleteResourceTarget.row"
    @close="deleteResourceTarget = null"
    @submit="submitDeleteResource"
  />
  <ShareResourceModal
    v-if="shareTarget"
    :kind="shareTarget.kind"
    :row="shareTarget.row"
    :chats="chats"
    @close="shareTarget = null"
    @submit="submitShare"
  />
  <McpAccessModal
    v-if="mcpModalOpen"
    :servers="mcpServers"
    :granted="chatGrantedMcp"
    @close="mcpModalOpen = false"
    @toggle="onMcpToggle"
  />
</template>

<style scoped>
.main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg);
}
</style>
