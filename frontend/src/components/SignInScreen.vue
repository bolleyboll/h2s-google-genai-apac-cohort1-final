<script setup>
import { inject } from 'vue';

const navigate = inject('navigate', (p) => { window.location.href = p; });
const go = (e, path) => { e.preventDefault(); navigate(path); };
</script>

<template>
  <div class="signin-page">
    <div class="signin-card">
      <h2>Sign in to Sidekick</h2>
      <p>Use your Google account to access tasks, calendar, and notes.</p>
      <p><a class="btn btn-primary" href="/login/google">Sign in with Google</a></p>
      <p class="legal-links">
        <a href="/privacy-policy" @click="go($event, '/privacy-policy')">Privacy policy</a>
        ·
        <a href="/terms-and-conditions" @click="go($event, '/terms-and-conditions')">Terms of Service</a>
      </p>
    </div>
  </div>
</template>

<style scoped>
.signin-page {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 2rem;
}
.signin-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 2rem;
  max-width: 22rem;
  text-align: center;
  box-shadow: var(--shadow);
}
.signin-card h2 { margin: 0 0 0.5rem; }
.signin-card p { color: var(--muted); }
.legal-links {
  margin-top: 1.75rem;
  padding-top: 1rem;
  border-top: 1px solid var(--border);
  font-size: 0.85rem;
  color: var(--muted);
}
.legal-links a {
  color: var(--accent);
  text-decoration: none;
  cursor: pointer;
  margin: 0 0.25rem;
}
.legal-links a:hover { text-decoration: underline; }
</style>
