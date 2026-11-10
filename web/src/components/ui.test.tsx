import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Proposals } from "../pages/TaskDetail";
import { ApprovalCard, TaskList } from "./shared";
import { AuthBadge, DiffView, ErrorBox, MockBadge, StateBadge, StructuredView } from "./ui";

afterEach(() => vi.restoreAllMocks());

describe("badges", () => {
  it("renders task state with a live indicator only for active states", () => {
    const { container, rerender } = render(<StateBadge state="EXECUTING" />);
    expect(screen.getByText("EXECUTING")).toBeInTheDocument();
    expect(container.querySelector(".animate-pulse")).not.toBeNull();
    rerender(<StateBadge state="AWAITING_APPROVAL" />);
    expect(screen.getByText("AWAITING APPROVAL")).toBeInTheDocument();
    expect(container.querySelector(".animate-pulse")).toBeNull();
  });

  it("labels mock activity explicitly", () => {
    render(<MockBadge />);
    expect(screen.getByText("MOCK")).toHaveAttribute("title", expect.stringContaining("not a real model"));
  });

  it("distinguishes expired auth from unverified login", () => {
    const { rerender } = render(<AuthBadge state="expired" />);
    expect(screen.getByText("auth expired")).toBeInTheDocument();
    rerender(<AuthBadge state="unverified" />);
    expect(screen.getByText("logged in (not yet verified)")).toBeInTheDocument();
  });
});

describe("structured handoffs", () => {
  it("renders a review with severities", () => {
    render(<StructuredView kind="review" data={{ verdict: "request_changes", summary: "needs work",
      issues: [{ severity: "blocker", file: "a.py", description: "crash on empty input", suggested_fix: "guard" }] }} />);
    expect(screen.getByText("request_changes")).toBeInTheDocument();
    expect(screen.getByText("blocker")).toBeInTheDocument();
    expect(screen.getByText("crash on empty input")).toBeInTheDocument();
  });

  it("shows unconfigured tests honestly", () => {
    render(<StructuredView kind="test_report" data={{ passed: null, skipped: true, summary: "No test command" }} />);
    expect(screen.getByText("not configured")).toBeInTheDocument();
  });

  it("colours diff lines", () => {
    render(<DiffView diff={"--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new"} />);
    expect(screen.getByText("+new").className).toContain("emerald");
    expect(screen.getByText("-old").className).toContain("red");
  });

  it("shows proposal differences side by side", () => {
    const props = [
      { id: "a1", run_id: "r1", kind: "proposal", title: "Proposal by claude", content: "", content_type: "", task_id: "t", created_at: 0,
        data: { title: "Milestone A", summary: "s", steps: ["write survey"], evidence: [], risks: [] } },
      { id: "a2", run_id: "r2", kind: "proposal", title: "Proposal by codex", content: "", content_type: "", task_id: "t", created_at: 0,
        data: { title: "Milestone B", summary: "s", steps: ["build prototype"], evidence: [], risks: [] } },
    ];
    const comparison = { proposals: [{ run_id: "r1", provider: "claude_code", model: "opus" }, { run_id: "r2", provider: "codex", model: "gpt" }],
      pairs: [{ a: "r1", b: "r2", title_similarity: 0.2, summary_similarity: 1, agreeing_steps: [], only_a_steps: ["write survey"],
        only_b_steps: ["build prototype"], shared_evidence: [], only_a_evidence: [], only_b_evidence: [], only_a_risks: [], only_b_risks: [] }] };
    render(<Proposals proposals={props as any} comparison={comparison} />);
    expect(screen.getByText("Proposal by claude")).toBeInTheDocument();
    expect(screen.getByText("Only in proposal B")).toBeInTheDocument();
    expect(screen.getAllByText("build prototype").length).toBeGreaterThan(1);
  });
});

describe("interactions", () => {
  it("posts an approval decision with the CSRF header", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ status: "approved" }), { headers: { "content-type": "application/json" } }));
    const onDone = vi.fn();
    render(<MemoryRouter><ApprovalCard a={{ id: "ap1", project_id: "p", task_id: "t1", kind: "merge", title: "Merge branch",
      detail: "diff", payload: {}, status: "pending", created_at: Date.now() / 1000, decided_at: null, decision_note: null }} onDone={onDone} /></MemoryRouter>);
    fireEvent.change(screen.getByLabelText("Decision note"), { target: { value: "looks good" } });
    fireEvent.click(screen.getByText("Approve"));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/approvals/ap1");
    expect((init.headers as Record<string, string>)["X-Forge-Client"]).toBe("web");
    expect(JSON.parse(init.body as string)).toEqual({ approve: true, note: "looks good" });
  });

  it("surfaces API errors", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ detail: "approval already approved" }), { status: 409 }));
    render(<MemoryRouter><ApprovalCard a={{ id: "ap1", project_id: "p", task_id: null, kind: "phase", title: "x", detail: "",
      payload: {}, status: "pending", created_at: 0, decided_at: null, decision_note: null }} /></MemoryRouter>);
    fireEvent.click(screen.getByText("Reject"));
    expect(await screen.findByRole("alert")).toHaveTextContent("approval already approved");
  });

  it("renders error boxes with retry", () => {
    const retry = vi.fn();
    render(<ErrorBox error="boom" onRetry={retry} />);
    fireEvent.click(screen.getByText("Retry"));
    expect(retry).toHaveBeenCalled();
  });

  it("lists tasks with links", () => {
    render(<MemoryRouter><TaskList tasks={[{ id: "t1", title: "Do it", state: "READY", phase: null, project_name: "p",
      created_at: Date.now() / 1000 } as any]} /></MemoryRouter>);
    expect(screen.getByText("Do it").closest("a")).toHaveAttribute("href", "/tasks/t1");
  });
});
