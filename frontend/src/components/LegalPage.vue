<script setup>
import { inject } from 'vue';

defineProps({
  title: { type: String, required: true },
  updated: { type: String, default: '' },
  // Path of the *other* legal page so the top nav can cross-link without
  // each component knowing the global URL map.
  otherPath: { type: String, required: true },
  otherLabel: { type: String, required: true },
});

// App.vue provides `navigate` via `provide('navigate', ...)`. Falls back to a
// plain hard navigation if the page were ever rendered outside the SPA shell.
const navigate = inject('navigate', (p) => {
  window.location.href = p;
});

const go = (e, path) => {
  e.preventDefault();
  navigate(path);
};
</script>

<template>
  <div class="legal-wrap">
    <p class="legal-nav">
      <a href="/" @click="go($event, '/')">← Back to Sidekick</a>
      ·
      <a :href="otherPath" @click="go($event, otherPath)">{{ otherLabel }}</a>
    </p>
    <h1>{{ title }}</h1>
    <p v-if="updated" class="legal-updated">Last updated: {{ updated }}</p>
    <slot />
  </div>
</template>

<style scoped>
.legal-wrap {
  width: 100%;
  max-width: 60rem;
  margin: 0 auto;
  padding: 2rem 2rem 3rem;
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
  overflow-y: auto;
}
@media (max-width: 720px) {
  .legal-wrap { padding: 1.5rem 1rem 3rem; }
}
.legal-nav { margin: 0 0 1.5rem; font-size: 0.9375rem; }
.legal-nav a { color: var(--accent); cursor: pointer; }
:deep(h1) {
  font-size: 1.75rem;
  font-weight: 600;
  margin: 0 0 0.25rem;
  letter-spacing: -0.02em;
}
.legal-updated {
  color: var(--muted);
  font-size: 0.875rem;
  margin: 0 0 2rem;
}
:deep(h2) {
  font-size: 1.125rem;
  font-weight: 600;
  margin: 2rem 0 0.75rem;
  border-top: 1px solid var(--border);
  padding-top: 1.5rem;
}
:deep(h2:first-of-type) {
  border-top: none;
  padding-top: 0;
  margin-top: 0;
}
:deep(h3) { font-size: 1rem; font-weight: 600; margin: 1.25rem 0 0.5rem; }
:deep(p), :deep(li) { margin: 0.5rem 0; color: var(--text); }
:deep(ul) { padding-left: 1.25rem; margin: 0.5rem 0; }
</style>

<style>
/* Wrapper that lets <LegalPage> fill the full viewport when used as the only
   top-level surface (i.e. anonymous visitors hitting /privacy-policy). When
   nested inside .main it just behaves like normal flow. */
.legal-page-host {
  flex: 1;
  display: flex;
  justify-content: center;
  overflow-y: auto;
}
</style>
