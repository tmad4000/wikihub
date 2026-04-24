# Feedback API

Public endpoint for submitting bug reports, feature requests, comments,
and praise. No authentication is required, but if you send a valid
`Authorization: Bearer wh_...` header the feedback will be associated
with your user account and you get a higher rate limit.

## Endpoint

```
POST /api/v1/feedback
```

## Request body

```json
{
  "kind": "bug",
  "subject": "Search ignores quoted phrases",
  "body": "Querying for \"exact phrase\" returns results that don't contain it.\n\nSteps to reproduce...",
  "context": {
    "page_url": "https://wikihub.globalbr.ai/search?q=%22exact+phrase%22",
    "user_agent": "Mozilla/5.0 ...",
    "wiki": "@alice/research",
    "extra": {"build": "abc123"}
  },
  "contact_email": "me@example.com"
}
```

### Fields

| field | type | required | notes |
|---|---|---|---|
| `kind` | string | yes | one of `bug`, `feature`, `comment`, `praise` |
| `subject` | string | yes | <=200 chars |
| `body` | string | yes | <=10000 chars, markdown allowed |
| `context` | object | no | free-form; `page_url`, `user_agent`, `wiki`, `extra` are conventional keys |
| `contact_email` | string | no | <=256 chars; for follow-up if you're anonymous |

## Response

`201 Created`:

```json
{
  "id": "fb_abc12345",
  "received_at": "2026-04-23T10:30:00+00:00",
  "status": "received"
}
```

Validation failures return `400` with an error envelope:

```json
{"error": "bad_request", "message": "kind must be one of ['bug', 'comment', 'feature', 'praise']", "field": "kind"}
```

## Rate limits

- Anonymous callers: **10 requests/minute per IP**
- Authenticated callers: **60 requests/minute per user**

Exceeded limits return `429 Too Many Requests` with a `Retry-After` header.

## Privacy

Raw IPs are never stored. Each submission records a daily-salted SHA-256
hash of the client IP (`sha256(ip + utc_date)`) for abuse review only;
the salt rotates at UTC midnight so the hash can't be used to link
activity across days.

## curl examples

Anonymous bug report:

```bash
curl -sS -X POST https://wikihub.globalbr.ai/api/v1/feedback \
  -H "Content-Type: application/json" \
  -d '{
    "kind": "bug",
    "subject": "404 on public wiki page after rename",
    "body": "After renaming a page, the old URL 404s instead of redirecting."
  }'
```

Authenticated feature request with context:

```bash
curl -sS -X POST https://wikihub.globalbr.ai/api/v1/feedback \
  -H "Authorization: Bearer wh_..." \
  -H "Content-Type: application/json" \
  -d '{
    "kind": "feature",
    "subject": "Bulk delete pages",
    "body": "Would like to delete multiple pages in one API call.",
    "context": {"wiki": "@alice/research"}
  }'
```
