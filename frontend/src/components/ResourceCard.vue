<script setup>
import { computed } from 'vue';

const props = defineProps({
  kind: { type: String, required: true }, // 'tasks' | 'calendar' | 'notes'
  row: { type: Object, required: true },
  activeChatId: { type: Number, default: null },
  compact: { type: Boolean, default: false },
  showShare: { type: Boolean, default: false },
});

defineEmits(['edit', 'delete', 'share']);

const formatWhen = (s) => {
  if (s == null || s === '') return '—';
  try {
    const d = new Date(s);
    if (Number.isNaN(d.getTime())) return String(s);
    return d.toLocaleString();
  } catch {
    return String(s);
  }
};

const title = computed(() => {
  if (props.kind === 'calendar') {
    return props.row.title || props.row.summary || '(no title)';
  }
  return props.row.title || '(no title)';
});

const isShared = computed(
  () => props.activeChatId != null && props.row.chat_id !== props.activeChatId,
);
</script>

<template>
  <article class="res-card" :class="{ 'res-card-compact': compact }">
    <div class="res-title">{{ title }}</div>
    <div class="res-meta">
      <span>{{ compact ? formatWhen(row.created_at) : `Created ${formatWhen(row.created_at)}` }}</span>
      <span v-if="kind === 'tasks' && row.status">{{ compact ? row.status : `Status: ${row.status}` }}</span>
      <span v-if="kind === 'tasks' && row.due_at">Due {{ formatWhen(row.due_at) }}</span>
      <span v-if="kind === 'calendar'">
        <template v-if="!compact">When: </template>
        {{ formatWhen(row.start_at) }} → {{ formatWhen(row.end_at) }}
      </span>
      <span class="chip" v-if="isShared">{{ compact ? 'shared' : 'shared from another chat' }}</span>
    </div>
    <div v-if="kind === 'notes' && row.body" class="res-body">{{ row.body }}</div>
    <div v-if="kind === 'calendar' && row.notes" class="res-body">{{ row.notes }}</div>
    <div class="res-actions">
      <a v-if="row.google_quick_link" class="btn"
         :href="row.google_quick_link" target="_blank" rel="noopener">
        {{ compact ? 'Open' : 'Open in Google' }}
      </a>
      <button class="btn" @click="$emit('edit', { kind, row })">Edit</button>
      <button v-if="showShare" class="btn" @click="$emit('share', { kind, row })">Share with chat…</button>
      <button class="btn btn-danger" @click="$emit('delete', { kind, row })">Delete</button>
    </div>
  </article>
</template>

<style>
.res-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 0.85rem 1rem;
  box-shadow: var(--shadow);
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}
.res-card .res-title {
  font-weight: 600;
  font-size: 0.95rem;
  color: var(--text);
}
.res-card .res-meta {
  font-size: 0.78rem;
  color: var(--muted);
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem 0.85rem;
}
.res-card .res-body {
  white-space: pre-wrap;
  font-size: 0.88rem;
  color: var(--text);
  max-height: 8rem;
  overflow: hidden;
  text-overflow: ellipsis;
}
.res-card .res-actions {
  display: flex;
  gap: 0.4rem;
  flex-wrap: wrap;
}
.res-card-compact {
  padding: 0.55rem 0.7rem;
  gap: 0.3rem;
  box-shadow: none;
  border-radius: 10px;
}
.res-card-compact .res-title { font-size: 0.88rem; }
.res-card-compact .res-meta { font-size: 0.72rem; gap: 0.35rem 0.65rem; }
.res-card-compact .res-body {
  font-size: 0.8rem;
  max-height: 4.5rem;
  line-height: 1.4;
}
.res-card-compact .res-actions { gap: 0.3rem; }
.res-card-compact .btn { padding: 0.25rem 0.55rem; font-size: 0.75rem; }
</style>
