import { ref } from 'vue';
import { apiGet } from '../api.js';

/** Deep equality check using JSON serialization */
const deepEqual = (a, b) => JSON.stringify(a) === JSON.stringify(b);

export const useTelemetry = () => {
  const runs = ref([]);
  const loading = ref(false);
  const error = ref('');

  const load = async (chatId, { limit = 10 } = {}) => {
    error.value = '';
    if (!chatId) {
      runs.value = [];
      return;
    }
    loading.value = true;
    try {
      const data = await apiGet(`/ui-api/chats/${chatId}/telemetry?limit=${limit}`);
      const newRuns = Array.isArray(data?.runs) ? data.runs : [];
      if (!deepEqual(runs.value, newRuns)) {
        runs.value = newRuns;
      }
    } catch (e) {
      error.value = e?.message || String(e);
    } finally {
      loading.value = false;
    }
  };

  return { runs, loading, error, load };
};