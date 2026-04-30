<script setup>
import { computed, ref } from 'vue';
import BaseModal from './BaseModal.vue';

const props = defineProps({
  kind: { type: String, required: true },
  row: { type: Object, required: true },
  chats: { type: Array, required: true },
});
const emit = defineEmits(['close', 'submit']);

const resourceType = computed(() => {
  if (props.kind === 'notes') return 'note';
  if (props.kind === 'tasks') return 'task';
  return 'calendar_event';
});
const label = computed(() => {
  if (resourceType.value === 'note') return 'note';
  if (resourceType.value === 'task') return 'task';
  return 'calendar event';
});
const candidates = computed(() =>
  props.chats.filter((c) => c.id !== props.row.chat_id),
);

const targetChatId = ref(candidates.value[0]?.id ?? null);
const error = ref('');
const saving = ref(false);

const submit = async () => {
  if (!targetChatId.value) return;
  saving.value = true;
  error.value = '';
  try {
    await emit('submit', {
      targetChatId: targetChatId.value,
      resourceType: resourceType.value,
      resourceId: props.row.id,
    });
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    saving.value = false;
  }
};
</script>

<template>
  <BaseModal title="Share with another chat" :error-text="error" @close="$emit('close')">
    <p style="margin: 0; font-size: 0.85rem; color: var(--muted);">
      Pick a chat that should be allowed to read and edit this {{ label }}.
    </p>
    <label>
      Target chat
      <select v-model="targetChatId">
        <option v-for="c in candidates" :key="c.id" :value="c.id">{{ c.title }}</option>
      </select>
    </label>
    <template #actions>
      <button class="btn" @click="$emit('close')">Cancel</button>
      <button class="btn btn-primary" @click="submit" :disabled="saving || !targetChatId">
        Grant access
      </button>
    </template>
  </BaseModal>
</template>
