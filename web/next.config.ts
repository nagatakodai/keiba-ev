import type { NextConfig } from "next";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

// /api/* は app/api/[...path]/route.ts (Node runtime) でプロキシしている。
// rewrites では HTTP ヘッダを付与できないため、共有キー (X-API-Key) を server-side で
// 付与する必要があり、Route Handler 側に寄せている。
//
// 上流 (FastAPI) の URL は API_TARGET env で Route Handler に渡る (デフォ http://localhost:8788)。

// keiba-ev/ の root には MCP 用の package-lock.json があり、Turbopack が
// それを workspace root と誤検出する。`turbopack.root` で web/ を明示。
const __dirname = dirname(fileURLToPath(import.meta.url));

const nextConfig: NextConfig = {
  turbopack: {
    root: __dirname,
  },
};

export default nextConfig;
