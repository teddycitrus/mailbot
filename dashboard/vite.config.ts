import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Relative base so the built bundle works from any mount point, including the
// packaged executable serving it off a temp directory. Nothing loads from a
// CDN, so the app runs with no internet connection.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "dist", emptyOutDir: true, assetsDir: "assets" },
  server: {
    proxy: { "/api": "http://127.0.0.1:8765" },
  },
});
