/**
 * Shared server-construction logic for the WikiHub MCP server.
 *
 * Both entrypoints (`src/index.ts` for stdio, `src/http.ts` for Streamable
 * HTTP) call `buildServer(config)` to get a fully-wired `McpServer` with all
 * tools registered. Each call returns a fresh `McpServer` instance that
 * closes over its own API client — so the HTTP entrypoint can create one
 * server per request and be confident no api keys leak across sessions.
 */

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { z } from 'zod';

import {
  createApi,
  pageUrl,
  wikiUrl,
  WikihubApiError,
  type WikihubApi,
  type WikihubConfig,
  type WikihubPage,
  type WikihubPageSummary,
  type WikihubSearchHit,
} from './api.js';

export const VERSION = '0.1.0';

const VISIBILITY_VALUES = ['public', 'public-edit', 'private', 'unlisted'] as const;

// ---------- formatting helpers ----------

function splitWikiKey(wiki: string | undefined): { owner?: string; slug?: string } {
  if (!wiki) return {};
  const [owner, slug] = wiki.split('/', 2);
  return { owner, slug };
}

function hitLine(h: WikihubSearchHit, baseUrl: string): string {
  const wiki = (h.wiki as string) || (h.owner && h.slug ? `${h.owner}/${h.slug}` : '?');
  const { owner, slug } = splitWikiKey(wiki);
  const path = (h.page as string) || h.path || '';
  const title = h.title || path || '(untitled)';
  const url = owner && slug && path ? pageUrl(owner, slug, path, baseUrl) : baseUrl;
  const excerpt = h.excerpt ? ` — ${String(h.excerpt).slice(0, 140).replace(/\s+/g, ' ')}` : '';
  return `- [${wiki}] ${title} (${path}) ${url}${excerpt}`;
}

function pageLine(p: WikihubPageSummary, owner: string, slug: string, baseUrl: string): string {
  const title = p.title || p.path;
  const vis = p.visibility ? ` [${p.visibility}]` : '';
  return `- ${title} (${p.path})${vis} ${pageUrl(owner, slug, p.path, baseUrl)}`;
}

function textResult(text: string) {
  return { content: [{ type: 'text' as const, text }] };
}

function errorResult(e: unknown) {
  if (e instanceof WikihubApiError) {
    return {
      isError: true,
      content: [
        {
          type: 'text' as const,
          text: `WikiHub API error (${e.status}): ${e.body.slice(0, 500)}\nURL: ${e.url}`,
        },
      ],
    };
  }
  const msg = e instanceof Error ? e.message : String(e);
  return { isError: true, content: [{ type: 'text' as const, text: `Error: ${msg}` }] };
}

function requireApiKey(api: WikihubApi, purpose: string) {
  if (!api.hasApiKey) {
    throw new Error(
      `WIKIHUB_API_KEY is not set. ${purpose} requires authentication. Pass Authorization: Bearer <key> / x-api-key on the request (HTTP transport) or set WIKIHUB_API_KEY in env (stdio).`
    );
  }
}

/**
 * Synthetic hit id used by the ChatGPT `search`/`fetch` alias pair.
 * Encodes owner, slug, and page path into a single opaque string so
 * `fetch(id)` can recover the three without extra state.
 */
function hitId(owner: string, slug: string, path: string): string {
  return `${owner}/${slug}:${path}`;
}

function parseHitId(id: string): { owner: string; slug: string; path: string } | null {
  const m = id.match(/^([^/]+)\/([^:]+):(.+)$/);
  if (!m) return null;
  return { owner: m[1], slug: m[2], path: m[3] };
}

// ---------- builder ----------

/**
 * Build a fresh `McpServer` bound to the given WikiHub config.
 *
 * The returned server has no transport attached — the caller connects it to
 * stdio or HTTP. Each `buildServer(...)` call is fully independent; two
 * concurrent callers do not share api-key state.
 *
 * `instructions` is the optional server-level system prompt surface the MCP
 * spec provides — clients typically inject it into the model's context on
 * connect. Use it to carry per-user personalization.
 */
export function buildServer(
  config: WikihubConfig,
  options: { instructions?: string } = {}
): McpServer {
  // Default provenance so every write is attributed to the MCP connector.
  const api = createApi({
    agentName: 'wikihub-mcp',
    agentVersion: VERSION,
    ...config,
  });
  const baseUrl = api.baseUrl;

  const server = new McpServer(
    {
      name: 'wikihub',
      version: VERSION,
    },
    options.instructions ? { instructions: options.instructions } : {}
  );

  // -- whoami --
  server.registerTool(
    'wikihub_whoami',
    {
      title: 'Who am I?',
      description:
        'Return the caller\'s WikiHub account. Useful as a first call to confirm which user the current api key authenticates as. Requires an api key.',
      inputSchema: {},
    },
    async () => {
      try {
        requireApiKey(api, 'whoami');
        const me = await api.whoami();
        return textResult(
          [
            `Authenticated as @${me.username} (user_id=${me.user_id})`,
            me.display_name ? `name: ${me.display_name}` : '',
            me.email ? `email: ${me.email}` : '(no email on file)',
            me.created_at ? `joined: ${me.created_at}` : '',
          ]
            .filter(Boolean)
            .join('\n')
        );
      } catch (e) {
        return errorResult(e);
      }
    }
  );

  // -- search --
  server.registerTool(
    'wikihub_search',
    {
      title: 'Search WikiHub',
      description:
        'Full-text + fuzzy search across pages. Returns hits with {wiki, page, title, excerpt}. Scope to one wiki with `wiki` ("owner/slug"). Anonymous callers see public + public-edit pages only.',
      inputSchema: {
        query: z.string().min(1).describe('Keyword or phrase to search for'),
        wiki: z
          .string()
          .optional()
          .describe('Scope to "owner/slug" — e.g. "jacobcole/notes"'),
        limit: z.number().int().min(1).max(100).optional().default(20),
      },
    },
    async ({ query, wiki, limit }) => {
      try {
        const resp = await api.search(query, limit);
        const hits: WikihubSearchHit[] =
          (resp.results as WikihubSearchHit[]) || (resp.hits as WikihubSearchHit[]) || [];
        let filtered = hits;
        if (wiki) filtered = hits.filter((h) => (h.wiki as string) === wiki);
        if (!filtered.length)
          return textResult(`No pages matched "${query}"${wiki ? ` in ${wiki}` : ''}.`);
        return textResult(
          `Found ${filtered.length} page(s) for "${query}":\n${filtered
            .map((h) => hitLine(h, baseUrl))
            .join('\n')}`
        );
      } catch (e) {
        return errorResult(e);
      }
    }
  );

  // -- get_page --
  server.registerTool(
    'wikihub_get_page',
    {
      title: 'Read a WikiHub page',
      description:
        'Fetch a single page\'s content and metadata. Respects ACL: private pages require an api key that can read them.',
      inputSchema: {
        owner: z.string().describe('Username of the wiki owner (no leading @)'),
        slug: z.string().describe('Wiki slug (e.g. "notes")'),
        path: z
          .string()
          .describe('Page path within the wiki, e.g. "hello.md" or "folder/sub.md"'),
      },
    },
    async ({ owner, slug, path }) => {
      try {
        const p = await api.getPage(owner, slug, path);
        const lines = [
          `# ${p.title || '(untitled)'}`,
          `wiki: ${owner}/${slug}`,
          `path: ${p.path}`,
          p.visibility ? `visibility: ${p.visibility}` : '',
          p.updated_at ? `updated_at: ${p.updated_at}` : '',
          `url: ${pageUrl(owner, slug, p.path, baseUrl)}`,
          '',
          '## content',
          (p.content as string) || '(no content)',
        ];
        return textResult(lines.filter(Boolean).join('\n'));
      } catch (e) {
        return errorResult(e);
      }
    }
  );

  // -- list_pages --
  server.registerTool(
    'wikihub_list_pages',
    {
      title: 'List pages in a wiki',
      description:
        'List all pages the caller can read inside one wiki. Returns {path, title, visibility, updated_at}.',
      inputSchema: {
        owner: z.string(),
        slug: z.string(),
      },
    },
    async ({ owner, slug }) => {
      try {
        const { pages } = await api.listPages(owner, slug);
        if (!pages?.length) return textResult(`No pages in @${owner}/${slug} (or none readable).`);
        return textResult(
          `${pages.length} page(s) in @${owner}/${slug}:\n${pages
            .map((p) => pageLine(p, owner, slug, baseUrl))
            .join('\n')}`
        );
      } catch (e) {
        return errorResult(e);
      }
    }
  );

  // -- get_wiki --
  server.registerTool(
    'wikihub_get_wiki',
    {
      title: 'Get a wiki',
      description:
        'Fetch metadata about a wiki: title, description, star_count, fork_count, page_count.',
      inputSchema: {
        owner: z.string(),
        slug: z.string(),
      },
    },
    async ({ owner, slug }) => {
      try {
        const w = await api.getWiki(owner, slug);
        return textResult(
          [
            `# ${w.title || w.slug}`,
            `url: ${wikiUrl(owner, slug, baseUrl)}`,
            w.description ? `description: ${w.description}` : '',
            typeof w.page_count === 'number' ? `pages: ${w.page_count}` : '',
            typeof w.star_count === 'number' ? `stars: ${w.star_count}` : '',
            typeof w.fork_count === 'number' ? `forks: ${w.fork_count}` : '',
            w.updated_at ? `updated_at: ${w.updated_at}` : '',
          ]
            .filter(Boolean)
            .join('\n')
        );
      } catch (e) {
        return errorResult(e);
      }
    }
  );

  // -- create_wiki --
  server.registerTool(
    'wikihub_create_wiki',
    {
      title: 'Create a wiki',
      description:
        'Create a new wiki under the authenticated user. Slug must be unique per-owner. Requires an api key.',
      inputSchema: {
        slug: z
          .string()
          .min(1)
          .describe('Short unique name, lowercase/alnum/-/_ — e.g. "notes"'),
        title: z.string().optional(),
        description: z.string().optional(),
        template: z
          .enum(['freeform', 'structured'])
          .optional()
          .describe('Scaffold type (default: structured)'),
      },
    },
    async (args) => {
      try {
        requireApiKey(api, 'Creating a wiki');
        const w = await api.createWiki(args);
        return textResult(
          `Created wiki @${w.owner}/${w.slug}.\nURL: ${wikiUrl(w.owner, w.slug, baseUrl)}`
        );
      } catch (e) {
        return errorResult(e);
      }
    }
  );

  // -- create_page --
  server.registerTool(
    'wikihub_create_page',
    {
      title: 'Create a page',
      description:
        'Create a new page in a wiki. Write access is governed by wiki ACL: owners always, `public-edit` pages allow anyone, otherwise the caller must be explicitly granted. Use `visibility` to override the default inherited visibility.',
      inputSchema: {
        owner: z.string(),
        slug: z.string(),
        path: z
          .string()
          .describe('Page path within the wiki, e.g. "notes/hello.md"'),
        content: z.string().describe('Full markdown content of the page'),
        visibility: z.enum(VISIBILITY_VALUES).optional(),
        anonymous: z
          .boolean()
          .optional()
          .describe(
            'Post anonymously (only valid on public-edit wikis, per AGENTS.md core principle 2)'
          ),
      },
    },
    async ({ owner, slug, ...body }) => {
      try {
        if (!body.anonymous) requireApiKey(api, 'Creating a page as an identified user');
        const p = await api.createPage(owner, slug, body);
        return textResult(
          `Created page "${p.title || p.path}" in @${owner}/${slug}.\nURL: ${pageUrl(
            owner,
            slug,
            p.path,
            baseUrl
          )}`
        );
      } catch (e) {
        return errorResult(e);
      }
    }
  );

  // -- update_page --
  server.registerTool(
    'wikihub_update_page',
    {
      title: 'Update a page',
      description:
        'Patch an existing page\'s content, title, or visibility. Omit fields you don\'t want to change. Requires edit access.',
      inputSchema: {
        owner: z.string(),
        slug: z.string(),
        path: z.string(),
        content: z.string().optional(),
        title: z.string().optional(),
        visibility: z.enum(VISIBILITY_VALUES).optional(),
      },
    },
    async ({ owner, slug, path, ...rest }) => {
      try {
        requireApiKey(api, 'Updating a page');
        const body = Object.fromEntries(
          Object.entries(rest).filter(([, v]) => v !== undefined)
        );
        if (Object.keys(body).length === 0) {
          return textResult('No fields supplied — nothing to update.');
        }
        const p = await api.updatePage(owner, slug, path, body);
        const fields = Object.keys(body).join(', ');
        return textResult(
          `Updated "${p.title || p.path}" in @${owner}/${slug}. Changed: ${fields}.\nURL: ${pageUrl(
            owner,
            slug,
            p.path,
            baseUrl
          )}`
        );
      } catch (e) {
        return errorResult(e);
      }
    }
  );

  // -- append_section --
  server.registerTool(
    'wikihub_append_section',
    {
      title: 'Append a section to a page',
      description:
        'Append markdown to a page, optionally under a new `## heading`. Non-destructive — existing content is preserved. Good for session logs and append-only journals.',
      inputSchema: {
        owner: z.string(),
        slug: z.string(),
        path: z.string(),
        heading: z.string().optional().describe('Optional heading for the new section'),
        content: z.string().describe('Markdown body to append'),
      },
    },
    async ({ owner, slug, path, heading, content }) => {
      try {
        requireApiKey(api, 'Appending to a page');
        const p = await api.appendSection(owner, slug, path, { heading, content });
        return textResult(
          `Appended to "${p.title || p.path}".\nURL: ${pageUrl(owner, slug, p.path, baseUrl)}`
        );
      } catch (e) {
        return errorResult(e);
      }
    }
  );

  // -- delete_page --
  server.registerTool(
    'wikihub_delete_page',
    {
      title: 'Delete a page',
      description:
        'Permanently delete a page you can edit. Prefer setting visibility=private to hide a page you still want to keep.',
      inputSchema: {
        owner: z.string(),
        slug: z.string(),
        path: z.string(),
      },
    },
    async ({ owner, slug, path }) => {
      try {
        requireApiKey(api, 'Deleting a page');
        await api.deletePage(owner, slug, path);
        return textResult(`Deleted @${owner}/${slug}/${path}.`);
      } catch (e) {
        return errorResult(e);
      }
    }
  );

  // -- set_visibility --
  server.registerTool(
    'wikihub_set_visibility',
    {
      title: 'Change a page\'s visibility',
      description:
        'Set a page to public, public-edit, private, or unlisted. Frontmatter visibility wins over ACL; this rewrites the frontmatter accordingly.',
      inputSchema: {
        owner: z.string(),
        slug: z.string(),
        path: z.string(),
        visibility: z.enum(VISIBILITY_VALUES),
      },
    },
    async ({ owner, slug, path, visibility }) => {
      try {
        requireApiKey(api, 'Changing page visibility');
        await api.setVisibility(owner, slug, path, visibility);
        return textResult(`Set @${owner}/${slug}/${path} visibility=${visibility}.`);
      } catch (e) {
        return errorResult(e);
      }
    }
  );

  // -- share --
  server.registerTool(
    'wikihub_share',
    {
      title: 'Share a page or wiki',
      description:
        'Grant read or edit access. Specify exactly one of {user, email}. Provide `path` for a page-level grant, or `pattern` / nothing for a wiki-level grant.',
      inputSchema: {
        owner: z.string(),
        slug: z.string(),
        path: z.string().optional().describe('Page path — if set, grants apply to that page'),
        pattern: z
          .string()
          .optional()
          .describe('Wiki-level pattern (CODEOWNERS-style). Omit for whole-wiki.'),
        user: z.string().optional().describe('WikiHub username'),
        email: z.string().email().optional(),
        level: z.enum(['read', 'edit']).optional().default('read'),
      },
    },
    async ({ owner, slug, path, pattern, user, email, level }) => {
      try {
        requireApiKey(api, 'Sharing');
        if (!user && !email) throw new Error('Pass either `user` or `email`.');
        if (path) {
          await api.sharePage(owner, slug, path, { user, email, level });
          return textResult(`Granted ${level} on @${owner}/${slug}/${path}.`);
        }
        await api.shareWiki(owner, slug, { user, email, pattern, level });
        return textResult(
          `Granted ${level} on @${owner}/${slug}${pattern ? ` (pattern=${pattern})` : ''}.`
        );
      } catch (e) {
        return errorResult(e);
      }
    }
  );

  // -- list_grants --
  server.registerTool(
    'wikihub_list_grants',
    {
      title: 'List sharing grants',
      description: 'Return all current ACL grants on a wiki.',
      inputSchema: {
        owner: z.string(),
        slug: z.string(),
      },
    },
    async ({ owner, slug }) => {
      try {
        requireApiKey(api, 'Listing grants');
        const r = await api.listGrants(owner, slug);
        const grants = r.grants || [];
        if (!grants.length) return textResult(`No grants on @${owner}/${slug}.`);
        return textResult(
          `Grants on @${owner}/${slug}:\n${grants
            .map((g) => `- ${JSON.stringify(g)}`)
            .join('\n')}`
        );
      } catch (e) {
        return errorResult(e);
      }
    }
  );

  // -- shared_with_me --
  server.registerTool(
    'wikihub_shared_with_me',
    {
      title: 'List content shared with me',
      description: 'Return wikis and pages explicitly shared with the authenticated caller.',
      inputSchema: {},
    },
    async () => {
      try {
        requireApiKey(api, 'Listing shared content');
        const r = await api.sharedWithMe();
        const wikis = r.wikis || [];
        const pages = r.pages || [];
        if (!wikis.length && !pages.length) return textResult('Nothing is currently shared with you.');
        const lines: string[] = [];
        if (wikis.length) {
          lines.push(`## ${wikis.length} wiki(s)`);
          for (const w of wikis) {
            lines.push(`- @${w.owner}/${w.slug} — ${wikiUrl(w.owner, w.slug, baseUrl)}`);
          }
        }
        if (pages.length) {
          lines.push('', `## ${pages.length} page(s)`);
          for (const p of pages) {
            const owner = String(p.owner ?? '?');
            const slug = String(p.slug ?? '?');
            const path = String(p.path ?? '');
            lines.push(`- @${owner}/${slug}/${path}`);
          }
        }
        return textResult(lines.join('\n'));
      } catch (e) {
        return errorResult(e);
      }
    }
  );

  // -- fork_wiki --
  server.registerTool(
    'wikihub_fork_wiki',
    {
      title: 'Fork a wiki',
      description: 'Copy a public wiki into your own namespace.',
      inputSchema: {
        owner: z.string(),
        slug: z.string(),
      },
    },
    async ({ owner, slug }) => {
      try {
        requireApiKey(api, 'Forking a wiki');
        const w = await api.forkWiki(owner, slug);
        return textResult(
          `Forked @${owner}/${slug} → @${w.owner}/${w.slug}.\nURL: ${wikiUrl(w.owner, w.slug, baseUrl)}`
        );
      } catch (e) {
        return errorResult(e);
      }
    }
  );

  // -- commit_log --
  server.registerTool(
    'wikihub_commit_log',
    {
      title: 'Commit log for a wiki',
      description: 'Read the git history for a wiki (author, message, timestamp per commit).',
      inputSchema: {
        owner: z.string(),
        slug: z.string(),
        limit: z.number().int().min(1).max(200).optional().default(50),
      },
    },
    async ({ owner, slug, limit }) => {
      try {
        const r = await api.getWikiHistory(owner, slug, limit);
        const commits =
          (r.commits as Array<Record<string, unknown>>) ||
          (r.history as Array<Record<string, unknown>>) ||
          [];
        if (!commits.length) return textResult(`No history for @${owner}/${slug}.`);
        return textResult(
          `${commits.length} commit(s) on @${owner}/${slug}:\n${commits
            .map((c) => `- ${JSON.stringify(c)}`)
            .join('\n')}`
        );
      } catch (e) {
        return errorResult(e);
      }
    }
  );

  // -- register_agent (self-register to obtain an api key) --
  server.registerTool(
    'wikihub_register_agent',
    {
      title: 'Register a new WikiHub account',
      description:
        'Self-register an account. Returns an api_key that works immediately — no email verification needed for basic reads/writes on your own wikis. Save the api_key; it is shown only once. Intended for one-shot agent onboarding (AGENTS.md §1).',
      inputSchema: {
        username: z
          .string()
          .min(2)
          .max(40)
          .regex(/^[a-z0-9_-]+$/)
          .optional()
          .describe('Desired username (2-40 chars, lowercase/alnum/-/_). Autogenerated if omitted.'),
        display_name: z.string().optional(),
        email: z.string().email().optional(),
        password: z.string().min(8).optional(),
      },
    },
    async (args) => {
      try {
        const r = await api.registerAgent(args);
        return textResult(
          [
            `Registered @${r.username} (user_id=${r.user_id}).`,
            `api_key: ${r.api_key}`,
            '',
            'Save the api_key — it is shown only once. Set it as WIKIHUB_API_KEY in your MCP client config or',
            'pass it via Authorization: Bearer <key> on the HTTP transport.',
          ].join('\n')
        );
      } catch (e) {
        return errorResult(e);
      }
    }
  );

  // ---------- ChatGPT Deep Research compatibility ----------
  //
  // ChatGPT's "bring your own MCP" / deep-research connector expects two
  // specific tools: `search` and `fetch`. We register aliases so the same
  // server works for both Claude (rich tool list) and ChatGPT DR
  // (minimum-viable tool list). The id we return from `search` is a
  // composite `owner/slug:path` string — `fetch(id)` parses it to resolve
  // the underlying page.

  server.registerTool(
    'search',
    {
      title: 'Search (ChatGPT DR shape)',
      description:
        'Alias of wikihub_search with the shape ChatGPT Deep Research expects: returns {results: [{id, title, text, url}]}.',
      inputSchema: { query: z.string().min(1) },
    },
    async ({ query }) => {
      try {
        const resp = await api.search(query, 20);
        const hits: WikihubSearchHit[] =
          (resp.results as WikihubSearchHit[]) || (resp.hits as WikihubSearchHit[]) || [];
        const results = hits.map((h) => {
          const wiki = (h.wiki as string) || '';
          const [owner, slug] = wiki.split('/', 2);
          const path = (h.page as string) || (h.path as string) || '';
          return {
            id: owner && slug ? hitId(owner, slug, path) : path,
            title: h.title || path || '(untitled)',
            text: (h.excerpt as string) || '',
            url: owner && slug && path ? pageUrl(owner, slug, path, baseUrl) : baseUrl,
          };
        });
        return { content: [{ type: 'text', text: JSON.stringify({ results }) }] };
      } catch (e) {
        return errorResult(e);
      }
    }
  );

  server.registerTool(
    'fetch',
    {
      title: 'Fetch (ChatGPT DR shape)',
      description:
        'Alias of wikihub_get_page with the shape ChatGPT Deep Research expects: returns {id, title, text, url, metadata}. `id` is the composite id returned by `search` (format: "owner/slug:path").',
      inputSchema: { id: z.string().min(1) },
    },
    async ({ id }) => {
      try {
        const parsed = parseHitId(id);
        if (!parsed) throw new Error(`Invalid id "${id}" — expected "owner/slug:path".`);
        const p: WikihubPage = await api.getPage(parsed.owner, parsed.slug, parsed.path);
        const payload = {
          id,
          title: p.title || p.path || '(untitled)',
          text: (p.content as string) || '',
          url: pageUrl(parsed.owner, parsed.slug, p.path || parsed.path, baseUrl),
          metadata: {
            wiki: `${parsed.owner}/${parsed.slug}`,
            path: p.path,
            visibility: p.visibility,
            updated_at: p.updated_at,
          },
        };
        return { content: [{ type: 'text', text: JSON.stringify(payload) }] };
      } catch (e) {
        return errorResult(e);
      }
    }
  );

  return server;
}
