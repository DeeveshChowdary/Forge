import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Badge, Card, ErrorBox, Field, MockBadge, Spinner } from "../components/ui";
import { api, type Project } from "../lib/api";
import { useApi } from "../lib/hooks";

type Route = { provider: string; model: string; effort?: string };

export default function NewTask() {
  const nav = useNavigate();
  const [params] = useSearchParams();
  const { data: projects } = useApi<Project[]>("/api/projects");
  const { data: settings } = useApi<any>("/api/settings");
  const { data: prov } = useApi<any>("/api/providers");

  const [projectId, setProjectId] = useState(params.get("project") ?? "");
  const [goal, setGoal] = useState("");
  const [title, setTitle] = useState("");
  const [criteria, setCriteria] = useState("");
  const [workflow, setWorkflow] = useState("");
  const [routing, setRouting] = useState<"auto" | "manual">("auto");
  const [mode, setMode] = useState("balanced");
  const [manual, setManual] = useState<Record<string, Route>>({});
  const [complexity, setComplexity] = useState("medium");
  const [criticality, setCriticality] = useState("normal");
  const [reviewDepth, setReviewDepth] = useState("standard");
  const [approval, setApproval] = useState("risky");
  const [maxUsd, setMaxUsd] = useState(0);
  const [decompose, setDecompose] = useState(false);
  const [milestone, setMilestone] = useState(false);
  const [mockJson, setMockJson] = useState("");
  const [preview, setPreview] = useState<Record<string, any>>({});
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const project = projects?.find((p) => p.id === projectId);
  const readOnly = project?.config.permissions.mode === "read_only";
  useEffect(() => {
    if (!projectId && projects?.length) setProjectId(projects[0].id);
  }, [projects, projectId]);
  useEffect(() => {
    if (project) {
      setWorkflow(readOnly ? "independent_proposals" : project.config.default_workflow);
      setMode(project.config.default_routing_mode);
    }
  }, [projectId]); // eslint-disable-line react-hooks/exhaustive-deps

  const workflows: any[] = settings?.workflows ?? [];
  const wf = workflows.find((w) => w.key === workflow);
  const agentPhases: any[] = useMemo(() => (wf?.phases ?? []).filter((p: any) => p.kind === "agent" || p.kind === "fanout"), [wf]);
  const models: any[] = useMemo(() => (prov?.providers ?? []).flatMap((p: any) => p.models.filter((m: any) => m.enabled)
    .map((m: any) => ({ ...m, auth: p.auth_state }))), [prov]);
  const mockEnabled = settings?.settings?.enable_mock_provider;

  const runPreview = async () => {
    if (!projectId || routing === "manual") return;
    setErr(null);
    const out: Record<string, any> = {};
    for (const ph of agentPhases) {
      try {
        out[ph.key] = await api.post("/api/router/preview", { project_id: projectId, phase: ph.key, role: ph.role, complexity, criticality, mode });
      } catch (e: any) {
        out[ph.key] = { error: e.message };
      }
    }
    setPreview(out);
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      let mock: any = undefined;
      if (mockJson.trim()) mock = JSON.parse(mockJson);
      const overrides: Record<string, any> = {};
      if (routing === "manual") {
        for (const ph of agentPhases) {
          const r = manual[ph.key];
          if (!r?.provider || !r?.model) throw new Error(`Choose a model for phase “${ph.key}”`);
          overrides[ph.key] = ph.kind === "fanout" ? [r, ...(manual[`${ph.key}#2`]?.model ? [manual[`${ph.key}#2`]] : [])] : r;
        }
      }
      const t = await api.post("/api/tasks", {
        project_id: projectId, title: title || goal.split("\n")[0].slice(0, 80), objective: goal, workflow,
        acceptance_criteria: criteria.split("\n").map((s) => s.trim()).filter(Boolean),
        complexity, criticality, routing_mode: routing === "manual" ? "manual" : mode, routing_overrides: overrides,
        review_depth: reviewDepth, approval_policy: approval, budget: { max_known_api_usd: maxUsd },
        decompose, as_milestone: milestone, mock, execution_mode: readOnly ? "read_only" : undefined,
      });
      nav(`/tasks/${t.id}`);
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  if (!projects || !settings) return <Spinner />;
  if (!projects.length) return <ErrorBox error="Add a project first (Projects → Add project)." />;

  const routeSelect = (key: string, label: string) => {
    const r = manual[key] ?? { provider: "", model: "" };
    const m = models.find((x) => x.provider === r.provider && x.model_id === r.model);
    const levels: string[] = m?.capabilities?.reasoning_levels ?? [];
    return (
      <div key={key} className="grid gap-2 sm:grid-cols-[8rem_1fr_8rem] sm:items-center">
        <span className="text-sm font-medium">{label}</span>
        <select className="input" aria-label={`Model for ${label}`} value={r.provider ? `${r.provider}::${r.model}` : ""}
          onChange={(e) => { const [provider, model] = e.target.value.split("::"); setManual({ ...manual, [key]: { provider, model } }); }}>
          <option value="">— choose model —</option>
          {models.map((x) => (
            <option key={`${x.provider}::${x.model_id}`} value={`${x.provider}::${x.model_id}`} disabled={x.status === "failed"}>
              {x.provider} / {x.model_id} ({x.status}{x.auth === "expired" ? ", auth expired" : ""})
            </option>
          ))}
        </select>
        <select className="input" aria-label={`Effort for ${label}`} value={r.effort ?? ""} disabled={!levels.length}
          onChange={(e) => setManual({ ...manual, [key]: { ...r, effort: e.target.value || undefined } })}>
          <option value="">default effort</option>
          {levels.map((l) => <option key={l}>{l}</option>)}
        </select>
      </div>
    );
  };

  return (
    <form onSubmit={submit} className="space-y-6">
      <h1 className="text-2xl font-semibold">New task</h1>
      <Card>
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Project">
              <select className="input" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
                {projects.map((p) => <option key={p.id} value={p.id}>{p.name}{p.config.permissions.mode === "read_only" ? " (read-only)" : ""}</option>)}
              </select>
            </Field>
            <Field label="Workflow">
              <select className="input" value={workflow} onChange={(e) => { setWorkflow(e.target.value); setPreview({}); }}>
                {workflows.map((w) => <option key={w.key} value={w.key} disabled={readOnly && !w.read_only_ok}>{w.name}{readOnly && !w.read_only_ok ? " (needs write)" : ""}</option>)}
              </select>
            </Field>
          </div>
          {wf && <p className="text-xs muted">{wf.description} Phases: {wf.phases.map((p: any) => p.key).join(" → ")}{wf.has_review_loop ? " (bounded fix loop)" : ""}</p>}
          <Field label="Describe your goal">
            <textarea className="input min-h-36 text-base" required value={goal} onChange={(e) => setGoal(e.target.value)}
              placeholder="e.g. Add a `slugify(text)` helper to utils.py that lowercases, strips accents and joins words with hyphens. Include tests." />
          </Field>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Title (optional)"><input className="input" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Defaults to the first line of the goal" /></Field>
            <Field label="Acceptance criteria (one per line)"><textarea className="input min-h-[2.6rem]" value={criteria} onChange={(e) => setCriteria(e.target.value)} /></Field>
          </div>
        </div>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Model routing">
          <div className="space-y-3">
            <div role="radiogroup" aria-label="Routing" className="grid grid-cols-2 gap-2">
              <button type="button" role="radio" aria-checked={routing === "auto"} className={`btn ${routing === "auto" ? "btn-primary" : ""}`} onClick={() => setRouting("auto")}>Automatic</button>
              <button type="button" role="radio" aria-checked={routing === "manual"} className={`btn ${routing === "manual" ? "btn-primary" : ""}`} onClick={() => setRouting("manual")}>Manual</button>
            </div>
            {routing === "auto" ? (
              <>
                <Field label="Mode">
                  <select className="input" value={mode} onChange={(e) => { setMode(e.target.value); setPreview({}); }}>
                    <option value="balanced">Balanced — quality, latency and cost</option>
                    <option value="max_quality">Maximum quality — strong models, independent review</option>
                    <option value="economy">Economy — cheapest adequate, escalate on failure</option>
                  </select>
                </Field>
                <div className="grid grid-cols-2 gap-3">
                  <Field label="Complexity"><select className="input" value={complexity} onChange={(e) => setComplexity(e.target.value)}>{["low", "medium", "high"].map((x) => <option key={x}>{x}</option>)}</select></Field>
                  <Field label="Criticality"><select className="input" value={criticality} onChange={(e) => setCriticality(e.target.value)}>{["low", "normal", "high"].map((x) => <option key={x}>{x}</option>)}</select></Field>
                </div>
                <button type="button" className="btn" onClick={runPreview}>Preview routing</button>
                {Object.keys(preview).length > 0 && (
                  <ul className="space-y-2 text-sm" data-testid="route-preview">
                    {agentPhases.map((ph) => {
                      const d = preview[ph.key];
                      if (!d) return null;
                      return (
                        <li key={ph.key} className="rounded-lg border border-zinc-200 p-2 dark:border-zinc-800">
                          <div className="flex flex-wrap items-center gap-2"><Badge>{ph.key}</Badge>
                            {d.error ? <span className="text-red-600">{d.error}</span> : <span className="font-mono text-xs">{d.provider}/{d.model}{d.effort ? ` · ${d.effort}` : ""}</span>}
                            {d.provider === "mock" && <MockBadge />}</div>
                          {d.reasons && <ul className="mt-1 list-disc pl-5 text-xs muted">{d.reasons.map((r: string, i: number) => <li key={i}>{r}</li>)}</ul>}
                        </li>
                      );
                    })}
                  </ul>
                )}
              </>
            ) : (
              <div className="space-y-2">
                {agentPhases.map((ph) => ph.kind === "fanout"
                  ? [routeSelect(ph.key, `${ph.key} #1`), routeSelect(`${ph.key}#2`, `${ph.key} #2`)]
                  : routeSelect(ph.key, ph.key === "fix" ? "fix" : ph.key))}
                {wf?.has_review_loop && <p className="text-xs muted">The fix phase uses the implementer's model unless routed separately.</p>}
              </div>
            )}
          </div>
        </Card>

        <Card title="Execution & approvals">
          <div className="space-y-3">
            <Field label="Execution mode"><input className="input" readOnly value={readOnly ? "Read-only (project is read-only)" : "Isolated git worktree per task"} /></Field>
            <Field label="Review depth">
              <select className="input" value={reviewDepth} onChange={(e) => setReviewDepth(e.target.value)}>
                <option value="standard">Standard — reviewer prefers a different provider</option>
                <option value="independent">Independent — reviewer must use a different provider when available</option>
                <option value="none">Same as standard (review phase is part of the workflow)</option>
              </select>
            </Field>
            <Field label="Approval policy">
              <select className="input" value={approval} onChange={(e) => setApproval(e.target.value)}>
                <option value="risky">Risky actions only (merge, spending, destructive)</option>
                <option value="every_phase">Every phase</option>
                <option value="never_merge">Never merge (leave changes on the branch)</option>
              </select>
            </Field>
            <Field label="Maximum known API spending (USD)" hint="Directly billed API usage only. Subscription CLI usage is not billed per call; 0 means no paid API use.">
              <input type="number" min={0} step={0.5} className="input" value={maxUsd} onChange={(e) => setMaxUsd(Number(e.target.value))} />
            </Field>
            <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={decompose} onChange={(e) => setDecompose(e.target.checked)} />Let the planner decompose into dependent subtasks</label>
            <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={milestone} onChange={(e) => setMilestone(e.target.checked)} />Track as a project milestone</label>
            {mockEnabled && (
              <details className="rounded-lg border border-dashed border-fuchsia-400 p-2">
                <summary className="cursor-pointer text-sm">Mock scenario (testing only) <MockBadge /></summary>
                <textarea className="input mt-2 min-h-24 font-mono text-xs" value={mockJson} onChange={(e) => setMockJson(e.target.value)}
                  placeholder='{"files": {"hello.txt": "hi"}, "reviews": ["approve"]}' aria-label="Mock scenario JSON" />
              </details>
            )}
          </div>
        </Card>
      </div>
      <ErrorBox error={err} />
      <div className="flex justify-end"><button className="btn btn-primary px-6 py-2" disabled={busy || !goal.trim()}>{busy ? "Submitting…" : "Start task"}</button></div>
    </form>
  );
}
