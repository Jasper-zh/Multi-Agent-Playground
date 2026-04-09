import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

const apiProxyTarget = "http://127.0.0.1:8011";
const desktopBuild = process.env.AGENT_PLAYGROUND_DESKTOP_BUILD === "1";

export default defineConfig({
  base: desktopBuild ? "./" : "/",
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: apiProxyTarget,
        changeOrigin: true,
      },
    },
  },
});
