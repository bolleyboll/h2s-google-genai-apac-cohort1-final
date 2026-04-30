<script setup>
import { computed, ref } from 'vue';
import BaseModal from './BaseModal.vue';

const props = defineProps({
  kind: { type: String, required: true },
  row: { type: Object, required: true },
});
const emit = defineEmits(['close', 'submit']);

const label = computed(() => {
  if (props.kind === 'tasks') return 'task';
  if (props.kind === 'calendar') return 'calendar event';
  return 'note';
});

const rowTitle = computed(() => {
  if (props.kind === 'calendar') return props.row.title || props.row.summary || '(no title)';
  return props.row.title || '(no title)';
});

const isGoogleBacked = computed(
  () => props.row.google_doc_id || props.row.google_event_id || props.row.google_task_id,
);

const error = ref('');
const saving = ref(false);

const submit = async () => {
  saving.value = true;
  error.value = '';
  try {
    await emit('submit', { kind: props.kind, row: props.row });
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    saving.value = false;
  }
};
</script>

<template>
  <BaseModal :title="`Delete ${label}`" :error-text="error" @close="$emit('close')">
    <p style="margin: 0; font-size: 0.88rem; color: var(--muted);">
      “{{ rowTitle }}” will be permanently removed
      <span v-if="isGoogleBacked">from Google as well.</span>
    </p>
    <template #actions>
      <button class="btn" @click="$emit('close')">Cancel</button>
      <button class="btn btn-danger" @click="submit" :disabled="saving">
        {{ saving ? 'Deleting…' : 'Delete' }}
      </button>
    </template>
  </BaseModal>
</template>
