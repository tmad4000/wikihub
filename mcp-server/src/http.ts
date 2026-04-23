#!/usr/bin/env node
/**
 * WikiHub MCP Server — Streamable HTTP entrypoint.
 *
 * Exposes the same tool set as `src/index.ts` but over the Streamable HTTP
 * transport that ChatGPT's MCP Connector (Deep Research / company knowledge)
 * and Claude Code's HTTP connector expect. The stdio server is unchanged —
 * this is a second, parallel process.
 *
 * Auth model: per-request api-key pickup. Each incoming request reads its
 * own key off the headers (or the `?key=` fallback) and builds a fresh
 * `McpServer` scoped to that key, so two concurrent users cannot leak api
 * keys into each other's sessions. No shared-state api key is stored
 * anywhere in this process.
 *
 * Env vars:
 *   PORT              HTTP port to bind (default 4200)
 *   HOST              Interface to bind (default 0.0.0.0)
 *   WIKIHUB_API_URL   Upstream WikiHub URL (default https://wikihub.md)
 *   WIKIHUB_API_KEY   Optional fallback key used when a client does not send
 *                     credentials. Leave unset in multi-tenant deployments
 *                     so anonymous reads stay read-only.
 *
 * Endpoints:
 *   POST /mcp         Streamable HTTP MCP transport (JSON-RPC in, SSE or
 *                     JSON out). Stateless mode: every request spins up a
 *                     fresh transport + server.
 *   GET  /healthz     Liveness probe. Returns {status:"ok"}.
 *
 * Client usage (Claude Code):
 *   claude mcp add -s user wikihub --transport http \\
 *     --header "Authorization: Bearer wh_yourkey" \\
 *     https://mcp.wikihub.md/mcp
 *
 * Client usage (ChatGPT Deep Research):
 *   Settings → Connectors → Custom → new connector:
 *     URL:  https://mcp.wikihub.md/mcp
 *     Auth: Authorization: Bearer wh_yourkey  (custom header)
 */

import express, { type Request, type Response } from 'express';
import { randomUUID } from 'node:crypto';

import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';

import { createApi, getConfig } from './api.js';
import { buildPersonalizedInstructions } from './instructions.js';
import { buildServer, VERSION } from './server.js';

function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const t = setTimeout(() => reject(new Error(`timed out after ${ms}ms`)), ms);
    p.then(
      (v) => {
        clearTimeout(t);
        resolve(v);
      },
      (e) => {
        clearTimeout(t);
        reject(e);
      }
    );
  });
}

const DEFAULT_PORT = 4200;
const DEFAULT_HOST = '0.0.0.0';
const MAX_BODY_BYTES = '1mb';

function pickApiKey(req: Request): string | undefined {
  // 1. Authorization: Bearer <key> — the primary WikiHub auth shape (matches
  //    the REST API). Works for Claude Code's --header "Authorization: Bearer ..."
  //    and any OAuth-shaped client that pastes a bearer token.
  const authz = req.header('authorization');
  if (authz && /^bearer\s+/i.test(authz)) {
    return authz.replace(/^bearer\s+/i, '').trim();
  }

  // 2. x-api-key — convenience header for clients that let users configure
  //    arbitrary headers without the Bearer prefix.
  const header = req.header('x-api-key');
  if (header && header.trim()) return header.trim();

  // 3. ?key=<apikey> query param — pragmatic workaround for Claude Desktop,
  //    whose custom-connector UI (early 2026) accepts OAuth client id/secret
  //    only and has no field for arbitrary headers. Per-user, revocable keys
  //    make this acceptable for friends/beta; drop once OAuth ships.
  const rawQueryKey = req.query.key;
  const queryKey = typeof rawQueryKey === 'string' ? rawQueryKey.trim() : undefined;
  if (queryKey) return queryKey;

  // 4. Last resort: process-wide fallback (useful for local dev / single-tenant).
  return process.env.WIKIHUB_API_KEY || undefined;
}

async function handleMcpRequest(req: Request, res: Response): Promise<void> {
  const reqId = randomUUID();
  const baseUrl = (process.env.WIKIHUB_API_URL || 'https://wikihub.md').replace(/\/+$/, '');
  const apiKey = pickApiKey(req);

  // Build a throw-away MCP server + transport for this single request. This
  // gives us hermetic per-request auth isolation: the api client is captured
  // in the closure of this McpServer, so even if two requests arrive at the
  // same millisecond each sees only its own key.
  const config = { baseUrl, apiKey };
  const probeApi = createApi(config);
  const instructions = await withTimeout(
    buildPersonalizedInstructions(probeApi),
    3000
  ).catch(() => undefined);
  const server = buildServer(config, { instructions });
  const transport = new StreamableHTTPServerTransport({
    sessionIdGenerator: undefined, // stateless
    enableJsonResponse: true,       // let curl-style clients read JSON directly
  });

  res.on('close', () => {
    void transport.close().catch(() => {});
    void server.close().catch(() => {});
  });

  try {
    await server.connect(transport);
    await transport.handleRequest(req, res, req.body);
  } catch (err) {
    console.error(`[wikihub-mcp-http] ${reqId} handler error:`, err);
    if (!res.headersSent) {
      res.status(500).json({
        jsonrpc: '2.0',
        error: {
          code: -32603,
          message: 'Internal error',
          data: err instanceof Error ? err.message : String(err),
        },
        id: null,
      });
    }
  }
}

function main(): void {
  const port = Number(process.env.PORT) || DEFAULT_PORT;
  const host = process.env.HOST || DEFAULT_HOST;
  const { baseUrl, apiKey: envKey } = getConfig();

  const app = express();
  app.disable('x-powered-by');
  app.use(express.json({ limit: MAX_BODY_BYTES }));

  app.get('/healthz', (_req, res) => {
    res.json({ status: 'ok', version: VERSION });
  });

  app.get('/', (_req, res) => {
    res
      .type('text/plain')
      .send(
        `WikiHub MCP Server (HTTP) v${VERSION}\n` +
          `MCP endpoint: POST /mcp  (Streamable HTTP transport)\n` +
          `Auth: Authorization: Bearer <wikihub api key>\n`
      );
  });

  app.post('/mcp', handleMcpRequest);
  app.get('/mcp', handleMcpRequest);
  app.delete('/mcp', handleMcpRequest);

  app.listen(port, host, () => {
    console.error(
      `[wikihub-mcp-http] v${VERSION} listening on http://${host}:${port}/mcp → ${baseUrl}` +
        (envKey ? ' (env fallback api key set)' : ' (no env fallback; clients must authenticate)')
    );
  });
}

main();
