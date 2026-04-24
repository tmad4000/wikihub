/**
 * Thin wrapper around the WikiHub REST API (/api/v1).
 *
 * Two usage patterns:
 *   1. Module-level singleton (stdio/index.ts): `import { api, pageUrl } from './api.js'`
 *   2. Per-request factory (http.ts → server.ts): `const client = createApi(config)`
 *
 * Configuration via env:
 *   WIKIHUB_API_URL   (default: https://wikihub.md)
 *   WIKIHUB_API_KEY   required for writes and private reads. Keys start with `wh_`.
 *
 * WikiHub auth is `Authorization: Bearer wh_...`. We accept either the bare key
 * (with or without the `wh_` prefix — we pass it through verbatim) and always
 * emit the Bearer scheme on the wire.
 *
 * Cloudflare gotcha: the WikiHub origin sits behind Cloudflare, which blocks
 * some non-curl user agents. We default to `curl/8.0` unless the caller
 * overrides. See docs/MCP_CONNECTOR_BLUEPRINT.md.
 */

const DEFAULT_BASE_URL = 'https://wikihub.md';
const DEFAULT_USER_AGENT = 'curl/8.0';

export interface WikihubConfig {
  baseUrl: string;
  apiKey?: string;
  userAgent?: string;
  /**
   * Provenance headers — land in ApiKey.agent_name / agent_version at key
   * lookup time on the server. Leave defaults for MCP traffic.
   */
  agentName?: string;
  agentVersion?: string;
}

export function getConfig(): WikihubConfig {
  const baseUrl = (process.env.WIKIHUB_API_URL || DEFAULT_BASE_URL).replace(/\/+$/, '');
  const apiKey = process.env.WIKIHUB_API_KEY;
  return { baseUrl, apiKey };
}

export interface WikihubPageSummary {
  path: string;
  title?: string;
  visibility?: string;
  updated_at?: string;
  [k: string]: unknown;
}

export interface WikihubPage extends WikihubPageSummary {
  id?: number | string;
  content?: string;
  content_html?: string;
  anonymous?: boolean;
  claimable?: boolean;
  author?: string | null;
  wiki?: { owner: string; slug: string };
  url?: string;
}

export interface WikihubWikiSummary {
  id?: number | string;
  owner: string;
  slug: string;
  title?: string;
  description?: string;
  page_count?: number;
  updated_at?: string;
  url?: string;
  [k: string]: unknown;
}

export interface WikihubSearchHit {
  owner?: string;
  slug?: string;
  path?: string;
  title?: string;
  excerpt?: string;
  snippet?: string;
  url?: string;
  score?: number;
  [k: string]: unknown;
}

export class WikihubApiError extends Error {
  constructor(public status: number, public body: string, public url: string) {
    super(`WikiHub API ${status} on ${url}: ${body.slice(0, 200)}`);
    this.name = 'WikihubApiError';
  }
}

/** URL for a page in the web UI. */
export function pageUrl(owner: string, slug: string, path: string, baseUrl?: string): string {
  const base = (baseUrl ?? getConfig().baseUrl).replace(/\/+$/, '');
  // strip trailing .md when building human-facing URLs (matches app.url_utils.url_path_from_page_path)
  const cleanPath = path.replace(/\.md$/i, '');
  return `${base}/@${encodeURIComponent(owner)}/${encodeURIComponent(slug)}/${cleanPath.split('/').map(encodeURIComponent).join('/')}`;
}

/** URL for a wiki's landing page. */
export function wikiUrl(owner: string, slug: string, baseUrl?: string): string {
  const base = (baseUrl ?? getConfig().baseUrl).replace(/\/+$/, '');
  return `${base}/@${encodeURIComponent(owner)}/${encodeURIComponent(slug)}`;
}

/**
 * Encode a relative page path for use in a URL. WikiHub routes use
 * `<path:page_path>`, so slashes must be preserved — we only encode segments.
 */
function encodePagePath(path: string): string {
  return path
    .replace(/^\/+/, '')
    .split('/')
    .map(encodeURIComponent)
    .join('/');
}

// ---------- factory ----------

function makeRequest(config: WikihubConfig) {
  return async function request<T = unknown>(
    method: string,
    path: string,
    opts: {
      query?: Record<string, string | number | boolean | undefined>;
      body?: unknown;
    } = {}
  ): Promise<T> {
    const url = new URL(config.baseUrl + path);
    if (opts.query) {
      for (const [k, v] of Object.entries(opts.query)) {
        if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, String(v));
      }
    }
    const headers: Record<string, string> = {
      Accept: 'application/json',
      'User-Agent': config.userAgent || DEFAULT_USER_AGENT,
    };
    if (config.apiKey) headers['Authorization'] = `Bearer ${config.apiKey}`;
    if (config.agentName) headers['X-Agent-Name'] = config.agentName;
    if (config.agentVersion) headers['X-Agent-Version'] = config.agentVersion;
    if (opts.body !== undefined) headers['Content-Type'] = 'application/json';

    const res = await fetch(url.toString(), {
      method,
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
    });

    const text = await res.text();
    if (!res.ok) throw new WikihubApiError(res.status, text, url.toString());
    if (!text) return undefined as T;
    try {
      return JSON.parse(text) as T;
    } catch {
      return text as unknown as T;
    }
  };
}

function buildApiMethods(request: ReturnType<typeof makeRequest>) {
  return {
    // ---- identity / account ----
    whoami: () =>
      request<{
        user_id: number;
        username: string;
        display_name?: string;
        email?: string;
        created_at?: string;
      }>('GET', '/api/v1/accounts/me'),

    /**
     * Self-register an account. Returns a usable api_key immediately.
     * No email verification required for basic use (see AGENTS.md §1).
     */
    registerAgent: (body: { username?: string; display_name?: string; email?: string; password?: string }) =>
      request<{
        user_id: number;
        username: string;
        api_key: string;
        client_config?: Record<string, unknown>;
      }>('POST', '/api/v1/accounts', { body }),

    // ---- search ----
    search: (q: string, limit?: number) =>
      request<{ results?: WikihubSearchHit[]; hits?: WikihubSearchHit[]; total?: number }>(
        'GET',
        '/api/v1/search',
        { query: { q, limit } }
      ),

    // ---- wikis ----
    listWikis: (owner?: string) => {
      // There is no dedicated `/api/v1/wikis` list endpoint; wikis are discoverable
      // via each owner page's HTML or per-wiki API. For now expose a best-effort
      // call that falls back to `whoami` + owner-wiki probing. If the owner is
      // omitted, we can't list — caller gets an empty result with a hint.
      if (!owner) {
        return Promise.resolve({ wikis: [] as WikihubWikiSummary[], note: 'owner required' });
      }
      // Use the public web surface as the list source: /@owner returns HTML,
      // but /@owner/llms.txt is machine-friendly if it exists. For MVP we
      // expose the raw URL so the caller can fetch it themselves.
      return Promise.resolve({
        wikis: [] as WikihubWikiSummary[],
        note: `List-wikis-per-owner is not yet exposed as JSON; visit /@${owner} or /@${owner}/<slug>/llms.txt for a specific wiki.`,
      });
    },

    getWiki: (owner: string, slug: string) =>
      request<WikihubWikiSummary>(
        'GET',
        `/api/v1/wikis/${encodeURIComponent(owner)}/${encodeURIComponent(slug)}`
      ),

    createWiki: (body: {
      slug: string;
      title?: string;
      description?: string;
      template?: 'freeform' | 'structured';
    }) => request<WikihubWikiSummary>('POST', '/api/v1/wikis', { body }),

    forkWiki: (owner: string, slug: string) =>
      request<WikihubWikiSummary>(
        'POST',
        `/api/v1/wikis/${encodeURIComponent(owner)}/${encodeURIComponent(slug)}/fork`
      ),

    getWikiHistory: (owner: string, slug: string, limit?: number) =>
      request<{ commits?: Array<Record<string, unknown>>; history?: Array<Record<string, unknown>> }>(
        'GET',
        `/api/v1/wikis/${encodeURIComponent(owner)}/${encodeURIComponent(slug)}/history`,
        { query: { limit } }
      ),

    listGrants: (owner: string, slug: string) =>
      request<{ grants?: Array<Record<string, unknown>> }>(
        'GET',
        `/api/v1/wikis/${encodeURIComponent(owner)}/${encodeURIComponent(slug)}/grants`
      ),

    sharedWithMe: () =>
      request<{ wikis?: WikihubWikiSummary[]; pages?: Array<Record<string, unknown>> }>(
        'GET',
        '/api/v1/shared-with-me'
      ),

    // ---- pages ----
    listPages: (owner: string, slug: string) =>
      request<{ pages: WikihubPageSummary[]; total?: number }>(
        'GET',
        `/api/v1/wikis/${encodeURIComponent(owner)}/${encodeURIComponent(slug)}/pages`
      ),

    getPage: (owner: string, slug: string, path: string) =>
      request<WikihubPage>(
        'GET',
        `/api/v1/wikis/${encodeURIComponent(owner)}/${encodeURIComponent(slug)}/pages/${encodePagePath(path)}`
      ),

    createPage: (
      owner: string,
      slug: string,
      body: {
        path: string;
        content: string;
        visibility?: 'public' | 'public-edit' | 'private' | 'unlisted';
        anonymous?: boolean;
        claimable?: boolean;
      }
    ) =>
      request<WikihubPage>(
        'POST',
        `/api/v1/wikis/${encodeURIComponent(owner)}/${encodeURIComponent(slug)}/pages`,
        { body }
      ),

    updatePage: (
      owner: string,
      slug: string,
      path: string,
      body: {
        content?: string;
        title?: string;
        visibility?: 'public' | 'public-edit' | 'private' | 'unlisted';
      }
    ) =>
      request<WikihubPage>(
        'PATCH',
        `/api/v1/wikis/${encodeURIComponent(owner)}/${encodeURIComponent(slug)}/pages/${encodePagePath(path)}`,
        { body }
      ),

    deletePage: (owner: string, slug: string, path: string) =>
      request<{ success?: boolean }>(
        'DELETE',
        `/api/v1/wikis/${encodeURIComponent(owner)}/${encodeURIComponent(slug)}/pages/${encodePagePath(path)}`
      ),

    appendSection: (
      owner: string,
      slug: string,
      path: string,
      body: { heading?: string; content: string }
    ) =>
      request<WikihubPage>(
        'POST',
        `/api/v1/wikis/${encodeURIComponent(owner)}/${encodeURIComponent(slug)}/pages/${encodePagePath(path)}/append-section`,
        { body }
      ),

    setVisibility: (
      owner: string,
      slug: string,
      path: string,
      visibility: 'public' | 'public-edit' | 'private' | 'unlisted'
    ) =>
      request<WikihubPage>(
        'POST',
        `/api/v1/wikis/${encodeURIComponent(owner)}/${encodeURIComponent(slug)}/pages/${encodePagePath(path)}/visibility`,
        { body: { visibility } }
      ),

    sharePage: (
      owner: string,
      slug: string,
      path: string,
      body: { user?: string; email?: string; level?: 'read' | 'edit' }
    ) =>
      request<{ success?: boolean }>(
        'POST',
        `/api/v1/wikis/${encodeURIComponent(owner)}/${encodeURIComponent(slug)}/pages/${encodePagePath(path)}/share`,
        { body }
      ),

    shareWiki: (
      owner: string,
      slug: string,
      body: { user?: string; email?: string; pattern?: string; level?: 'read' | 'edit' }
    ) =>
      request<{ success?: boolean }>(
        'POST',
        `/api/v1/wikis/${encodeURIComponent(owner)}/${encodeURIComponent(slug)}/share`,
        { body }
      ),
  };
}

/** API client type — the shape returned by `createApi()`. */
export type WikihubApi = ReturnType<typeof buildApiMethods> & {
  baseUrl: string;
  hasApiKey: boolean;
};

/** Create a per-request API client bound to the given config. */
export function createApi(config: WikihubConfig): WikihubApi {
  const request = makeRequest(config);
  return {
    ...buildApiMethods(request),
    baseUrl: config.baseUrl,
    hasApiKey: Boolean(config.apiKey),
  };
}

/** Module-level singleton for the stdio entrypoint (reads env on each call). */
export const api = buildApiMethods(makeRequest(getConfig()));
