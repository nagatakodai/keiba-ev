import type { NextRequest } from "next/server";

// Proxy all /api/* requests to the upstream FastAPI and inject the shared
// `X-API-Key` header server-side, so the key never reaches the browser.
//
// - Streams request & response bodies (SSE / text/event-stream supported).
// - Strips hop-by-hop headers in both directions.
// - Preserves query string.
//
// Environment:
//   API_TARGET        upstream base (default http://localhost:8788 for `make api`)
//   API_SHARED_KEY    shared X-API-Key value (省略可。ローカル運用なら未設定で OK)

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const API_TARGET = process.env.API_TARGET ?? "http://localhost:8788";
const API_KEY = process.env.API_SHARED_KEY ?? "";

const HOP_BY_HOP = new Set([
  "host",
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
  "content-length",
]);

// Drop these in addition to hop-by-hop: browser-scoped session state that has
// no business reaching the upstream API.
const DROP_REQ_HEADERS = new Set(["cookie"]);

type Ctx = { params: Promise<{ path: string[] }> };

async function proxy(req: NextRequest, ctx: Ctx): Promise<Response> {
  const { path } = await ctx.params;
  const url = `${API_TARGET}/api/${path.join("/")}${req.nextUrl.search}`;

  const headers = new Headers();
  req.headers.forEach((value, key) => {
    const k = key.toLowerCase();
    if (HOP_BY_HOP.has(k) || DROP_REQ_HEADERS.has(k)) return;
    headers.set(key, value);
  });
  if (API_KEY) headers.set("x-api-key", API_KEY);

  const bodyless = req.method === "GET" || req.method === "HEAD";
  // `duplex: "half"` is required by Node's fetch when streaming a ReadableStream body.
  const init: RequestInit & { duplex?: "half" } = {
    method: req.method,
    headers,
    body: bodyless ? null : req.body,
    redirect: "manual",
    cache: "no-store",
    // Propagate client disconnect so the upstream SSE/long-poll connection is
    // torn down instead of leaking until upstream times out.
    signal: req.signal,
  };
  if (!bodyless && req.body) init.duplex = "half";

  let upstream: Response;
  try {
    upstream = await fetch(url, init as RequestInit);
  } catch (err) {
    if ((err as { name?: string })?.name === "AbortError") {
      // Client went away mid-flight — nothing useful to return.
      return new Response(null, { status: 499, statusText: "Client Closed Request" });
    }
    const detail = err instanceof Error ? err.message : String(err);
    return new Response(JSON.stringify({ detail: `upstream fetch failed: ${detail}` }), {
      status: 502,
      headers: { "content-type": "application/json" },
    });
  }

  const respHeaders = new Headers();
  upstream.headers.forEach((value, key) => {
    if (!HOP_BY_HOP.has(key.toLowerCase())) respHeaders.set(key, value);
  });

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: respHeaders,
  });
}

export async function GET(req: NextRequest, ctx: Ctx) { return proxy(req, ctx); }
export async function HEAD(req: NextRequest, ctx: Ctx) { return proxy(req, ctx); }
export async function POST(req: NextRequest, ctx: Ctx) { return proxy(req, ctx); }
export async function PUT(req: NextRequest, ctx: Ctx) { return proxy(req, ctx); }
export async function PATCH(req: NextRequest, ctx: Ctx) { return proxy(req, ctx); }
export async function DELETE(req: NextRequest, ctx: Ctx) { return proxy(req, ctx); }
export async function OPTIONS(req: NextRequest, ctx: Ctx) { return proxy(req, ctx); }
