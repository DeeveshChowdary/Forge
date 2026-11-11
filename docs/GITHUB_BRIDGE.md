# GitHub coordination bridge

The bridge lets an external assistant (ChatGPT, or a person) submit work to Forge by
opening a GitHub issue, follow progress in that issue, and send feedback with
comments — without anyone copying messages between agents. Forge polls GitHub; no
webhook, public endpoint or inbound internet access is needed.

## How a request flows

1. An **authorized author** opens an issue in an **allowlisted repository**, adds the
   label **`forge-task`**, and puts exactly one JSON block in the body:

   ````markdown
   ```forge-task
   {
     "project": "Independent Research",
     "title": "Survey closest prior art for R4",
     "objective": "Read R4-computation-migration/docs and produce a report …",
     "workflow": "research",
     "acceptance_criteria": ["Every claim cites a file", "No novelty claims"],
     "complexity": "high",
     "routing_mode": "balanced",
     "review_depth": "standard",
     "approval_policy": "risky",
     "read_only": false
   }
   ```
   ````

   Allowed fields: `project` (required), `title` (required), `objective` (required, ≤ 8000
   chars), `workflow`, `acceptance_criteria` (≤ 20), `complexity` (low/medium/high),
   `routing_mode` (balanced/max_quality/economy), `review_depth` (standard/independent),
   `approval_policy` (risky/never_merge), `read_only`. Unknown fields are rejected —
   there is no way to pass shell commands, model overrides that bypass verification,
   auto-merge, or permission changes.
2. On the next poll the bridge **discovers** the issue and **validates** it: repository
   allowlisted and private, author allowlisted *and* an owner/member/collaborator,
   label present, one valid block, project enabled for this repository, workflow
   allowed.
3. By default the task is created **awaiting authorization**: it appears in Forge
   (Approvals and Coordination pages) and no agent runs until you approve it. Turn
   this off per repository ("Require my authorization per task") to queue directly.
4. The task runs through the normal engine (routing, worktree, tests, review, bounded
   fix loop). The bridge maintains one **status comment** on the issue, edited in
   place: state, phase, agent/model, review cycle, test result, review verdict, runs,
   commits (linked if they exist on GitHub, otherwise marked local).
5. **Milestone comments** (each posted once): awaiting authorization, every human
   approval Forge needs (merge, unresolved review, spending) — these can only be
   decided in Forge — blockers, and a **final summary** with outcome, tests, review
   findings and excerpts of plans/reports.
6. **Feedback**: an authorized author comments with one of
   * `/forge revise <text>` — when the task is waiting for merge/review approval, sends
     the latest review plus your text to the fix phase (bounded by Forge's revision limit);
   * `/forge feedback <text>` — records feedback; agents receive it as a handoff artifact
     in later phases;
   * `/forge status` — republish the status comment now.
   Any other comment is ignored. Comments from anyone else are never read into Forge.

## States shown on the Coordination page

`discovered → validated → awaiting_authorization → queued → running → completed`,
or `rejected` (unauthorized author, invalid request, project not allowed, or rejected by
you) / `failed` (task failed or was cancelled). `running` covers execution and waiting
for your approvals; the status comment says which.

## Guarantees

* **No duplicates.** `(repo, issue_number)` is unique and is inserted in the same
  database transaction that creates the task. Processed comment ids are stored in the
  same transaction as the action they trigger. Outbound messages have unique keys and
  are sent from a persistent outbox, so restarts or network failures never double-post.
* **Edits after submission are not applied** (the bridge says so once); open a new issue
  or use `/forge feedback`.
* **Only one bridge worker** acts at a time (`worker_locks` row with heartbeat).
* **Rate limits:** conditional requests (ETag) so unchanged polls are free; the client
  stops before a reserve of 50 requests; HTTP 403/429 rate limits back off until the
  reset time; network/5xx errors retry twice, then the cycle backs off exponentially
  (poll interval × 2ⁿ, capped at 30 min). Poll interval is configurable (≥ 30 s, default 120 s).
* **Sanitized output:** secrets/token patterns, environment lines, e-mail addresses,
  local paths (`/Users/…`, worktrees, temp dirs) and `@mentions` are removed or
  neutralized; images are stripped; length is capped.

## Setup

1. **Token.** Create a fine-grained personal access token restricted to
   `DeeveshChowdary/independent-research` with:
   * Repository permissions → **Issues: Read and write**, **Metadata: Read** (automatic),
     optionally **Contents: Read** (only used to check whether reported commits exist).
   Provide it to Forge without putting it in a file, by one of:
   * `gh auth login` (GitHub CLI) — Forge calls `gh auth token`;
   * macOS keychain: `security add-generic-password -s forge-github-token -a forge -w`
     (prompts for the token);
   * environment variable `FORGE_GITHUB_TOKEN` for the `forge serve`/`forge bridge`
     process.
2. `forge bridge check` — verifies the token, the account, that each repository is
   private, and that issues can be listed. Comment-write permission is proven by the
   first published comment.
3. In **Coordination**, confirm authorized authors (default `DeeveshChowdary`) and which
   Forge projects the repository may target (default `Independent Research`).
4. **Enable bridge** (or `forge bridge enable`). The worker runs inside `forge serve`;
   `forge bridge run` runs it standalone; `forge bridge sync` does one cycle.
5. Create the `forge-task` label in the repository.

## Commands

```bash
forge bridge status|check|enable|disable|sync|run
```
