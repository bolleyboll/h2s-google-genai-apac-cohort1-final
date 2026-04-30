<script setup>
import { ref, watch } from 'vue';
import BaseModal from './BaseModal.vue';

const props = defineProps({ chat: { type: Object, required: true } });
const emit = defineEmits(['close', 'submit']);

const draft = ref(props.chat.title);
const error = ref('');
const saving = ref(false);

watch(() => props.chat, (c) => { draft.value = c.title; });

const submit = async () => {
  saving.value = true;
  error.value = '';
  try {
    await emit('submit', { id: props.chat.id, title: draft.value });
  } catch (e) {
    error.value = e.message || 'Failed to rename';
  } finally {
    saving.value = false;
  }
};
</script>

<template>
  <BaseModal title="Rename chat" :error-text="error" @close="$emit('close')">
    <label>
      Title
      <input v-model="draft" @keydown.enter="submit" />
    </label>
    <template #actions>
      <button class="btn" @click="$emit('close')">Cancel</button>
      <button class="btn btn-primary" @click="submit" :disabled="saving">Save</button>
    </template>
  </BaseModal>
</template>
