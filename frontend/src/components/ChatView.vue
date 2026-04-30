<script setup>
import ConversationPane from './ConversationPane.vue';
import ChatRail from './ChatRail.vue';

defineProps({
  messages: { type: Array, required: true },
  busy: { type: Boolean, required: true },
  kinds: { type: Array, required: true },
  inv: { type: Object, required: true },
  invLoading: { type: Object, required: true },
  invError: { type: Object, required: true },
  telemetryRuns: { type: Array, required: true },
  telemetryLoading: { type: Boolean, required: true },
  telemetryError: { type: String, default: '' },
  activeChatId: { type: Number, default: null },
});
defineEmits(['send', 'refresh', 'refresh-telemetry', 'edit', 'delete', 'open-central', 'grant', 'dismiss-grant']);
</script>

<template>
  <div class="chat-body">
    <ConversationPane
      :messages="messages"
      :busy="busy"
      @send="$emit('send', $event)"
      @grant="$emit('grant', $event)"
      @dismiss-grant="$emit('dismiss-grant', $event)"
    />
    <ChatRail
      :kinds="kinds"
      :inv="inv"
      :inv-loading="invLoading"
      :inv-error="invError"
      :telemetry-runs="telemetryRuns"
      :telemetry-loading="telemetryLoading"
      :telemetry-error="telemetryError"
      :active-chat-id="activeChatId"
      @refresh="$emit('refresh', $event)"
      @refresh-telemetry="$emit('refresh-telemetry')"
      @edit="$emit('edit', $event)"
      @delete="$emit('delete', $event)"
      @open-central="$emit('open-central')"
    />
  </div>
</template>

<style scoped>
.chat-body {
  flex: 1;
  display: flex;
  min-height: 0;
}
@media (max-width: 960px) {
  .chat-body { flex-direction: column; }
}
</style>
