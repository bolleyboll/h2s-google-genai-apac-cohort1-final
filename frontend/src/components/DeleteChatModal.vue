<script setup>
import { ref } from 'vue';
import BaseModal from './BaseModal.vue';

const props = defineProps({ chat: { type: Object, required: true } });
const emit = defineEmits(['close', 'submit']);

const error = ref('');
const saving = ref(false);

const submit = async () => {
  saving.value = true;
  error.value = '';
  try {
    await emit('submit', { id: props.chat.id });
  } catch (e) {
    error.value = e.message || 'Failed to delete';
  } finally {
    saving.value = false;
  }
};
</script>

<template>
  <BaseModal title="Delete chat" :error-text="error" @close="$emit('close')">
    <p style="margin: 0; font-size: 0.88rem; color: var(--muted);">
      “{{ chat.title }}” will be deleted along with its conversation history.
      Resources created in this chat stay in “All resources” but lose their home chat.
    </p>
    <template #actions>
      <button class="btn" @click="$emit('close')">Cancel</button>
      <button class="btn btn-danger" @click="submit" :disabled="saving">Delete</button>
    </template>
  </BaseModal>
</template>
