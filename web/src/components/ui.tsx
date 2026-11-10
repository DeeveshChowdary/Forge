import { useEffect, useRef, useState, type ReactNode } from "react";
import type { TaskState } from "../lib/api";
import { duration } from "../lib/format";
import { useNow } from "../lib/hooks";

const STATE_STYLE: Record<string, string> = {
  CREATED: "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
  READY: "bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-300",
  PLANNING: "bg-violet-100 text-violet-800 dark:bg-violet-950 dark:text-violet-300",
  EXECUTING: "bg-orange-100 text-orange-800 dark:bg-orange-950 dark:text-orange-300",
  TESTING: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  REVIEWING: "bg-indigo-100 text-indigo-800 dark:bg-indigo-950 dark:text-indigo-300",
  COMPLETED: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300",
  FAILED: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300",
  BLOCKED: "bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300",
  PAUSED: "bg-zinc-200 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
  CANCELLED: "bg-zinc-200 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400",
  AWAITING_APPROVAL: "bg-yellow-100 text-yellow-900 dark:bg-yellow-950 dark:text-yellow-300",
};
const ACTIVE = new Set(["PLANNING", "EXECUTING", "TESTING", "REVIEWING"]);

export function StateBadge({ state }: { state: TaskState | string }) {
  return (
    <span className={`pill ${STATE_STYLE[state] ?? STATE_STYLE.CREATED}`} data-testid="state-badge">
      {ACTIVE.has(state) && <span className="size-1.5 animate-pulse rounded-full bg-current" aria-hidden />}
      {state.replace("_", " ")}
    </span>
  );
}

const RUN_STYLE: Record<string, string> = {
  running: STATE_STYLE.EXECUTING, succeeded: STATE_STYLE.COMPLETED, failed: STATE_STYLE.FAILED,
  cancelled: STATE_STYLE.CANCELLED, interrupted: STATE_STYLE.BLOCKED,
};
export function RunBadge({ state }: { state: string }) {
  return <span className={`pill ${RUN_STYLE[state] ?? STATE_STYLE.CREATED}`}>{state}</span>;
}

export function Badge({ children, tone = "zinc", title }: { children: ReactNode; tone?: string; title?: string }) {
  const tones: Record<string, string> = {
    zinc: "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
    green: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300",
    red: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300",
    yellow: "bg-yellow-100 text-yellow-900 dark:bg-yellow-950 dark:text-yellow-300",
    blue: "bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-300",
    orange: "bg-orange-100 text-orange-800 dark:bg-orange-950 dark:text-orange-300",
    violet: "bg-violet-100 text-violet-800 dark:bg-violet-950 dark:text-violet-300",
  };
  return <span title={title} className={`pill ${tones[tone] ?? tones.zinc}`}>{children}</span>;
}

/** Every piece of mock-provider activity is labelled so it is never mistaken for real agent work. */
export function MockBadge() {
  return (
    <span className="pill border border-dashed border-fuchsia-500 bg-fuchsia-50 text-fuchsia-700 dark:bg-fuchsia-950 dark:text-fuchsia-300"
      title="Produced by the deterministic mock provider, not a real model">
      MOCK
    </span>
  );
}

export function AuthBadge({ state }: { state?: string }) {
  const map: Record<string, [string, string]> = {
    ok: ["green", "authenticated (verified)"], unverified: ["yellow", "logged in (not yet verified)"],
    expired: ["red", "auth expired"], missing: ["red", "not logged in"], unknown: ["zinc", "unknown"],
  };
  const [tone, label] = map[state ?? "unknown"] ?? map.unknown;
  return <Badge tone={tone}>{label}</Badge>;
}

export function ModelStatus({ status }: { status: string }) {
  const tone = status === "verified" ? "green" : status === "failed" ? "red" : "yellow";
  return <Badge tone={tone}>{status}</Badge>;
}

export function Card({ title, actions, children, className = "", bodyClass = "p-4" }:
  { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; bodyClass?: string }) {
  return (
    <section className={`card ${className}`}>
      {title && <header className="card-h"><h2>{title}</h2><div className="flex items-center gap-2">{actions}</div></header>}
      <div className={bodyClass}>{children}</div>
    </section>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 p-4 text-sm muted" role="status">
      <span className="size-4 animate-spin rounded-full border-2 border-zinc-300 border-t-orange-500" aria-hidden />
      {label}…
    </div>
  );
}

export function ErrorBox({ error, onRetry }: { error: string | null; onRetry?: () => void }) {
  if (!error) return null;
  return (
    <div role="alert" className="flex items-start justify-between gap-3 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/50 dark:text-red-200">
      <span className="break-words">{error}</span>
      {onRetry && <button className="btn btn-sm" onClick={onRetry}>Retry</button>}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="p-6 text-center text-sm muted">{children}</div>;
}

export function Elapsed({ from, to }: { from: number | null; to?: number | null }) {
  const now = useNow(1000);
  if (!from) return <span>—</span>;
  return <span className="tabular-nums">{duration((to ?? now) - from)}</span>;
}

export function Tabs({ tabs, active, onChange }: { tabs: { key: string; label: ReactNode }[]; active: string; onChange: (k: string) => void }) {
  return (
    <div role="tablist" className="flex gap-1 overflow-x-auto border-b border-zinc-200 dark:border-zinc-800">
      {tabs.map((t) => (
        <button key={t.key} role="tab" aria-selected={active === t.key} onClick={() => onChange(t.key)}
          className={`-mb-px whitespace-nowrap border-b-2 px-3 py-2 text-sm font-medium ${active === t.key
            ? "border-orange-500 text-orange-700 dark:text-orange-400"
            : "border-transparent muted hover:text-zinc-900 dark:hover:text-zinc-100"}`}>
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function Modal({ open, onClose, title, children, wide }: { open: boolean; onClose: () => void; title: string; children: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    ref.current?.querySelector<HTMLElement>("input,textarea,select,button")?.focus();
    return () => window.removeEventListener("keydown", h);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 p-4 sm:pt-16" onMouseDown={onClose}>
      <div ref={ref} role="dialog" aria-modal="true" aria-label={title}
        className={`card w-full ${wide ? "max-w-4xl" : "max-w-lg"}`} onMouseDown={(e) => e.stopPropagation()}>
        <header className="card-h"><h2>{title}</h2><button className="btn btn-sm" onClick={onClose} aria-label="Close">✕</button></header>
        <div className="p-4">{children}</div>
      </div>
    </div>
  );
}

export function DiffView({ diff, maxHeight = "32rem" }: { diff: string; maxHeight?: string }) {
  if (!diff.trim()) return <Empty>No changes.</Empty>;
  return (
    <pre className="code" style={{ maxHeight }} data-testid="diff">
      {diff.split("\n").map((l, i) => {
        const cls = l.startsWith("+++") || l.startsWith("---") ? "text-zinc-500"
          : l.startsWith("+") ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
          : l.startsWith("-") ? "bg-red-500/10 text-red-700 dark:text-red-300"
          : l.startsWith("@@") ? "text-sky-600 dark:text-sky-400"
          : l.startsWith("diff ") || l.startsWith("commit ") ? "font-semibold text-zinc-700 dark:text-zinc-300" : "";
        return <div key={i} className={cls}>{l || " "}</div>;
      })}
    </pre>
  );
}

function List({ items }: { items?: string[] }) {
  if (!items?.length) return <span className="muted">none</span>;
  return <ul className="ml-4 list-disc space-y-0.5">{items.map((s, i) => <li key={i}>{s}</li>)}</ul>;
}

/** Renders the structured handoff artifacts (plan/handoff/review/report/proposal/test_report). */
export function StructuredView({ kind, data }: { kind: string; data: any }) {
  if (!data || typeof data !== "object") return <pre className="code whitespace-pre-wrap">{String(data ?? "")}</pre>;
  const row = (label: string, v: ReactNode) => (
    <div className="grid gap-1 sm:grid-cols-[9rem_1fr]"><div className="text-xs font-medium muted">{label}</div><div className="text-sm">{v}</div></div>
  );
  if (kind === "review") {
    return (
      <div className="space-y-2">
        {row("Verdict", <Badge tone={data.verdict === "approve" ? "green" : "yellow"}>{data.verdict}</Badge>)}
        {row("Summary", data.summary)}
        {row("Issues", data.issues?.length ? (
          <ul className="space-y-1">{data.issues.map((i: any, k: number) => (
            <li key={k} className="rounded border border-zinc-200 p-2 dark:border-zinc-800">
              <Badge tone={["blocker", "major"].includes(i.severity) ? "red" : "zinc"}>{i.severity}</Badge>{" "}
              <span className="font-mono text-xs">{i.file}</span>
              <div>{i.description}</div>{i.suggested_fix && <div className="text-xs muted">Fix: {i.suggested_fix}</div>}
            </li>))}</ul>) : <span className="muted">none</span>)}
      </div>
    );
  }
  if (kind === "test_report") {
    return (
      <div className="space-y-2">
        {row("Result", data.skipped ? <Badge>not configured</Badge> : <Badge tone={data.passed ? "green" : "red"}>{data.passed ? "passed" : "failed"}</Badge>)}
        {row("Summary", data.summary)}
        {data.command && row("Command", <code className="text-xs">{data.command}</code>)}
        {data.duration_s != null && row("Duration", `${data.duration_s}s (exit ${data.exit_code})`)}
        {data.output_tail && <pre className="code max-h-64 whitespace-pre-wrap">{data.output_tail}</pre>}
      </div>
    );
  }
  const fields: [string, string][] = [
    ["title", "Title"], ["summary", "Summary"], ["rationale", "Rationale"], ["steps", "Steps"],
    ["files_to_change", "Files to change"], ["files_changed", "Files changed"], ["tests_added", "Tests added"],
    ["acceptance_criteria", "Acceptance"], ["test_plan", "Test plan"], ["findings", "Findings"],
    ["evidence", "Evidence"], ["risks", "Risks"], ["success_criteria", "Success criteria"],
    ["next_steps", "Next steps"], ["notes_for_reviewer", "Notes for reviewer"], ["open_issues", "Open issues"],
    ["estimated_effort", "Effort"], ["confidence", "Confidence"],
  ];
  const shown = fields.filter(([k]) => data[k] !== undefined && !(Array.isArray(data[k]) && !data[k].length && !["steps"].includes(k)));
  return (
    <div className="space-y-2">
      {shown.map(([k, label]) => <div key={k}>{row(label, Array.isArray(data[k]) ? <List items={data[k]} /> : String(data[k]))}</div>)}
      {data.subtasks?.length > 0 && row("Subtasks", <List items={data.subtasks.map((s: any) => `${s.title}: ${s.objective}`)} />)}
    </div>
  );
}

export function CopyText({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button className="btn btn-sm" onClick={() => { navigator.clipboard?.writeText(text); setDone(true); setTimeout(() => setDone(false), 1200); }}>
      {done ? "Copied" : "Copy"}
    </button>
  );
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: ReactNode }) {
  return (
    <label className="block">
      <span className="label">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs muted">{hint}</span>}
    </label>
  );
}
