import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const apiPrefix = env.VITE_API_URL?.trim().replace(/\/+$/, '')
  const proxyTarget = env.API_PROXY_TARGET?.trim()
  const proxy = apiPrefix?.startsWith('/') && proxyTarget
    ? {
        [apiPrefix]: {
          target: proxyTarget,
          changeOrigin: true,
          rewrite: (path: string) => path.slice(apiPrefix.length) || '/',
        },
      }
    : undefined

  return {
    plugins: [react()],
    // Keep MapLibre's sibling module-worker URL intact in development.
    optimizeDeps: { exclude: ['maplibre-gl'] },
    server: { proxy },
    preview: { proxy },
  }
})
