import { useState } from "react";
import { api, type ForgeEvent, type Run } from "../lib/api";
import { duration } from "../lib/format";
import { useNow } from "../lib/hooks";
import { Badge, Card, ErrorBox, MockBadge } from "./ui";

/** Current phase, agent, steps vs limit and runtime vs limit — from persisted runs and live events only. */
export function ProgressLimits({ t, events }: { t: any; events: ForgeEvent[] }) {
  const now = useNow(1000);
  const running: Run | undefined = t.runs.find((r: Run) => r.state === "running");
  const last: Run | undefined = running ?? [...t.runs].reverse().find((r: Run) => r.kind === "agent");
  if (!last) return null;
  const lim = t.phase_limits?.[last.phase] ?? {};
  const steps = running
    ? events.filter((e) => e.run_id === running.id && (e.type === "agent.tool" || e.type === "agent.message")).length
    : last.usage?.turns;
  const stepLimit = last.usage?.max_turns ?? lim.max_turns;
  const elapsed = (last.ended_at ?? now) - last.started_at;
  const rtLimit = last.usage?.max_runtime_s ?? lim.max_runtime_s;
  const bar = (v: number | null | undefined, max: number | null | undefined) => {
    if (v == null || !max) return null;
    const pct = Math.min(100, Math.round((100 * v) / max));
    return (
      <div className="mt-1 h-1.5 w-full rounded bg-zinc-200 dark:bg-zinc-800" aria-hidden>
        <div className={`h-1.5 rounded ${pct >= 90 ? "bg-red-500" : pct >= 70 ? "bg-yellow-500" : "bg-emerald-500"}`} style={{ width: `${pct}%` }} />
      </div>
    );
  };
  return (
    <Card title={running ? "Running now" : "Last agent run"}>
      <div className="grid gap-4 text-sm sm:grid-cols-4">
        <div><div className="text-xs muted">Phase</div><div className="font-medium">{last.phase} <span className="muted">({last.role})</span></div></div>
        <div><div className="text-xs muted">Agent</div><div className="font-mono text-xs">{last.provider}/{last.resolved_model ?? last.model}{last.effort ? ` · ${last.effort}` : ""} {last.is_mock && <MockBadge />}</div></div>
        <div data-testid="steps"><div className="text-xs muted">Agent steps vs limit</div><div>{steps ?? "—"} / {stepLimit ?? "—"}{running && <span className="muted"> (live, approx.)</span>}</div>{bar(steps, stepLimit)}</div>
        <div><div className="text-xs muted">Runtime vs limit</div><div>{duration(elapsed)} / {rtLimit ? duration(rtLimit) : "—"}</div>{bar(elapsed, rtLimit)}</div>
      </div>
    </Card>
  );
}

export function RoutePlan({ t }: { t: any }) {
  const plan = t.metadata?.route_plan;
  const warnings: string[] = t.metadata?.preflight?.warnings ?? [];
  if (!plan && !warnings.length) return null;
  return (
    <Card title={<span className="flex items-center gap-2">Planned routes <Badge tone="blue">{plan?.policy ?? t.metadata?.collaboration ?? "auto"}</Badge></span>}>
      {plan && (
        <ul className="space-y-1 text-sm">
          {plan.phases.filter((p: any) => p.agents.length).map((p: any) => (
            <li key={p.phase} className="flex flex-wrap gap-2"><span className="w-44 font-medium">{p.phase}</span>
              {p.agents.map((a: any, i: number) => a.error ? <Badge key={i} tone="red">blocked: {a.error.slice(0, 80)}</Badge>
                : <span key={i} className="font-mono text-xs">{a.provider}/{a.model}{a.effort ? ` · ${a.effort}` : ""}</span>)}
            </li>))}
        </ul>
      )}
      {warnings.length > 0 && <ul className="mt-3 space-y-1 text-xs text-yellow-800 dark:text-yellow-300">{warnings.map((w, i) => <li key={i}>⚠ {w}</li>)}</ul>}
    </Card>
  );
}

/** Failure reason, retry eligibility, recoverable work, checkpoints. Never shows a failed task as empty. */
export function FailureAndRecovery({ t, onChange }: { t: any; onChange: () => void }) {
  const a = t.analysis;
  const [turns, setTurns] = useState<number>(Math.max(100, (a?.last_failure?.step_limit ?? 40) * 3));
  const [cont, setCont] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  if (!a || (!a.last_failure && !a.recoverable.length && !a.checkpoints.length)) return null;
  const lf = a.last_failure;
  const retry = async () => {
    setErr(null);
    try { await api.post(`/api/tasks/${t.id}/retry`, { phase_max_turns: turns, continue_from_checkpoint: cont }); onChange(); }
    catch (e: any) { setErr(e.message); }
  };
  const recover = async (artifact_id: string) => {
    setErr(null);
    try { await api.post(`/api/tasks/${t.id}/recover`, { artifact_id }); setMsg("Recovery approval requested — approve it above or in Approvals."); onChange(); }
    catch (e: any) { setErr(e.message); }
  };
  return (
    <div className="space-y-4">
      {t.state === "FAILED" && (a.outputs.length > 0 || a.recoverable.length > 0) && (
        <div className="rounded-lg border border-sky-300 bg-sky-50 p-3 text-sm dark:border-sky-900 dark:bg-sky-950/40" data-testid="recoverable-banner">
          This task failed, but it is <b>not empty</b>: {a.completed_phases.length} phase(s) completed ({a.completed_phases.join(", ") || "none"}),
          {" "}{a.outputs.length} structured output(s), and {a.recoverable.reduce((n: number, r: any) => n + r.files.length, 0)} file(s) of partial work were preserved.
        </div>
      )}
      {lf && (
        <Card title={<span className="flex items-center gap-2">Failure analysis <Badge tone={lf.classification === "limit_exhausted" ? "orange" : "red"}>{lf.classification}</Badge></span>}>
          <dl className="grid gap-2 text-sm sm:grid-cols-3">
            <div><dt className="text-xs muted">Phase / agent</dt><dd>{lf.phase} · <span className="font-mono text-xs">{lf.agent}</span></dd></div>
            <div><dt className="text-xs muted">Steps used / limit</dt><dd>{lf.steps_used ?? "—"} / {lf.step_limit ?? "—"}</dd></div>
            <div><dt className="text-xs muted">Runtime / limit</dt><dd>{duration(lf.runtime_s)} / {lf.runtime_limit_s ? duration(lf.runtime_limit_s) : "—"}</dd></div>
            <div className="sm:col-span-3"><dt className="text-xs muted">Reason</dt><dd className="text-red-700 dark:text-red-400">{lf.error}{lf.identical_failures > 1 ? ` (same failure ${lf.identical_failures}×)` : ""}</dd></div>
          </dl>
          {t.state === "FAILED" && a.retry && (
            <div className="mt-3 space-y-2 rounded-lg bg-zinc-50 p-3 text-sm dark:bg-zinc-800/60">
              <div><b>Retry eligibility:</b> {a.retry.requires ? `needs ${a.retry.requires}` : "can be retried"}</div>
              <div className="flex flex-wrap items-center gap-3">
                <label className="flex items-center gap-1">Step limit for <code>{lf.phase}</code>
                  <input type="number" min={10} max={1000} className="input w-24 py-1" value={turns} onChange={(e) => setTurns(Number(e.target.value))} aria-label="New step limit" /></label>
                <label className="flex items-center gap-1"><input type="checkbox" checked={cont} onChange={(e) => setCont(e.target.checked)} disabled={!a.checkpoints.length && !a.recoverable.length} />continue from preserved work</label>
                <button className="btn btn-primary" onClick={retry}>Retry with these settings</button>
              </div>
            </div>
          )}
        </Card>
      )}
      {(a.recoverable.length > 0 || a.checkpoints.length > 0) && (
        <Card title="Recoverable work & checkpoints">
          <ul className="space-y-2 text-sm">
            {a.recoverable.map((r: any) => (
              <li key={r.artifact_id} className="rounded border border-zinc-200 p-2 dark:border-zinc-800">
                <div className="flex flex-wrap items-center justify-between gap-2"><span className="font-medium">{r.title}</span>
                  <button className="btn btn-sm" onClick={() => recover(r.artifact_id)}>Recover to a new branch…</button></div>
                <div className="mt-1 font-mono text-xs muted">{r.files.join(", ")} · +{r.lines_added} lines</div>
              </li>))}
            {a.checkpoints.map((c: any) => (
              <li key={c.artifact_id} className="text-xs muted">checkpoint <code>{c.ref}</code> ({c.sha?.slice(0, 10)}) — {c.reason}</li>))}
          </ul>
          <p className="mt-2 text-xs muted">Recovery creates a new branch from the task's base commit after your approval; your working tree is not touched.</p>
          {msg && <div className="mt-2 text-sm text-emerald-600">{msg}</div>}
        </Card>
      )}
      <ErrorBox error={err} />
    </div>
  );
}
