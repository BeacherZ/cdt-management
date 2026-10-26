import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../app/static',
    emptyOutDir: true,
    sourcemap: false,
    target: 'es2022',
    cssCodeSplit: true,
  },
  server: {
    port: 5273,
    proxy: {
      '/api': 'http://127.0.0.1:8080',
    },
  },
})
