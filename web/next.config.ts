import type { NextConfig } from "next";

// /api/* は app/api/[...path]/route.ts (Node runtime) でプロキシしている。
// rewrites では HTTP ヘッダを付与できないため、共有キー (X-API-Key) を server-side で
// 付与する必要があり、Route Handler 側に寄せている。
//
// 上流 (FastAPI) の URL は API_TARGET env で Route Handler に渡る (デフォ http://localhost:8787)。

const nextConfig: NextConfig = {};

export default nextConfig;
