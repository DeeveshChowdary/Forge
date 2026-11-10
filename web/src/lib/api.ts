// Thin client for the Forge API. Mutating requests carry X-Forge-Client (CSRF guard).

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-Forge-Client": "web" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  if (!res.ok) {
    let msg = text;
    try {
      const j = JSON.parse(text);
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* plain text */
    }
    throw new ApiError(res.status, msg || res.statusText);
  }
  const ct = res.headers.get("content-type") ?? "";
  return (ct.includes("application/json") ? JSON.parse(text) : text) as T;
}

export const api = {
  get: <T = any>(p: string) => request<T>("GET", p),
  post: <T = any>(p: string, b: unknown = {}) => request<T>("POST", p, b),
  put: <T = any>(p: string, b: unknown) => request<T>("PUT", p, b),
  patch: <T = any>(p: string, b: unknown) => request<T>("PATCH", p, b),
  del: <T = any>(p: string) => request<T>("DELETE", p),
};

export const q = (params: Record<string, string | number | boolean | undefined | null>) => {
  const s = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") s.set(k, String(v));
  const str = s.toString();
  return str ? `?${str}` : "";
};

// ---- types (subset of what the API returns) ----
export type TaskState =
  | "CREATED" | "PLANNING" | "READY" | "EXECUTING" | "TESTING" | "REVIEWING" | "COMPLETED"
  | "FAILED" | "BLOCKED" | "PAUSED" | "CANCELLED" | "AWAITING_APPROVAL";

export interface Project {
  id: string; name: string; root_path: string; description: string; origin: string; status: string;
  instructions: string; config: any; created_at: number; updated_at: number; task_counts?: Record<string, number>;
  git?: any; instruction_files?: any[]; milestones?: any[]; usage?: any;
}

export interface Task {
  id: string; project_id: string; project_name?: string; parent_id: string | null; title: string; objective: string;
  acceptance_criteria: string[]; category: string; workflow: string; state: TaskState; phase: string | null;
  complexity: string; criticality: string; routing_mode: string; routing_overrides: any; review_depth: string;
  approval_policy: string; execution_mode: string; budget: any; assigned_provider: string | null;
  assigned_model: string | null; attempts: number; review_cycle: number; worktree_path: string | null;
  branch: string | null; base_commit: string | null; disposition: string | null; error: string | null;
  pause_requested: number; cancel_requested: number; metadata: any; depends_on: string[];
  created_at: number; updated_at: number; started_at: number | null; finished_at: number | null;
}

export interface Run {
  id: string; task_id: string; phase: string; role: string; kind: string; provider: string | null;
  model: string | null; resolved_model: string | null; effort: string | null; state: string; attempt: number;
  review_cycle: number; session_id: string | null; output_text: string; structured_output: any; usage: any;
  exit_code: number | null; error: string | null; started_at: number; ended_at: number | null; is_mock: boolean;
  prompt_chars: number;
}

export interface Artifact {
  id: string; task_id: string; run_id: string | null; kind: string; title: string; content: string;
  content_type: string; data?: any; created_at: number;
}

export interface ForgeEvent {
  id: number; ts: number; project_id: string | null; task_id: string | null; run_id: string | null;
  type: string; level: string; message: string; data: any;
}

export interface Approval {
  id: string; project_id: string; task_id: string | null; kind: string; title: string; detail: string;
  payload: any; status: string; created_at: number; decided_at: number | null; decision_note: string | null;
}
