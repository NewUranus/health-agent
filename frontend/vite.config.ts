/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 构建结果提交到 ../web，由 FastAPI 在 /static 下托管；服务器内存小，不在服务器上构建
export default defineConfig({
  plugins: [react()],
  base: "/static/",
  build: { outDir: "../web", emptyOutDir: true },
  server: { proxy: { "/api": "http://localhost:8000" } },
  test: { environment: "jsdom", setupFiles: "./src/test-setup.ts", restoreMocks: true },
});
