<script setup>
import { nextTick, onBeforeUnmount, ref, watch } from 'vue';
import { markdownHtml } from '../markdown.js';

const props = defineProps({
  messages: { type: Array, required: true },
  busy: { type: Boolean, required: true },
});
const emit = defineEmits(['send', 'grant', 'dismiss-grant']);

const resourceLabel = (rt) => {
  if (rt === 'note') return 'note';
  if (rt === 'task') return 'task';
  if (rt === 'calendar_event') return 'calendar event';
  return 'resource';
};

const input = ref('');
const surface = ref(null);

const voiceListening = ref(false);
const voiceTranscribing = ref(false);
const voiceError = ref('');

/** @type {MediaStream | null} */
let mediaStream = null;
/** @type {MediaRecorder | null} */
let mediaRecorder = null;
/** @type {BlobPart[]} */
let recordedChunks = [];
let voiceTeardown = false;

const scrollDown = () => {
  if (surface.value) surface.value.scrollTop = surface.value.scrollHeight;
};

watch(
  () => props.messages.length,
  () => nextTick(scrollDown),
);

const send = () => {
  const text = input.value.trim();
  if (!text || props.busy) return;
  input.value = '';
  emit('send', text);
};

const displayRole = (role) => (role === 'assistant' ? 'sidekick' : role);

function pickRecorderMime() {
  const candidates = ['audio/webm;codecs=opus', 'audio/webm'];
  for (const m of candidates) {
    if (typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported(m)) {
      return m;
    }
  }
  return '';
}

function stopMediaTracks() {
  if (mediaStream) {
    for (const t of mediaStream.getTracks()) {
      t.stop();
    }
    mediaStream = null;
  }
}

async function toggleVoice() {
  voiceError.value = '';
  if (props.busy || voiceTranscribing.value) return;

  if (voiceListening.value) {
    if (mediaRecorder && mediaRecorder.state === 'recording') {
      mediaRecorder.stop();
    }
    voiceListening.value = false;
    return;
  }

  if (typeof MediaRecorder === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
    voiceError.value = 'Voice input is not supported in this browser.';
    return;
  }

  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    voiceError.value = 'Microphone permission denied or unavailable.';
    return;
  }

  const mime = pickRecorderMime();
  recordedChunks = [];
  try {
    mediaRecorder = mime
      ? new MediaRecorder(mediaStream, { mimeType: mime })
      : new MediaRecorder(mediaStream);
  } catch {
    stopMediaTracks();
    voiceError.value = 'Could not start audio recorder.';
    return;
  }

  const blobType = mime || mediaRecorder.mimeType || 'audio/webm';

  mediaRecorder.ondataavailable = (e) => {
    if (e.data && e.data.size > 0) recordedChunks.push(e.data);
  };

  mediaRecorder.onstop = async () => {
    voiceListening.value = false;
    stopMediaTracks();
    const rec = mediaRecorder;
    mediaRecorder = null;
    try {
      if (voiceTeardown) {
        recordedChunks = [];
        return;
      }
      const blob = new Blob(recordedChunks, { type: blobType });
      recordedChunks = [];
      if (blob.size < 32) {
        return;
      }
      voiceTranscribing.value = true;
      const fd = new FormData();
      fd.append('audio', blob, 'recording.webm');
      try {
        const res = await fetch('/ui-api/speech/transcribe', {
          method: 'POST',
          body: fd,
          credentials: 'same-origin',
        });
        const raw = await res.text();
        if (res.status === 401) {
          voiceError.value = 'Session expired — please sign in again.';
          return;
        }
        let data = {};
        try {
          data = JSON.parse(raw);
        } catch {
          /* non-JSON error body */
        }
        if (!res.ok) {
          voiceError.value = data.detail || data.error || `Transcription failed (${res.status}).`;
          return;
        }
        const t = (data.text || '').trim();
        if (t) {
          const cur = input.value;
          input.value = cur ? `${cur.endsWith(' ') ? cur : `${cur} `}${t}` : t;
        }
      } catch (e) {
        console.error(e);
        voiceError.value = 'Network error while transcribing.';
      }
    } finally {
      voiceTranscribing.value = false;
      if (rec) {
        rec.ondataavailable = null;
        rec.onstop = null;
      }
    }
  };

  mediaRecorder.start();
  voiceListening.value = true;
}

onBeforeUnmount(() => {
  voiceTeardown = true;
  try {
    if (mediaRecorder && mediaRecorder.state === 'recording') {
      mediaRecorder.stop();
    }
  } catch {
    /* ignore */
  }
  stopMediaTracks();
  mediaRecorder = null;
  recordedChunks = [];
});
</script>

<template>
  <div class="conversation">
    <div class="chat-surface" ref="surface">
      <div class="chat-inner">
        <template v-for="(m, i) in messages" :key="i">
          <div
            v-if="m.kind === 'grant-prompt'"
            class="msg system grant-msg"
          >
            <div class="grant-card" :class="`is-${m.resolved || 'pending'}`">
              <div class="grant-head">Grant access?</div>
              <p class="grant-body">
                <template v-if="m.denial.home_chat_is_orphan">
                  This {{ resourceLabel(m.denial.resource_type) }} isn't bound to any chat
                  (it lives only in the central view). Grant this chat permission to act on it?
                </template>
                <template v-else-if="m.denial.home_chat_title">
                  This {{ resourceLabel(m.denial.resource_type) }} lives in the chat
                  <strong>“{{ m.denial.home_chat_title }}”</strong>.
                  Grant this chat permission to act on it?
                </template>
                <template v-else>
                  This {{ resourceLabel(m.denial.resource_type) }} lives in another chat.
                  Grant this chat permission to act on it?
                </template>
              </p>
              <div v-if="m.resolved === 'granted'" class="grant-status grant-status-ok">
                ✓ Access granted. Retrying…
              </div>
              <div v-else-if="m.resolved === 'dismissed'" class="grant-status">
                Cancelled.
              </div>
              <div v-else-if="m.resolved === 'error'" class="grant-status grant-status-err">
                {{ m.errorText || 'Failed to grant access.' }}
              </div>
              <div v-else class="grant-actions">
                <button
                  class="btn btn-primary"
                  @click="emit('grant', { index: i, denial: m.denial })"
                >
                  Grant access
                </button>
                <button
                  class="btn"
                  @click="emit('dismiss-grant', i)"
                >
                  Cancel
                </button>
              </div>
            </div>
          </div>
          <div v-else class="msg" :class="m.role">
            <div class="bubble">
              <div class="role" v-if="m.role !== 'user'">{{ displayRole(m.role) }}</div>
              <div v-html="markdownHtml(m.text)"></div>
            </div>
          </div>
        </template>
        <div v-if="busy" class="msg assistant">
          <div class="bubble">
            <div class="role">{{ displayRole('assistant') }}</div>
            <div class="typing"><span></span><span></span><span></span></div>
          </div>
        </div>
        <div v-if="!messages.length && !busy" class="empty-state">
          Start the conversation — ask about tasks, your schedule, or save a note.
        </div>
      </div>
    </div>

    <div class="composer-wrap">
      <div class="composer-stack">
        <div class="composer">
          <textarea
            v-model="input"
            @keydown.enter.exact.prevent="send"
            @keydown.enter.shift.exact.stop
            placeholder="Message Sidekick…"
            rows="1"
          />
          <button
            type="button"
            class="voice-btn"
            :class="{ active: voiceListening, working: voiceTranscribing }"
            :disabled="busy || voiceTranscribing"
            :aria-pressed="voiceListening"
            :aria-label="voiceListening ? 'Stop recording' : 'Start voice input'"
            :title="voiceListening ? 'Stop and transcribe' : 'Voice input'"
            @click="toggleVoice"
          >
            <span v-if="voiceTranscribing" class="voice-btn-label">…</span>
            <svg
              v-else
              class="voice-icon"
              width="20"
              height="20"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
            >
              <path d="M12 14a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3Z" />
              <path d="M19 10v1a7 7 0 0 1-14 0v-1" />
              <line x1="12" x2="12" y1="19" y2="22" />
            </svg>
          </button>
          <button class="send-btn" :disabled="busy || !input.trim()" @click="send">Send</button>
        </div>
        <p v-if="voiceError" class="voice-err" role="status">{{ voiceError }}</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.conversation {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.chat-surface {
  flex: 1;
  overflow-y: auto;
  padding: 1.25rem 1rem 7rem;
  display: flex;
  flex-direction: column;
  align-items: center;
}
.chat-inner {
  width: 100%;
  max-width: 46rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.msg { display: flex; gap: 0.75rem; align-items: flex-start; }
.msg .bubble {
  padding: 0.7rem 0.95rem;
  border-radius: 14px;
  max-width: 100%;
  font-size: 0.95rem;
  line-height: 1.55;
  box-shadow: var(--shadow);
  border: 1px solid var(--border);
}
.msg.user { justify-content: flex-end; }
.msg.user .bubble {
  background: var(--user-bubble);
  border-color: transparent;
  box-shadow: none;
}
.msg.assistant .bubble { background: var(--bot-bubble); }
.msg .role {
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted);
  margin-bottom: 0.25rem;
}
.msg.error .bubble {
  background: var(--error-bg);
  border-color: var(--error-border);
  color: var(--error-text);
}
.msg.system .bubble {
  background: var(--warn-bg);
  border-color: var(--warn-border);
  color: var(--warn-text);
  font-size: 0.85rem;
}
.typing { display: inline-flex; gap: 0.18rem; padding: 0.3rem 0; }
.typing span {
  width: 0.42rem;
  height: 0.42rem;
  border-radius: 50%;
  background: var(--muted);
  opacity: 0.6;
  animation: blink 1s infinite ease-in-out both;
}
.typing span:nth-child(2) { animation-delay: 0.15s; }
.typing span:nth-child(3) { animation-delay: 0.3s; }
@keyframes blink {
  0%, 80%, 100% { opacity: 0.2; transform: translateY(0); }
  40%           { opacity: 1; transform: translateY(-2px); }
}
.composer-wrap {
  position: sticky;
  bottom: 0;
  width: 100%;
  display: flex;
  justify-content: center;
  padding: 0.75rem 1rem 1rem;
  background: linear-gradient(to bottom, transparent, var(--bg) 30%);
}
.composer-stack {
  width: 100%;
  max-width: 46rem;
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}
.composer {
  width: 100%;
  display: flex;
  gap: 0.5rem;
  align-items: flex-end;
  padding: 0.6rem;
  background: var(--surface);
  border: 1px solid var(--border-strong);
  border-radius: 14px;
  box-shadow: var(--shadow);
}
.voice-btn {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 2.5rem;
  height: 2.5rem;
  padding: 0;
  border-radius: 10px;
  border: 1px solid var(--border-strong);
  background: var(--surface);
  color: var(--muted);
  cursor: pointer;
}
.voice-btn:hover:not(:disabled) {
  color: var(--text);
  border-color: var(--border);
}
.voice-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.voice-btn.active {
  color: var(--accent);
  border-color: var(--accent);
  box-shadow: 0 0 0 1px var(--accent);
}
.voice-btn.working {
  color: var(--accent);
  font-weight: 700;
  font-size: 1rem;
}
.voice-icon { display: block; }
.voice-btn-label { line-height: 1; }
.voice-err {
  margin: 0;
  font-size: 0.8rem;
  color: var(--error-text);
  padding: 0 0.25rem;
}
.composer textarea {
  flex: 1;
  resize: none;
  border: none;
  outline: none;
  background: transparent;
  color: var(--text);
  font: inherit;
  font-size: 0.95rem;
  line-height: 1.4;
  max-height: 10rem;
  min-height: 1.4rem;
  padding: 0.4rem 0.5rem;
}
.send-btn {
  background: var(--accent);
  color: var(--on-accent);
  border: none;
  border-radius: 10px;
  padding: 0.5rem 0.85rem;
  font-weight: 600;
}
.send-btn:hover:not(:disabled) { background: var(--accent-hover); }
.send-btn:disabled { opacity: 0.5; cursor: not-allowed; }

.grant-msg { justify-content: stretch; }
.grant-card {
  width: 100%;
  border: 1px solid var(--accent);
  background: var(--surface);
  border-radius: 14px;
  padding: 0.85rem 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  box-shadow: var(--shadow);
}
.grant-card.is-granted { border-color: var(--border); opacity: 0.85; }
.grant-card.is-dismissed { border-color: var(--border); opacity: 0.7; }
.grant-card.is-error { border-color: var(--error-border); }
.grant-head {
  font-weight: 600;
  font-size: 0.95rem;
  color: var(--text);
}
.grant-body {
  margin: 0;
  font-size: 0.9rem;
  color: var(--text);
  line-height: 1.45;
}
.grant-actions {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}
.grant-status {
  font-size: 0.85rem;
  color: var(--muted);
}
.grant-status-ok { color: var(--accent); }
.grant-status-err { color: var(--error-text); }
</style>
