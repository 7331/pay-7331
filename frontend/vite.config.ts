import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// Built into dist/, which the API serves at /static/ (backend/app/payments/pages/router.py); every page route answers
// dist/index.html and the app routes on the client. `pnpm dev` proxies /api to a running API.
export default defineConfig({
  base: "/static/",
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "./src") } },
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    proxy: { "/api": { target: process.env.PAY_DEV_API_URL ?? "http://127.0.0.1:18080" } },
  },
})
