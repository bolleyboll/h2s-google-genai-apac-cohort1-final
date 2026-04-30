<script setup>
defineProps({
  view: { type: String, required: true },
  activeChat: { type: Object, default: null },
  sidebarCollapsed: { type: Boolean, required: true },
  mcpServers: { type: Array, default: () => [] },
  chatGrantedMcp: { type: Array, default: () => [] },
});
defineEmits(['toggle-collapse', 'open-mcp']);
</script>

<template>
  <div class="main-head">
    <button
      v-if="sidebarCollapsed"
      class="icon-btn"
      @click="$emit('toggle-collapse', false)"
      title="Open sidebar"
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M3 6h18M3 12h18M3 18h18" />
      </svg>
    </button>
    <h1 v-if="view === 'chat' && activeChat">{{ activeChat.title }}</h1>
    <h1 v-else-if="view === 'central'">All resources</h1>
    <span v-if="view === 'central'" class="subtitle">across every chat you own</span>
    <button
      v-if="view === 'chat' && activeChat && mcpServers.length"
      class="icon-btn"
      @click="$emit('open-mcp')"
      :title="`MCP access (${chatGrantedMcp.length}/${mcpServers.length})`"
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="12" cy="12" r="3" />
        <path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 11-4 0v-.09A1.65 1.65 0 008 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 11-2.83-2.83l.06-.06A1.65 1.65 0 004.6 15a1.65 1.65 0 00-1.51-1H3a2 2 0 110-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06A1.65 1.65 0 009 4.6a1.65 1.65 0 001-1.51V3a2 2 0 114 0v.09A1.65 1.65 0 0015 4.6a1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06A1.65 1.65 0 0019.4 9c.36.43.61.97.6 1.51V11a2 2 0 110 4h-.09a1.65 1.65 0 00-1.51 1z" />
      </svg>
    </button>
  </div>
</template>

<style scoped>
.main-head {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.65rem 1rem;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
}
.main-head h1 {
  margin: 0;
  font-size: 0.95rem;
  font-weight: 600;
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.main-head .subtitle {
  font-size: 0.75rem;
  color: var(--muted);
  margin-left: 0.5rem;
  flex-shrink: 0;
}
</style>
