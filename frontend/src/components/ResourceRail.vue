<script setup>
import ResourceCard from './ResourceCard.vue';

defineProps({
  kinds: { type: Array, required: true },
  inv: { type: Object, required: true },
  invLoading: { type: Object, required: true },
  invError: { type: Object, required: true },
  activeChatId: { type: Number, default: null },
});
defineEmits(['refresh', 'edit', 'delete', 'open-central']);

const titleFor = (kind) => {
  if (kind === 'notes') return 'Notes';
  if (kind === 'tasks') return 'Tasks';
  return 'Calendar';
};
</script>

<template>
  <aside class="right-rail">
    <section v-for="kind in kinds" :key="kind" class="rail-section">
      <header class="rail-head">
        <span class="rail-title">{{ titleFor(kind) }}</span>
        <span
          class="rail-spinner"
          :class="{ 'is-loading': invLoading[kind] }"
          aria-hidden="true"
        />
        <button
          class="icon-btn"
          @click="$emit('refresh', kind)"
          :title="invLoading[kind] ? 'Loading…' : 'Refresh'"
        >
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21 12a9 9 0 11-3.13-6.84M21 4v5h-5" />
          </svg>
        </button>
      </header>
      <div v-if="invError[kind]" class="modal-error">{{ invError[kind] }}</div>
      <div v-if="!inv[kind].items.length" class="rail-empty">
        Nothing here yet.
      </div>
      <TransitionGroup tag="div" name="rail-card" class="rail-cards">
        <ResourceCard
          v-for="row in inv[kind].items.slice(0, 8)"
          :key="`${kind}-rail-${row.id}`"
          :kind="kind"
          :row="row"
          :active-chat-id="activeChatId"
          compact
          @edit="$emit('edit', $event)"
          @delete="$emit('delete', $event)"
        />
      </TransitionGroup>
      <a
        v-if="inv[kind].items.length > 8"
        class="rail-more"
        @click="$emit('open-central')"
      >
        See all {{ inv[kind].items.length }} in All resources →
      </a>
    </section>
  </aside>
</template>

<style scoped>
.right-rail {
  width: 320px;
  flex-shrink: 0;
  border-left: 1px solid var(--border);
  background: var(--surface);
  overflow-y: auto;
  padding: 0.25rem 0.4rem 1rem;
}
.rail-section {
  padding: 0.6rem 0.5rem 0.75rem;
  border-bottom: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}
.rail-section:last-child { border-bottom: none; }
.rail-head { display: flex; align-items: center; gap: 0.4rem; }
.rail-title {
  flex: 1;
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--muted);
}

/* Calm fading spinner — visible only while loading, fades out instead of
   disappearing instantly so the section never "snaps" between states. */
.rail-spinner {
  width: 0.85rem;
  height: 0.85rem;
  border-radius: 50%;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  opacity: 0;
  transition: opacity 0.4s ease-out;
  animation: rail-spin 0.9s linear infinite;
  animation-play-state: paused;
}
.rail-spinner.is-loading {
  opacity: 1;
  animation-play-state: running;
}
@keyframes rail-spin {
  to { transform: rotate(360deg); }
}

.rail-empty {
  font-size: 0.8rem;
  color: var(--muted);
  padding: 0.2rem 0.1rem;
}
.rail-more {
  display: inline-block;
  margin-top: 0.2rem;
  font-size: 0.75rem;
  color: var(--accent);
  cursor: pointer;
}
.rail-more:hover { text-decoration: underline; }

.rail-cards {
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
  position: relative;
}

/* TransitionGroup: smooth enter/leave/move so refresh ticks don't snap. */
.rail-card-enter-active,
.rail-card-leave-active {
  transition: opacity 0.25s ease, transform 0.25s ease;
}
.rail-card-enter-from,
.rail-card-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
.rail-card-leave-active {
  position: absolute;
  width: calc(100% - 0px);
}
.rail-card-move {
  transition: transform 0.3s ease;
}
</style>
