/**
 * Personalized server instructions.
 *
 * MCP's `InitializeResult.instructions` is an optional server-level system
 * prompt. Clients typically inject it into the model's context on connect,
 * so whatever we put here is the model's first impression of WikiHub.
 *
 * This builder takes an api client (scoped to the caller's key) and returns
 * a dynamic, personalized string. All API calls are best-effort — if they
 * fail we still return a useful generic instructions block rather than
 * breaking the MCP handshake.
 */

import type { WikihubApi } from './api.js';

const GENERIC_INSTRUCTIONS = `You are connected to WikiHub — GitHub for LLM wikis. Markdown pages live
inside wikis, which live under owners (/@username/wiki-slug/...). The server
is the source of truth; git repos back every wiki.

Core tools you have (17 total):

READ
- wikihub_search           — fuzzy search across pages (scope with wiki="owner/slug")
- wikihub_get_page         — read one page's content
- wikihub_list_pages       — list every page in a wiki you can read
- wikihub_get_wiki         — wiki metadata (title, description, counts)
- wikihub_commit_log       — git history for a wiki
- wikihub_shared_with_me   — pages and wikis granted to you
- wikihub_whoami           — identity of the current api key

WRITE
- wikihub_create_wiki
- wikihub_create_page
- wikihub_update_page
- wikihub_append_section   — append markdown under an optional heading (non-destructive)
- wikihub_delete_page
- wikihub_set_visibility   — public | public-edit | private | unlisted
- wikihub_share            — grant read/edit to a user or email
- wikihub_list_grants
- wikihub_fork_wiki
- wikihub_register_agent   — self-register a new account, returns an api_key

KEY INVARIANTS
- Visibilities are: public, public-edit, private, unlisted. Frontmatter
  wins over ACL file — setting page-level visibility overrides the wiki
  default.
- On public-edit wikis, anyone (even without a key) can create pages
  (AGENTS.md core principle 2). Pass anonymous=true to wikihub_create_page
  to do this explicitly.
- Writes default to attributing via the MCP connector's provenance
  headers. The server records these in ApiKey.agent_name/agent_version.
- Page URLs: https://wikihub.md/@owner/slug/path (drop trailing .md).
- Prefer wikihub_append_section over wikihub_update_page for journals and
  session logs — it preserves prior content.
`;

/**
 * Ask WikiHub who the caller is, fetch a little context, and return a
 * personalized instructions string to seed the connected model's context.
 */
export async function buildPersonalizedInstructions(
  api: WikihubApi
): Promise<string> {
  if (!api.hasApiKey) return GENERIC_INSTRUCTIONS;

  const meResult = await Promise.allSettled([api.whoami()]);
  const me = meResult[0].status === 'fulfilled' ? meResult[0].value : null;

  const identity = me
    ? `You are connected as **@${me.username}** (${me.email || 'no email on file'}, user_id=${me.user_id}).`
    : 'You are connected to WikiHub but the caller\'s identity could not be resolved (the api key may be invalid).';

  return [identity, '', '---', '', GENERIC_INSTRUCTIONS].join('\n');
}
