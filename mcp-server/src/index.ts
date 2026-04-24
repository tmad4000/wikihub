#!/usr/bin/env node
/**
 * WikiHub MCP Server — stdio entrypoint.
 *
 * Thin wrapper around the shared `buildServer()` factory in `./server.ts`.
 * All tool registrations live there so stdio and HTTP transports expose
 * an identical surface.
 *
 * Transport: stdio (the universal MCP transport for Claude Desktop + Code).
 *
 * Env vars:
 *   WIKIHUB_API_URL   default https://wikihub.md
 *   WIKIHUB_API_KEY   required for writes and private reads (starts `wh_`)
 *
 * Note: stdout is reserved for the MCP protocol. All logging goes to stderr.
 */

import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';

import { createApi, getConfig } from './api.js';
import { buildPersonalizedInstructions } from './instructions.js';
import { buildServer, VERSION } from './server.js';

async function main() {
  const config = getConfig();
  console.error(
    `[wikihub-mcp] v${VERSION} → ${config.baseUrl} ${config.apiKey ? '(authenticated)' : '(anonymous, read-only on public content)'}`
  );
  const probeApi = createApi(config);
  const instructions = await Promise.race([
    buildPersonalizedInstructions(probeApi),
    new Promise<undefined>((resolve) => setTimeout(() => resolve(undefined), 3000)),
  ]).catch(() => undefined);
  const server = buildServer(config, { instructions });
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error('[wikihub-mcp] listening on stdio');
}

main().catch((e) => {
  console.error('[wikihub-mcp] fatal:', e);
  process.exit(1);
});
