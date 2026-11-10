# Security model and limitations

Forge runs AI agents on your machine with your existing CLI logins. This document states
what Forge enforces, what it relies on the provider CLIs for, and what it does not protect
against.

## What Forge enforces

**Network exposure**
* `forge serve` binds to `127.0.0.1` and refuses non-local addresses unless a hidden
  override flag is passed. The API has no authentication — do not expose it.
* Requests with a non-local `Host` header are rejected (DNS-rebinding guard).
* Mutating API calls require an `X-Forge-Client` header and a local `Origin` if present,
  so other websites cannot issue cross-site requests to the local server.

**Credentials and billing**
* Forge never reads, writes, or prints provider credentials. It calls `claude auth status`
  and `codex login status` only to report state.
* Agent subprocesses get an allowlisted environment. `ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY` and host-session variables are not passed, so a CLI cannot silently
  switch to directly billed API usage.
* Directly billed API providers are excluded by the router unless both the global switch
  and the project's `allow_paid_api` are enabled. A known-spend limit (default $0) blocks
  further runs and raises a `spending` approval.

**File-system isolation**
* Agents that write work only in a dedicated git worktree on a `forge/<task>` branch.
  Forge commits inside that worktree; your working tree is untouched.
* Merging into your working branch requires approval by default and is refused if your
  working tree has uncommitted changes.
* Projects cannot overlap (one registered root cannot contain another).
* Importing a folder never modifies it. Folders without git history are registered
  read-only rather than initialised.
* File browsing through the API is confined to the project root (`..` and symlink escapes
  rejected; `.git` internals hidden).
* Read-only projects: Claude runs with only `Read, Grep, Glob`; Codex runs with
  `-s read-only`. No worktree is created.

**Untrusted instructions**
* Repository files such as `AGENTS.md`, `CLAUDE.md`, `.cursorrules` are recorded as
  untrusted. Forge does not put them in prompts until you trust them; trust is pinned to
  the file's SHA-256 and revoked automatically when the file changes.
* Claude runs with `--setting-sources user` (a repository's `.claude/settings*.json` cannot
  grant itself permissions or hooks) and `--strict-mcp-config` (a repository `.mcp.json`
  is ignored unless you configure MCP for the project in Forge).
* Codex runs with `--ignore-user-config` by default for reproducible, isolated runs.
* Every agent prompt states that repository content is data, not instructions.

**Shell access**
* Forge never runs commands through a shell. Test commands are split into argv.
* Claude's shell is limited to an allowlist of prefixes (`python -m pytest`, `pytest`,
  `git status|diff|log`, `ls`, and your test command) via `Bash(prefix:*)` rules.
  Unrestricted `Bash` is never granted. Network tools are off unless enabled per project.

**Approvals** are required for merges, spending, accepting unresolved review issues, and
(optionally) every phase. Decisions are recorded in project memory with a source.

## What Forge relies on the CLIs for

* **Claude Code has no OS sandbox.** Restrictions are tool allowlists and permission
  rules enforced by the CLI. An allowed command such as `python -m pytest` executes
  repository code (the tests) with your user's permissions — so does running the project's
  tests yourself. Only enable write workflows on code you are willing to execute.
* **Codex `workspace-write`** is an OS-level sandbox provided by Codex; Forge does not
  verify its internals.
* Both CLIs may load repository instruction files (`CLAUDE.md`, `AGENTS.md`) natively as
  context, even if Forge has not trusted them. Forge's trust model governs only what Forge
  itself injects.

## Known limitations

* The test command (`{python} -m pytest …`) runs project code without a sandbox, the same
  as running tests manually.
* Orphan-process recovery kills a recorded pid only if its command line still looks like
  an agent/test process; a different process reusing the pid with a similar name could in
  theory be affected.
* The Zotero integration's `r4_cli` backend executes the R4 project's own `r4` CLI (read
  commands `status`, `search`, `get` only). That tool may update its own ignored private
  cache under `.local/`. The default `local_api` backend executes no project code.
* GitHub publishing (push, PR creation) is not automated. Nothing is ever pushed.
* No multi-user access control: anyone with local access to the port can use the API.

## Reporting

This is a personal tool; report issues to the repository owner.
