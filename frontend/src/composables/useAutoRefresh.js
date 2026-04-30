import { onBeforeUnmount, onMounted } from 'vue';

/**
 * Run `tick()` every `intervalMs` while the document is visible.
 * Each tick is a no-op when `shouldSkip()` returns truthy (use this to gate on
 * in-flight fetches, open modals, the wrong view, etc.). Brings the next tick
 * forward when the tab becomes visible again.
 */
export const useAutoRefresh = (tick, { intervalMs = 5000, shouldSkip = () => false } = {}) => {
  let timer = null;
  const run = () => {
    if (typeof document !== 'undefined' && document.hidden) return;
    if (shouldSkip()) return;
    tick();
  };
  const onVisible = () => { if (!document.hidden) run(); };

  onMounted(() => {
    timer = setInterval(run, intervalMs);
    document.addEventListener('visibilitychange', onVisible);
  });
  onBeforeUnmount(() => {
    if (timer) clearInterval(timer);
    timer = null;
    document.removeEventListener('visibilitychange', onVisible);
  });
};
