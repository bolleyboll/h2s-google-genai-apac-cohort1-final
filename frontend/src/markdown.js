import { marked } from 'marked';
import DOMPurify from 'dompurify';

const escapeHtml = (str) =>
  String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

/**
 * Render markdown to sanitized HTML safe for `v-html`.
 * Falls back to escape+<br> if marked isn't available (defence-in-depth).
 */
export const markdownHtml = (text) => {
  const s = text == null ? '' : String(text);
  if (!s.trim()) return '';
  let raw;
  try {
    raw = marked.parse(s, { breaks: true, gfm: true });
  } catch {
    raw = '<p>' + escapeHtml(s).replace(/\n/g, '<br>') + '</p>';
  }
  return DOMPurify.sanitize(raw, { USE_PROFILES: { html: true } });
};
