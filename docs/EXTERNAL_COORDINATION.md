# External coordination (ChatGPT and other assistants)

Forge exposes one coordination layer (`forge/coordination/`) with three front doors.
All three call the same `CoordinationService`, which reuses Forge's engine
(`submit_task`, approvals, `request_revision`, events, artifacts) — there is no second
orchestrator — and enforces scopes, project limits and sanitization in one place.

| Front door | Transport | Who can use it today | Writes |
|---|---|---|---|
| GitHub bridge | issues + comments in an allowlisted private repo, polled by Forge | anything that can write GitHub issues as an authorized user (ChatGPT via its GitHub connector or a person) | submit task, feedback, revision |
| Coordination API `/api/v1/coordination/*` | HTTP + bearer token, **localhost only** | local scripts/tools on this Mac | per token scopes |
| MCP server `forge mcp` | stdio JSON-RPC | local MCP clients that launch it (Claude Desktop/Code, Codex, …) | per token scopes |

## Using it from ChatGPT (works with the GitHub bridge)

ChatGPT cannot reach `127.0.0.1`. It can work through GitHub if it has access to the
repository (e.g. a GitHub connector authorized by you, or you paste its output into an
issue). A workable loop:

1. Ask ChatGPT to draft a task as a `forge-task` JSON block (schema in
   `docs/GITHUB_BRIDGE.md`), then create the issue with label `forge-task` in
   `DeeveshChowdary/independent-research` **as you** (only allowlisted authors are
   accepted; ChatGPT's connector acts with your GitHub identity when you authorize it).
2. Approve the task in Forge (Coordination or Approvals page) unless you disabled
   per-task authorization.
3. ChatGPT reads the issue: the status comment (edited in place) and the final summary
   contain state, models used, test results, review findings and report excerpts.
4. ChatGPT replies on the issue with `/forge feedback …` or, when Forge is waiting for
   merge approval, `/forge revise …`. Merges and other approvals remain yours, in Forge.

Whether your ChatGPT plan/workspace offers a GitHub connector with **issue write**
access, and how it is authorized, is controlled by OpenAI and your account; Forge does
not assume it. Without it, the same loop works with you as the courier for single
messages (issue text in, issue comments out) — still no copying between Claude and Codex.

## Coordination API (local)

Tokens: Coordination page → API tokens, or
`forge token create chatgpt --scope tasks:write --scope feedback:write --project "Independent Research"`.
Shown once; stored as SHA-256. Scopes: `read` (always), `tasks:write`, `feedback:write`.
No scope can approve merges, spending, or change permissions.

```
GET  /api/v1/coordination/projects
GET  /api/v1/coordination/tasks?project=&state=&limit=&offset=
GET  /api/v1/coordination/tasks/{id}
GET  /api/v1/coordination/tasks/{id}/events?after=&limit=
GET  /api/v1/coordination/tasks/{id}/artifacts?kind=&limit=
GET  /api/v1/coordination/agents
POST /api/v1/coordination/tasks                 (tasks:write) body = forge-task schema
POST /api/v1/coordination/tasks/{id}/feedback   (feedback:write) {"kind": "comment"|"revision", "message": "…"}
```

Errors are JSON `{"error", "detail"}` with 401/403/404/409/422/429. Unauthorized
projects return 404 (existence is not disclosed). Responses never include local paths,
prompts, raw logs, provider credentials or Zotero data; there is no file browsing.
Per-token rate limit: 120 requests/minute (in-process).

Example:

```bash
T=fgt_…   # from `forge token create`
curl -s -H "Authorization: Bearer $T" http://127.0.0.1:8765/api/v1/coordination/tasks?state=EXECUTING
curl -s -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d '{"project":"Independent Research","title":"…","objective":"…","workflow":"research"}' \
  http://127.0.0.1:8765/api/v1/coordination/tasks
```

## MCP (local clients only)

```json
{ "mcpServers": { "forge": {
    "command": "/Users/<you>/research/forge/.venv/bin/forge", "args": ["mcp"],
    "env": { "FORGE_COORDINATION_TOKEN": "fgt_…" } } } }
```

Tools: `forge_list_projects`, `forge_list_tasks`, `forge_get_task`,
`forge_get_task_events`, `forge_get_task_artifacts`, `forge_list_agents`,
`forge_submit_task`, `forge_submit_feedback` — each enforced by the token's scopes.
This is a stdio server; ChatGPT cannot use it (see `docs/REMOTE_ACCESS.md`).
