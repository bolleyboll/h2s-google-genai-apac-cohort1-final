<script setup>
import { ref } from 'vue';
import ResourceRail from './ResourceRail.vue';
import TelemetryRail from './TelemetryRail.vue';

defineProps({
  kinds: { type: Array, required: true },
  inv: { type: Object, required: true },
  invLoading: { type: Object, required: true },
  invError: { type: Object, required: true },
  telemetryRuns: { type: Array, required: true },
  telemetryLoading: { type: Boolean, required: true },
  telemetryError: { type: String, default: '' },
  activeChatId: { type: Number, default: null },
});

defineEmits(['refresh', 'edit', 'delete', 'open-central', 'refresh-telemetry']);

const activeTab = ref('resources');
</script>

<template>
  <aside class="chat-rail">
    <div class="rail-tabs">
      <button class="tab-btn" :class="{ active: activeTab === 'resources' }" @click="activeTab = 'resources'">
        Resources
      </button>
      <button class="tab-btn" :class="{ active: activeTab === 'telemetry' }" @click="activeTab = 'telemetry'">
        Timeline
      </button>
    </div>

    <ResourceRail
      v-if="activeTab === 'resources'"
      :kinds="kinds"
      :inv="inv"
      :inv-loading="invLoading"
      :inv-error="invError"
      :active-chat-id="activeChatId"
      @refresh="$emit('refresh', $event)"
      @edit="$emit('edit', $event)"
      @delete="$emit('delete', $event)"
      @open-central="$emit('open-central')"
    />

    <TelemetryRail
      v-else
      :runs="telemetryRuns"
      :loading="telemetryLoading"
      :error="telemetryError"
      @refresh="$emit('refresh-telemetry')"
    />
  </aside>
</template>

<style scoped>
.chat-rail {
  width: 320px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  border-left: 1px solid var(--border);
  background: var(--surface);
  overflow: hidden;
}
.rail-tabs {
  display: flex;
  gap: 0.35rem;
  padding: 0.5rem;
  border-bottom: 1px solid var(--border);
  background: var(--bg);
}
.tab-btn {
  flex: 1;
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 0.4rem 0.65rem;
  background: var(--surface);
  color: var(--muted);
  font-size: 0.78rem;
  font-weight: 600;
}
.tab-btn.active {
  color: var(--text);
  border-color: var(--accent);
  box-shadow: var(--shadow);
}
.chat-rail :deep(.right-rail),
.chat-rail :deep(.telemetry-rail) {
  width: 100%;
  border-left: none;
  border-top: none;
  flex: 1;
  min-height: 0;
}

@media (max-width: 960px) {
  .chat-rail { width: auto; max-height: 45vh; border-left: none; border-top: 1px solid var(--border); }
}
</style>