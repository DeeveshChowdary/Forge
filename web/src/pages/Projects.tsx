import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Badge, Card, Empty, ErrorBox, Field, Modal, Spinner } from "../components/ui";
import { api, q, type Project } from "../lib/api";
import { ago } from "../lib/format";
import { useApi, useLiveReload } from "../lib/hooks";

export function AddProjectDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const nav = useNavigate();
  const [mode, setMode] = useState<"create" | "import_folder" | "import_git">("import_folder");
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [url, setUrl] = useState("");
  const [description, setDescription] = useState("");
  const [testCommand, setTestCommand] = useState("");
  const [readOnly, setReadOnly] = useState(false);
  const [suggest, setSuggest] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const inspect = async () => {
    setErr(null);
    try {
      const s = await api.get(`/api/projects/suggest${q({ path })}`);
      setSuggest(s);
      if (!name) setName(s.name);
      if (!testCommand && s.suggested_test_command) setTestCommand(s.suggested_test_command);
    } catch (e: any) {
      setErr(e.message);
      setSuggest(null);
    }
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      const config: any = {};
      if (testCommand) config.test_command = testCommand;
      if (readOnly) config.permissions = { mode: "read_only" };
      const p = await api.post<Project>("/api/projects", { mode, name: name || undefined, path: path || undefined,
        url: url || undefined, description, config });
      onClose();
      nav(`/projects/${p.id}`);
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="Add a project">
      <form onSubmit={submit} className="space-y-4">
        <div role="radiogroup" aria-label="How to add" className="grid grid-cols-3 gap-2">
          {([["import_folder", "Import folder"], ["import_git", "Clone Git URL"], ["create", "Create new"]] as const).map(([k, l]) => (
            <button type="button" key={k} role="radio" aria-checked={mode === k} onClick={() => setMode(k)}
              className={`btn ${mode === k ? "btn-primary" : ""}`}>{l}</button>
          ))}
        </div>
        {mode === "import_git" ? (
          <Field label="Repository URL" hint="https or ssh URL; cloned into ~/.forge/projects. A local path also works.">
            <input className="input" required value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://github.com/org/repo.git" />
          </Field>
        ) : (
          <Field label={mode === "create" ? "Location (optional)" : "Folder path"}
            hint={mode === "create" ? "Default: ~/.forge/projects/<name>. A new git repository is initialised." : "Absolute path. Forge never modifies the folder on import."}>
            <div className="flex gap-2">
              <input className="input font-mono" required={mode === "import_folder"} value={path} onChange={(e) => setPath(e.target.value)}
                placeholder="/Users/you/code/my-app" />
              {mode === "import_folder" && <button type="button" className="btn" onClick={inspect} disabled={!path}>Inspect</button>}
            </div>
          </Field>
        )}
        {suggest && (
          <div className="rounded-lg bg-zinc-50 p-3 text-xs dark:bg-zinc-800/60">
            <div>Git repository: {suggest.is_git ? (suggest.has_commits ? "yes" : "yes (no commits → read-only)") : "no (→ read-only)"}</div>
            {suggest.instruction_files.length > 0 && (
              <div>Instruction files found: {suggest.instruction_files.join(", ")} — untrusted until you approve them.</div>
            )}
          </div>
        )}
        <Field label="Name"><input className="input" required={mode === "create"} value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="Description"><input className="input" value={description} onChange={(e) => setDescription(e.target.value)} /></Field>
        <Field label="Test command" hint={<>Run in task worktrees after implementation. <code>{"{python}"}</code> = Forge's Python (has pytest).</>}>
          <input className="input font-mono" value={testCommand} onChange={(e) => setTestCommand(e.target.value)} placeholder="{python} -m pytest -q" />
        </Field>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={readOnly} onChange={(e) => setReadOnly(e.target.checked)} />
          Read-only (agents may read but never write; no worktrees)
        </label>
        <ErrorBox error={err} />
        <div className="flex justify-end gap-2">
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" disabled={busy}>{busy ? "Adding…" : "Add project"}</button>
        </div>
      </form>
    </Modal>
  );
}

export default function Projects() {
  const { data, error, loading, reload } = useApi<Project[]>("/api/projects");
  const [open, setOpen] = useState(false);
  useLiveReload(reload, (e) => e.type.startsWith("project.") || e.type === "task.state");
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Projects</h1>
        <button className="btn btn-primary" onClick={() => setOpen(true)}>Add project</button>
      </div>
      <AddProjectDialog open={open} onClose={() => { setOpen(false); reload(); }} />
      <ErrorBox error={error} onRetry={reload} />
      {loading && !data ? <Spinner /> : (
        <Card bodyClass="p-0">
          {!data?.length ? <Empty>No projects yet. Add one to get started.</Empty> : (
            <div className="overflow-x-auto">
              <table className="tbl">
                <thead><tr><th>Name</th><th>Path</th><th>Origin</th><th>Mode</th><th>Status</th><th>Tasks</th><th>Updated</th></tr></thead>
                <tbody>
                  {data.map((p) => (
                    <tr key={p.id}>
                      <td><Link to={`/projects/${p.id}`} className="font-medium hover:underline">{p.name}</Link></td>
                      <td className="max-w-xs truncate font-mono text-xs">{p.root_path}</td>
                      <td>{p.origin.replace("_", " ")}</td>
                      <td>{p.config.permissions.mode === "read_only" ? <Badge tone="blue">read-only</Badge> : <Badge>worktree write</Badge>}</td>
                      <td>{p.status === "active" ? <Badge tone="green">active</Badge> : <Badge tone="yellow">{p.status}</Badge>}</td>
                      <td>{Object.values(p.task_counts ?? {}).reduce((a: number, b: any) => a + b, 0)}</td>
                      <td className="text-xs muted">{ago(p.updated_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
