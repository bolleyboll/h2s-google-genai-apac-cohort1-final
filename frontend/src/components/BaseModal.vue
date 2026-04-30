<script setup>
defineProps({
  title: { type: String, required: true },
  errorText: { type: String, default: '' },
});
defineEmits(['close']);
</script>

<template>
  <div class="modal-mask" @click.self="$emit('close')">
    <div class="modal" role="dialog" aria-modal="true">
      <h2>{{ title }}</h2>
      <slot />
      <div v-if="errorText" class="modal-error">{{ errorText }}</div>
      <div class="modal-actions">
        <slot name="actions" />
      </div>
    </div>
  </div>
</template>

<style>
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 17, 22, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
  padding: 1rem;
}
.modal {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 1.25rem;
  width: 100%;
  max-width: 28rem;
  box-shadow: 0 20px 50px rgba(0, 0, 0, 0.25);
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
}
.modal h2 {
  margin: 0;
  font-size: 1rem;
  font-weight: 600;
}
.modal label {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  font-size: 0.83rem;
  color: var(--muted);
}
.modal input,
.modal textarea,
.modal select {
  padding: 0.5rem 0.6rem;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  color: var(--text);
  font: inherit;
  font-size: 0.9rem;
}
.modal textarea { min-height: 6rem; resize: vertical; }
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
}
</style>
