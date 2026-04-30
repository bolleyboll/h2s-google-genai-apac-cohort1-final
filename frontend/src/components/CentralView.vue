<script setup>
import ResourceCard from './ResourceCard.vue';

defineProps({
  resKind: { type: String, required: true },
  inv: { type: Object, required: true },
  invLoading: { type: Object, required: true },
  invError: { type: Object, required: true },
});
defineEmits(['change-kind', 'refresh', 'edit', 'delete', 'share']);
</script>

<template>
  <div class="pane-tabs">
    <button
      class="pane-tab"
      :class="{ active: resKind === 'tasks' }"
      @click="$emit('change-kind', 'tasks')"
    >Tasks</button>
    <button
      class="pane-tab"
      :class="{ active: resKind === 'calendar' }"
      @click="$emit('change-kind', 'calendar')"
    >Calendar</button>
    <button
      class="pane-tab"
      :class="{ active: resKind === 'notes' }"
      @click="$emit('change-kind', 'notes')"
    >Notes</button>
  </div>
  <div class="resources-surface">
    <div class="resources-toolbar">
      <button class="btn" @click="$emit('refresh')">
        Refresh
      </button>
      <span
        class="central-spinner"
        :class="{ 'is-loading': invLoading[resKind] }"
        aria-hidden="true"
      />
      <span style="margin-left: auto; font-size: 0.8rem; color: var(--muted);">
        Everything you own. Use “Share” to expose a resource to another chat.
      </span>
    </div>
    <div v-if="invError[resKind]" class="modal-error">{{ invError[resKind] }}</div>
    <div v-if="!inv[resKind].items.length" class="empty-state">
      You haven't created any {{ resKind === 'calendar' ? 'calendar events' : resKind }} yet.
    </div>
    <TransitionGroup tag="div" name="central-card" class="resources-grid">
      <ResourceCard
        v-for="row in inv[resKind].items"
        :key="`${resKind}-c-${row.id}`"
        :kind="resKind"
        :row="row"
        show-share
        @edit="$emit('edit', $event)"
        @delete="$emit('delete', $event)"
        @share="$emit('share', $event)"
      />
    </TransitionGroup>
  </div>
</template>

<style scoped>
.pane-tabs {
  display: flex;
  gap: 0.25rem;
  padding: 0.4rem 0.75rem;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
}
.pane-tab {
  padding: 0.35rem 0.75rem;
  border-radius: 8px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--muted);
  font-size: 0.85rem;
  font-weight: 500;
}
.pane-tab:hover { color: var(--text); background: var(--surface-2); }
.pane-tab.active {
  color: var(--text);
  background: var(--surface-2);
  border-color: var(--border);
}
.resources-surface {
  flex: 1;
  overflow-y: auto;
  padding: 1rem 1.25rem 2rem;
}
.resources-toolbar {
  display: flex;
  align-items: center;
  gap: 0.55rem;
  margin-bottom: 1rem;
  flex-wrap: wrap;
}
.resources-grid {
  display: grid;
  gap: 0.75rem;
  position: relative;
}

.central-spinner {
  width: 0.95rem;
  height: 0.95rem;
  border-radius: 50%;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  opacity: 0;
  transition: opacity 0.4s ease-out;
  animation: central-spin 0.9s linear infinite;
  animation-play-state: paused;
}
.central-spinner.is-loading {
  opacity: 1;
  animation-play-state: running;
}
@keyframes central-spin {
  to { transform: rotate(360deg); }
}

.central-card-enter-active,
.central-card-leave-active {
  transition: opacity 0.25s ease, transform 0.25s ease;
}
.central-card-enter-from,
.central-card-leave-to {
  opacity: 0;
  transform: translateY(-6px);
}
.central-card-leave-active {
  position: absolute;
  width: 100%;
}
.central-card-move {
  transition: transform 0.3s ease;
}
</style>
