import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("/node_modules/")) {
            if (/\/(antd|@ant-design|@rc-component|rc-[^/]+)\//.test(id))
              return "ui";
            if (
              /\/(react|react-dom|scheduler|react-router|react-router-dom)\//.test(
                id,
              )
            )
              return "react";
          }
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.TRANSLATION_API_PROXY || "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
