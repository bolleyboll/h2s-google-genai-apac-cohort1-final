<script setup>
import { computed } from 'vue';

const props = defineProps({
  runs: { type: Array, required: true },
  loading: { type: Boolean, required: true },
  error: { type: String, default: '' },
});

defineEmits(['refresh']);

const pretty = (value) => {
  if (value == null) return 'null';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
};

const formatTime = (value) => {
  if (!value) return 'now';
  try {
    const dt = new Date(value);
    return Number.isNaN(dt.getTime()) ? String(value) : dt.toLocaleString();
  } catch {
    return String(value);
  }
};

const topRun = computed(() => props.runs?.[0] || null);
</script>

<template>
  <aside class="right-rail telemetry-rail">
    <section class="rail-section">
      <header class="rail-head">
        <span class="rail-title">Timeline</span>
        <span
          class="rail-spinner"
          :class="{ 'is-loading': loading }"
          aria-hidden="true"
        />
        <button class="icon-btn" @click="$emit('refresh')" :title="loading ? 'Loading…' : 'Refresh'">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21 12a9 9 0 11-3.13-6.84M21 4v5h-5" />
          </svg>
        </button>
      </header>
      <div v-if="error" class="modal-error">{{ error }}</div>
      <div v-else-if="!runs.length" class="rail-empty">
        No agent runs yet. Send a message and Sidekick will show the execution steps here.
      </div>

      <template v-else>
        <article v-for="run in runs" :key="run.id" class="telemetry-run" :class="{ 'is-latest': topRun && run.id === topRun.id }">
          <details :open="topRun && run.id === topRun.id" class="telemetry-run-details">
            <summary class="telemetry-run-summary">
              <span class="telemetry-run-title">{{ run.run_path || 'run' }}</span>
              <span class="telemetry-run-meta">{{ run.event_count || 0 }} events</span>
              <span class="telemetry-run-meta">{{ run.duration_ms || 0 }} ms</span>
              <span class="telemetry-run-meta">{{ formatTime(run.created_at) }}</span>
            </summary>
            <div class="telemetry-run-body">
              <div v-if="run.user_text" class="telemetry-blurb">
                <span class="telemetry-label">User</span>
                <div class="telemetry-pre">{{ run.user_text }}</div>
              </div>
              <div v-if="run.assistant_text" class="telemetry-blurb">
                <span class="telemetry-label">Assistant</span>
                <div class="telemetry-pre">{{ run.assistant_text }}</div>
              </div>

              <div class="telemetry-steps">
                <details v-for="step in (run.timeline && run.timeline.steps) || []" :key="step.index" class="telemetry-step" :open="false">
                  <summary class="telemetry-step-summary">
                    <span class="telemetry-step-kind" :class="`is-${step.kind}`">{{ step.kind }}</span>
                    <span class="telemetry-step-title">{{ step.title || step.summary || 'event' }}</span>
                    <span class="telemetry-step-author">{{ step.author || 'unknown' }}</span>
                  </summary>
                  <div class="telemetry-step-body">
                    <div v-if="step.summary" class="telemetry-pre telemetry-summary">{{ step.summary }}</div>
                    <pre class="telemetry-json">{{ pretty(step.payload) }}</pre>
                  </div>
                </details>
              </div>
            </div>
          </details>
        </article>
      </template>
    </section>
  </aside>
</template>

<style scoped>
.telemetry-rail {
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

.rail-head { display: flex; align-items: center; gap: 0.4rem; }
.rail-title {
  flex: 1;
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--muted);
}

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

.telemetry-run {
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--bg);
  overflow: hidden;
}
.telemetry-run.is-latest { border-color: var(--accent); }

.telemetry-run-summary,
.telemetry-step-summary {
  list-style: none;
  cursor: pointer;
}
.telemetry-run-summary::-webkit-details-marker,
.telemetry-step-summary::-webkit-details-marker { display: none; }

.telemetry-run-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem 0.55rem;
  align-items: center;
  padding: 0.7rem 0.8rem;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
}

.telemetry-run-title { font-weight: 600; font-size: 0.85rem; }
.telemetry-run-meta {
  font-size: 0.72rem;
  color: var(--muted);
  padding: 0.08rem 0.35rem;
  border-radius: 999px;
  background: rgba(255,255,255,0.04);
  border: 1px solid var(--border);
}

.telemetry-run-body {
  padding: 0.75rem;
  display: flex;
  flex-direction: column;
  gap: 0.7rem;
}

.telemetry-blurb {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}
.telemetry-label {
  font-size: 0.68rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted);
}
.telemetry-pre {
  white-space: pre-wrap;
  font-size: 0.82rem;
  line-height: 1.45;
  color: var(--text);
}
.telemetry-summary { color: var(--muted); }

.telemetry-steps {
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}
.telemetry-step {
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
  overflow: hidden;
}
.telemetry-step-summary {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 0.4rem;
  align-items: center;
  padding: 0.5rem 0.65rem;
}
.telemetry-step-kind {
  font-size: 0.65rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  border-radius: 999px;
  padding: 0.1rem 0.35rem;
  border: 1px solid var(--border);
}
.telemetry-step-kind.is-tool-call { border-color: var(--accent); color: var(--accent); }
.telemetry-step-kind.is-tool-result { border-color: var(--warn-border); color: var(--warn-text); }
.telemetry-step-kind.is-text { border-color: var(--border); color: var(--muted); }
.telemetry-step-title {
  font-size: 0.82rem;
  font-weight: 600;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.telemetry-step-author {
  font-size: 0.72rem;
  color: var(--muted);
  white-space: nowrap;
}
.telemetry-step-body {
  padding: 0 0.65rem 0.65rem;
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}
.telemetry-json {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 0.76rem;
  line-height: 1.5;
  color: var(--text);
  background: rgba(0,0,0,0.12);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 0.6rem;
}
</style>