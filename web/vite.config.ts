import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    proxy: {
      '/api': 'http://127.0.0.1:7777',
      '/auth': 'http://127.0.0.1:7777',
    }
  },
  build: {
    outDir: 'dist'
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
  }
} as any)
