import { reactive, ref } from 'vue';
import { apiGet, apiSend, apiDelete, apiRaw } from '../api.js';

/** Deep equality check using JSON serialization */
const deepEqual = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const KINDS = ['notes', 'tasks', 'calendar'];
const empty = () => ({ google_api_enabled: false, items: [], mode: 'all' });

const inv = reactive({
  notes: empty(),
  tasks: empty(),
  calendar: empty(),
});
const invLoading = reactive({ notes: false, tasks: false, calendar: false });
const invError = reactive({ notes: '', tasks: '', calendar: '' });
const resKind = ref('notes');

const buildUrl = (kind, { chatId, scope }) => {
  let url = `/ui-api/inventory/${kind}`;
  if (scope === 'all') url += '?scope=all';
  else if (chatId != null) url += `?chat_id=${encodeURIComponent(chatId)}`;
  return url;
};

const reload = async (kind, opts = {}) => {
  if (!KINDS.includes(kind)) return;
  invLoading[kind] = true;
  invError[kind] = '';
  try {
    const newData = await apiGet(buildUrl(kind, opts));
    if (!deepEqual(inv[kind], newData)) {
      inv[kind] = newData;
    }
  } catch (e) {
    invError[kind] = e.message || String(e);
  } finally {
    invLoading[kind] = false;
  }
};

const reloadAll = (opts) => Promise.all(KINDS.map((k) => reload(k, opts)));

const anyLoading = () => invLoading.notes || invLoading.tasks || invLoading.calendar;

// PATCH and DELETE pick a Google or DB endpoint based on the row's external id.
const editRow = async (kind, row, form) => {
  let url, body;
  if (kind === 'tasks') {
    if (row.google_task_id) {
      const q = new URLSearchParams();
      if (row.google_tasklist_id) q.set('tasklist_id', row.google_tasklist_id);
      url = `/ui-api/google/tasks/${encodeURIComponent(row.google_task_id)}` +
        (q.toString() ? `?${q.toString()}` : '');
      body = {
        title: form.title,
        status: form.status === 'open' ? 'needsAction' : form.status,
        due_rfc3339: form.due_at || null,
      };
    } else {
      url = `/ui-api/db/tasks/${encodeURIComponent(row.id)}`;
      body = { title: form.title, status: form.status, due_at: form.due_at || null };
    }
  } else if (kind === 'calendar') {
    if (row.google_event_id) {
      url = `/ui-api/google/calendar/${encodeURIComponent(row.google_event_id)}`;
      body = {
        summary: form.title,
        start_at: form.start_at,
        end_at: form.end_at,
        description: form.notes,
      };
    } else {
      url = `/ui-api/db/calendar/${encodeURIComponent(row.id)}`;
      body = {
        title: form.title,
        start_at: form.start_at,
        end_at: form.end_at,
        notes: form.notes,
      };
    }
  } else {
    if (row.google_doc_id) {
      url = `/ui-api/google/notes/${encodeURIComponent(row.google_doc_id)}`;
    } else {
      url = `/ui-api/db/notes/${encodeURIComponent(row.id)}`;
    }
    body = { title: form.title, body: form.body };
  }
  await apiSend(url, 'PATCH', body);
};

const deleteRow = async (kind, row) => {
  let url;
  if (kind === 'tasks') {
    if (row.google_task_id) {
      const q = new URLSearchParams();
      if (row.google_tasklist_id) q.set('tasklist_id', row.google_tasklist_id);
      url = `/ui-api/google/tasks/${encodeURIComponent(row.google_task_id)}` +
        (q.toString() ? `?${q.toString()}` : '');
    } else {
      url = `/ui-api/db/tasks/${encodeURIComponent(row.id)}`;
    }
  } else if (kind === 'calendar') {
    url = row.google_event_id
      ? `/ui-api/google/calendar/${encodeURIComponent(row.google_event_id)}`
      : `/ui-api/db/calendar/${encodeURIComponent(row.id)}`;
  } else {
    url = row.google_doc_id
      ? `/ui-api/google/notes/${encodeURIComponent(row.google_doc_id)}`
      : `/ui-api/db/notes/${encodeURIComponent(row.id)}`;
  }
  // apiRaw to swallow non-JSON 200 responses cleanly.
  const res = await apiRaw(url, { method: 'DELETE' });
  if (!res.ok) {
    let detail = '';
    try { detail = await res.text(); } catch {}
    throw new Error(`${res.status} ${detail.slice(0, 200)}`);
  }
};

export const useResources = () => ({
  KINDS,
  inv,
  invLoading,
  invError,
  resKind,
  reload,
  reloadAll,
  anyLoading,
  editRow,
  deleteRow,
});
