import { useState, type ReactNode } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useApi, useLiveReload, useStreamStatus } from "../lib/hooks";

const NAV = [
  { to: "/", label: "Dashboard", icon: "▦" },
  { to: "/projects", label: "Projects", icon: "◫" },
  { to: "/new", label: "New task", icon: "＋" },
  { to: "/live", label: "Live execution", icon: "◉" },
  { to: "/approvals", label: "Approvals", icon: "✓" },
  { to: "/agents", label: "Agent control", icon: "⚙" },
  { to: "/settings", label: "Settings", icon: "☰" },
];

function ThemeToggle() {
  const [dark, setDark] = useState(() => document.documentElement.classList.contains("dark"));
  const toggle = () => {
    const next = !dark;
    setDark(next);
    document.documentElement.classList.toggle("dark", next);
    try { localStorage.setItem("forge-theme", next ? "dark" : "light"); } catch { /* storage unavailable */ }
  };
  return (
    <button className="btn btn-sm" onClick={toggle} aria-label={`Switch to ${dark ? "light" : "dark"} theme`}>
      {dark ? "☀ Light" : "☾ Dark"}
    </button>
  );
}

function StatusBar() {
  const live = useStreamStatus();
  const { data, reload } = useApi<any>("/api/health");
  const { data: approvals, reload: reloadAp } = useApi<any[]>("/api/approvals");
  useLiveReload(() => { reload(); reloadAp(); }, (e) => e.type.startsWith("approval.") || e.type.startsWith("engine."));
  const engineAlive = data?.engine?.alive;
  return (
    <div className="flex flex-wrap items-center gap-3 text-xs">
      <span className="flex items-center gap-1.5" title="Live event stream">
        <span className={`size-2 rounded-full ${live ? "bg-emerald-500" : "bg-zinc-400"}`} />
        {live ? "Live" : "Reconnecting"}
      </span>
      <span className="flex items-center gap-1.5" title={engineAlive ? `Engine pid ${data?.engine?.pid}` : "No engine holds the execution lock"}>
        <span className={`size-2 rounded-full ${engineAlive ? "bg-emerald-500" : "bg-red-500"}`} />
        Engine {engineAlive ? "running" : "stopped"}
      </span>
      {approvals && approvals.length > 0 && (
        <NavLink to="/approvals" className="pill bg-yellow-100 text-yellow-900 dark:bg-yellow-950 dark:text-yellow-300">
          {approvals.length} approval{approvals.length > 1 ? "s" : ""} pending
        </NavLink>
      )}
    </div>
  );
}

export default function Layout({ children }: { children?: ReactNode }) {
  const [open, setOpen] = useState(false);
  const loc = useLocation();
  return (
    <div className="flex min-h-full">
      <aside className={`fixed inset-y-0 left-0 z-40 w-60 shrink-0 border-r border-zinc-200 bg-white p-3 transition-transform dark:border-zinc-800 dark:bg-zinc-900 md:static md:translate-x-0 ${open ? "translate-x-0" : "-translate-x-full"}`}>
        <div className="mb-6 flex items-center gap-2 px-2 pt-1">
          <span className="grid size-8 place-items-center rounded-lg bg-orange-600 font-bold text-white">F</span>
          <div>
            <div className="font-semibold leading-tight">Forge</div>
            <div className="text-[11px] muted">AI engineering harness</div>
          </div>
        </div>
        <nav className="space-y-0.5" aria-label="Main">
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.to === "/"} onClick={() => setOpen(false)}
              className={({ isActive }) => `flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm ${isActive
                ? "bg-orange-50 font-medium text-orange-800 dark:bg-orange-950/60 dark:text-orange-300"
                : "text-zinc-700 hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-800"}`}>
              <span className="w-4 text-center" aria-hidden>{n.icon}</span>{n.label}
            </NavLink>
          ))}
        </nav>
      </aside>
      {open && <div className="fixed inset-0 z-30 bg-black/30 md:hidden" onClick={() => setOpen(false)} />}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex items-center justify-between gap-3 border-b border-zinc-200 bg-white/80 px-4 py-2.5 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/80">
          <div className="flex items-center gap-3">
            <button className="btn btn-sm md:hidden" onClick={() => setOpen(true)} aria-label="Open navigation">☰</button>
            <StatusBar />
          </div>
          <ThemeToggle />
        </header>
        <main key={loc.pathname.split("/")[1]} className="mx-auto w-full max-w-7xl flex-1 p-4 sm:p-6">
          {children ?? <Outlet />}
        </main>
      </div>
    </div>
  );
}
