import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Minimal Vite setup: serves the React frontend and proxies API/image
// requests to the Express backend (App/server/index.ts), so the browser can
// call /api/catalog/chat and load /data/... images without any CORS
// configuration needed. Backend port defaults to 3000 (the server's own
// default) but is overridable via BACKEND_PORT when that port is taken.
const backendPort = process.env.BACKEND_PORT || '3000';
const backendOrigin = `http://localhost:${backendPort}`;

export default defineConfig({
  plugins: [react()],
  root: 'App',
  server: {
    proxy: {
      '/api': backendOrigin,
      '/data': backendOrigin,
    },
  },
});