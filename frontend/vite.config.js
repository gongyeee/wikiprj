import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      // Preserve the browser host so Django can validate same-origin CSRF tokens.
      "/api": { target: "http://127.0.0.1:8091", changeOrigin: false }
    }
  }
});
