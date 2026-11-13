import { defineConfig, loadEnv } from "vite";

// All browser API requests stay same-origin through Vite's /api proxy.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const target = (env.BACKEND_API_ORIGIN || "http://127.0.0.1:8000").trim().replace(/\/$/, "");
  const local = /^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/i.test(target);
  const secure = /^https:\/\/[a-z0-9.-]+(:\d+)?$/i.test(target);
  if (!local && !secure) {
    throw new Error("BACKEND_API_ORIGIN must use HTTPS or loopback HTTP.");
  }
  return {
    server: {
      proxy: { "/api": { target, changeOrigin: true, secure: true } },
    },
  };
});
