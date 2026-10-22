import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8865',
        changeOrigin: true,
        configure(proxy) {
          proxy.on('proxyReq', request => request.setHeader('origin', 'http://127.0.0.1:8865'))
        },
      },
    },
  },
})
