<script setup>
import { ref } from 'vue';
import BaseModal from './BaseModal.vue';

const props = defineProps({
  servers: { type: Array, required: true },
  granted: { type: Array, required: true },
});
const emit = defineEmits(['close', 'toggle']);

const error = ref('');
const saving = ref('');

const toggle = async (prefix, enable) => {
  saving.value = prefix;
  error.value = '';
  try {
    await emit('toggle', { prefix, enable });
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    saving.value = '';
  }
};
</script>

<template>
  <BaseModal title="MCP access for this chat" :error-text="error" @close="$emit('close')">
    <p style="margin: 0; font-size: 0.85rem; color: var(--muted);">
      Each MCP server is gated per chat. Until you toggle one on here, the agent
      can't read or write through it from this chat — calls return an explicit
      access-denied error instead.
    </p>
    <div v-if="!servers.length" class="empty-state" style="padding: 1rem 0;">
      No MCP servers are configured on the server.
    </div>
    <div
      v-for="prefix in servers"
      :key="prefix"
      style="display: flex; align-items: center; gap: 0.6rem; padding: 0.4rem 0;"
    >
      <input
        type="checkbox"
        :id="`mcp-${prefix}`"
        :checked="granted.includes(prefix)"
        :disabled="saving === prefix"
        @change="toggle(prefix, $event.target.checked)"
      />
      <label
        :for="`mcp-${prefix}`"
        style="flex: 1; font-size: 0.9rem; color: var(--text);"
      >
        {{ prefix }}
        <span v-if="saving === prefix" style="font-size: 0.75rem; color: var(--muted);"> · saving…</span>
      </label>
    </div>
    <template #actions>
      <button class="btn" @click="$emit('close')">Done</button>
    </template>
  </BaseModal>
</template>
