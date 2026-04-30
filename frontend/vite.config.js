import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import path from 'node:path';

// Build output is consumed by Flask under /static/dist/. The dev server
// proxies API/UI-API/auth/login/logout endpoints to Flask on :8080 so HMR
// works without CORS gymnastics.
export default defineConfig({
  plugins: [vue()],
  base: '/static/dist/',
  build: {
    outDir: path.resolve(__dirname, '../static/dist'),
    emptyOutDir: true,
    sourcemap: false,
    cssCodeSplit: false, // single CSS bundle
    rollupOptions: {
      output: {
        // Single JS chunk so the production deploy is one HTML + one JS + one CSS.
        manualChunks: undefined,
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8080',
      '/ui-api': 'http://127.0.0.1:8080',
      '/auth': 'http://127.0.0.1:8080',
      '/login': 'http://127.0.0.1:8080',
      '/logout': 'http://127.0.0.1:8080',
    },
  },
});
