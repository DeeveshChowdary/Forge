# Remote access design

Forge's server binds to localhost and its UI API has no user authentication (only
host/CSRF guards). It must not be exposed to the internet. This document evaluates how
remote assistants can still coordinate with Forge, and what each option would require.

## Options

### 1. GitHub Issues as an asynchronous transport — implemented

* **How:** Forge polls GitHub outbound (HTTPS to api.github.com). The assistant writes
  issues/comments; Forge writes status comments.
* **Network exposure:** none inbound. Only outbound HTTPS from the Mac.
* **Authentication:** GitHub identities. Forge accepts only allowlisted authors who are
  also repo collaborators, in allowlisted private repositories; per-task authorization
  in Forge by default.
* **Latency:** poll interval (default 120 s, min 30 s).
* **Requires:** a GitHub token with Issues read/write on the repository (fine-grained PAT
  or `gh auth login`); for ChatGPT, a GitHub connector with issue write access
  authorized in your ChatGPT account (availability is decided by OpenAI and your plan).
* **Status:** implemented and tested against a simulated GitHub; live GitHub writes not
  yet verified on this machine (no token configured — `gh` is not installed and
  `FORGE_GITHUB_TOKEN` is unset).

### 2. Authenticated MCP server

* **Local stdio MCP — implemented** (`forge mcp`). Reuses the coordination service and
  token scopes. Usable by MCP clients running on this Mac.
* **Remote MCP for ChatGPT — not implemented.** ChatGPT connectors (custom
  connectors / developer mode, where your plan allows them) need a **remote HTTPS MCP
  endpoint** reachable from OpenAI's servers, typically with **OAuth 2.1**
  authorization, registered by you in ChatGPT's connector settings. ChatGPT does not
  discover arbitrary MCP servers. Providing one would require:
  1. a public HTTPS endpoint (a hosted service, or a tunnel such as Cloudflare Tunnel /
     Tailscale Funnel / ngrok — **each requires your explicit approval**; none is set up);
  2. an OAuth authorization server (or a provider's), mapping to Forge tokens and
     scopes;
  3. the MCP Streamable HTTP transport in front of `CoordinationService` (not the UI API);
  4. connector registration in ChatGPT and per-user authorization;
  5. a security review: rate limiting at the edge, audit logging, token rotation, and a
     kill switch.

### 3. Outbound relay for live communication

* **How:** Forge keeps an outbound WebSocket/long-poll connection to a relay you control;
  the assistant talks to the relay. No inbound ports on the Mac.
* **Requires:** hosting a relay service, authenticating both sides, and an assistant
  integration that can call the relay (again a connector/action on the ChatGPT side).
* **Status:** not implemented. Recommended only if GitHub's latency is a real problem.

## Recommendation

Use the GitHub bridge (option 1) now. If near-real-time interaction from ChatGPT becomes
necessary, build option 2 as a separate small service exposing only the coordination
service over MCP with OAuth, behind a tunnel you approve — never the existing UI API.

## What is never done without your explicit approval

Public tunnels, router/port configuration, binding Forge to non-local addresses,
changing repository visibility, registering connectors, or creating tokens on your
behalf.
