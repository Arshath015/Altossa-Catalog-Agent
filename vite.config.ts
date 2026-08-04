import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Minimal Vite setup: serves the React frontend and proxies API/image
// requests to the Express backend (App/server/index.ts) running on :3000,
// so the browser can call /api/catalog/chat and load /data/... images
// without any CORS configuration needed.
export default defineConfig({
  plugins: [react()],
  root: 'App',
  server: {
    proxy: {
      '/api': 'http://localhost:3000',
      '/data': 'http://localhost:3000',
    },
  },
});