<script setup>
import { computed, reactive, ref } from 'vue';
import BaseModal from './BaseModal.vue';

const props = defineProps({
  kind: { type: String, required: true },
  row: { type: Object, required: true },
});
const emit = defineEmits(['close', 'submit']);

const initialForm = (kind, row) => {
  if (kind === 'tasks') {
    return { title: row.title || '', status: row.status || 'open', due_at: row.due_at || '' };
  }
  if (kind === 'calendar') {
    return {
      title: row.title || '',
      start_at: row.start_at || '',
      end_at: row.end_at || '',
      notes: row.notes || '',
    };
  }
  return { title: row.title || '', body: row.body || '' };
};

const form = reactive(initialForm(props.kind, props.row));
const error = ref('');
const saving = ref(false);

const title = computed(() => {
  if (props.kind === 'tasks') return 'Edit task';
  if (props.kind === 'calendar') return 'Edit calendar event';
  return 'Edit note';
});

const submit = async () => {
  saving.value = true;
  error.value = '';
  try {
    await emit('submit', { kind: props.kind, row: props.row, form: { ...form } });
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    saving.value = false;
  }
};
</script>

<template>
  <BaseModal :title="title" :error-text="error" @close="$emit('close')">
    <template v-if="kind === 'tasks'">
      <label>Title<input v-model="form.title" /></label>
      <label>
        Status
        <select v-model="form.status">
          <option value="open">Open</option>
          <option value="needsAction">Needs action</option>
          <option value="completed">Completed</option>
        </select>
      </label>
      <label>Due (RFC 3339, optional)<input v-model="form.due_at" placeholder="2026-04-30T17:00:00Z" /></label>
    </template>

    <template v-else-if="kind === 'calendar'">
      <label>Title<input v-model="form.title" /></label>
      <label>Start (RFC 3339)<input v-model="form.start_at" placeholder="2026-04-30T15:00:00Z" /></label>
      <label>End (RFC 3339)<input v-model="form.end_at" placeholder="2026-04-30T16:00:00Z" /></label>
      <label>Description<textarea v-model="form.notes" /></label>
    </template>

    <template v-else>
      <label>Title<input v-model="form.title" /></label>
      <label>Body<textarea v-model="form.body" rows="8" /></label>
    </template>

    <template #actions>
      <button class="btn" @click="$emit('close')">Cancel</button>
      <button class="btn btn-primary" @click="submit" :disabled="saving">
        {{ saving ? 'Saving…' : 'Save' }}
      </button>
    </template>
  </BaseModal>
</template>
