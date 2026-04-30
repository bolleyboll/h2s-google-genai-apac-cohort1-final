<script setup>
import { inject } from 'vue';

defineProps({
  collapsed: { type: Boolean, required: true },
  chats: { type: Array, required: true },
  activeChatId: { type: Number, default: null },
  view: { type: String, required: true },
  oauthEnabled: { type: Boolean, required: true },
  userEmail: { type: String, default: '' },
  theme: { type: String, required: true },
});
defineEmits([
  'toggle-collapse',
  'open-chat',
  'create-chat',
  'open-central',
  'rename-chat',
  'delete-chat',
  'toggle-theme',
]);

const avatarInitial = (email) => (email?.[0] || 'S').toUpperCase();
const navigate = inject('navigate', (p) => { window.location.href = p; });
const goLegal = (e, path) => { e.preventDefault(); navigate(path); };
</script>

<template>
  <aside class="sidebar" :class="{ collapsed }">
    <div class="sidebar-head">
      <div class="brand">⚡ Sidekick</div>
      <button
        class="icon-btn"
        @click="$emit('toggle-collapse', true)"
        aria-label="Collapse sidebar"
        title="Collapse"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M15 6l-6 6 6 6" />
        </svg>
      </button>
    </div>

    <button class="new-chat-btn" @click="$emit('create-chat')">
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5">
        <path d="M12 5v14M5 12h14" />
      </svg>
      New chat
    </button>

    <div class="sidebar-section-label">Chats</div>
    <div class="chat-list">
      <div
        v-for="chat in chats"
        :key="chat.id"
        class="chat-row"
        :class="{ active: activeChatId === chat.id && view === 'chat' }"
        @click="$emit('open-chat', chat.id)"
      >
        <span class="chat-title">{{ chat.title }}</span>
        <span class="row-actions" @click.stop>
          <button class="icon-btn" @click="$emit('rename-chat', chat)" title="Rename">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M12 20h9" />
              <path d="M16.5 3.5a2.121 2.121 0 113 3L7 19l-4 1 1-4 12.5-12.5z" />
            </svg>
          </button>
          <button class="icon-btn" @click="$emit('delete-chat', chat)" title="Delete">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6" />
            </svg>
          </button>
        </span>
      </div>
      <div v-if="!chats.length" class="empty-state" style="padding: 1.5rem 0.75rem;">
        No chats yet — start one above.
      </div>
    </div>

    <div class="sidebar-foot">
      <div class="nav-item" :class="{ active: view === 'central' }" @click="$emit('open-central')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <rect x="3" y="3" width="7" height="7" rx="1" />
          <rect x="14" y="3" width="7" height="7" rx="1" />
          <rect x="3" y="14" width="7" height="7" rx="1" />
          <rect x="14" y="14" width="7" height="7" rx="1" />
        </svg>
        All resources
      </div>
      <div class="nav-item" @click="$emit('toggle-theme')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path v-if="theme === 'dark'" d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z" />
          <g v-else>
            <circle cx="12" cy="12" r="5" />
            <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42" />
          </g>
        </svg>
        {{ theme === 'dark' ? 'Light mode' : 'Dark mode' }}
      </div>
      <div v-if="oauthEnabled" class="user-strip">
        <span class="avatar">{{ avatarInitial(userEmail) }}</span>
        <span style="flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
          {{ userEmail || 'Signed in' }}
        </span>
        <a class="icon-btn" href="/logout" title="Sign out">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9" />
          </svg>
        </a>
      </div>
      <div class="legal-strip">
        <a href="/privacy-policy" @click="goLegal($event, '/privacy-policy')">Privacy</a>
        ·
        <a href="/terms-and-conditions" @click="goLegal($event, '/terms-and-conditions')">Terms</a>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.sidebar {
  width: var(--sidebar-w);
  background: var(--sidebar);
  color: var(--sidebar-fg);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  transition: margin-left 0.2s ease;
}
.sidebar.collapsed { margin-left: calc(-1 * var(--sidebar-w)); }
.sidebar-head {
  padding: 0.75rem 1rem 0.5rem;
  display: flex;
  align-items: center;
  gap: 0.5rem;
  justify-content: space-between;
}
.sidebar-head .brand {
  font-weight: 700;
  font-size: 1rem;
  letter-spacing: 0.02em;
}
.new-chat-btn {
  margin: 0.5rem 0.75rem 0.75rem;
  padding: 0.55rem 0.75rem;
  border: 1px solid var(--border-strong);
  background: var(--surface);
  color: var(--sidebar-fg);
  border-radius: 8px;
  display: flex;
  align-items: center;
  gap: 0.5rem;
  justify-content: center;
  font-weight: 500;
  transition: background 0.15s ease, border-color 0.15s ease;
}
.new-chat-btn:hover { background: var(--surface-2); }
.sidebar-section-label {
  padding: 0.5rem 1rem 0.25rem;
  font-size: 0.7rem;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--sidebar-muted);
}
.chat-list { flex: 1; overflow-y: auto; padding: 0 0.5rem 0.5rem; }
.chat-row {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.45rem 0.55rem;
  border-radius: 8px;
  cursor: pointer;
  color: var(--sidebar-fg);
  font-size: 0.9rem;
  position: relative;
}
.chat-row:hover { background: var(--surface-2); }
.chat-row.active { background: var(--sidebar-active); }
.chat-row .chat-title {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.chat-row .row-actions {
  display: none;
  gap: 0.15rem;
  flex-shrink: 0;
}
.chat-row:hover .row-actions,
.chat-row.active .row-actions { display: inline-flex; }
.chat-row .row-actions :deep(.icon-btn) { width: 1.6rem; height: 1.6rem; }
.sidebar-foot {
  border-top: 1px solid var(--border);
  padding: 0.5rem;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 0.55rem;
  padding: 0.5rem 0.6rem;
  border-radius: 8px;
  color: var(--sidebar-fg);
  cursor: pointer;
  font-size: 0.88rem;
}
.nav-item:hover { background: var(--surface-2); }
.nav-item.active { background: var(--sidebar-active); }
.nav-item svg { width: 1rem; height: 1rem; flex-shrink: 0; opacity: 0.85; }
.user-strip {
  padding: 0.5rem 0.6rem;
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.8rem;
  color: var(--sidebar-muted);
}
.user-strip .avatar {
  width: 1.6rem;
  height: 1.6rem;
  border-radius: 50%;
  background: var(--accent);
  color: var(--on-accent);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-weight: 600;
  font-size: 0.7rem;
  flex-shrink: 0;
}
.legal-strip {
  padding: 0.25rem 0.6rem 0.5rem;
  font-size: 0.72rem;
  color: var(--sidebar-muted);
  display: flex;
  align-items: center;
  gap: 0.35rem;
}
.legal-strip a {
  color: var(--sidebar-muted);
  cursor: pointer;
}
.legal-strip a:hover { color: var(--accent); }

@media (max-width: 720px) {
  .sidebar { position: fixed; height: 100vh; z-index: 50; }
}
</style>
