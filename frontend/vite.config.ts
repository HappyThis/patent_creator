import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.indexOf('node_modules') === -1) {
            return undefined;
          }
          if (
            id.indexOf('react-dom') !== -1 ||
            id.indexOf('node_modules/react') !== -1 ||
            id.indexOf('node_modules/scheduler') !== -1
          ) {
            return 'vendor-react';
          }
          if (id.indexOf('katex') !== -1) {
            return 'vendor-katex';
          }
          return undefined;
        },
      },
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
  },
});
