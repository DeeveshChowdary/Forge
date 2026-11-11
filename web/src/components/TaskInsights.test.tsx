import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FailureAndRecovery } from "./TaskInsights";

const failed = {
  id: "t_x", state: "FAILED",
  analysis: {
    failed: true, completed_phases: ["research", "hypothesis"],
    outputs: [{ artifact_id: "a1", kind: "report", title: "research" }, { artifact_id: "a2", kind: "report", title: "hypothesis" }],
    recoverable: [{ artifact_id: "d1", title: "Partial changes discarded", files: ["deep_review.md", "formal_model.md"], lines_added: 594 }],
    checkpoints: [],
    last_failure: { phase: "experiment", agent: "claude_code/sonnet", error: "turn limit 40 exceeded", classification: "limit_exhausted",
      steps_used: 41, step_limit: 40, runtime_s: 392, runtime_limit_s: 1800, identical_failures: 2 },
    retry: { eligible: true, requires: "a higher phase limit" },
  },
};

describe("FailureAndRecovery", () => {
  it("never presents a failed task with saved work as empty", () => {
    render(<FailureAndRecovery t={failed} onChange={() => {}} />);
    expect(screen.getByTestId("recoverable-banner")).toHaveTextContent("not empty");
    expect(screen.getByTestId("recoverable-banner")).toHaveTextContent("2 file(s) of partial work");
    expect(screen.getByText("limit_exhausted")).toBeInTheDocument();
    expect(screen.getByText("41 / 40")).toBeInTheDocument();
    expect(screen.getByText(/same failure 2×/)).toBeInTheDocument();
    expect(screen.getByText(/needs a higher phase limit/)).toBeInTheDocument();
    expect(screen.getByText("Recover to a new branch…")).toBeInTheDocument();
  });
});
