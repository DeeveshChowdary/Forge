import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import "./index.css";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";
import Projects from "./pages/Projects";
import ProjectWorkspace from "./pages/ProjectWorkspace";
import NewTask from "./pages/NewTask";
import TaskDetail from "./pages/TaskDetail";
import Live from "./pages/Live";
import Approvals from "./pages/Approvals";
import Agents from "./pages/Agents";
import Settings from "./pages/Settings";
import Coordination from "./pages/Coordination";

function NotFound() {
  return <div className="p-8 text-center muted">Page not found.</div>;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Dashboard />} />
          <Route path="projects" element={<Projects />} />
          <Route path="projects/:pid" element={<ProjectWorkspace />} />
          <Route path="projects/:pid/:tab" element={<ProjectWorkspace />} />
          <Route path="new" element={<NewTask />} />
          <Route path="tasks/:tid" element={<TaskDetail />} />
          <Route path="live" element={<Live />} />
          <Route path="approvals" element={<Approvals />} />
          <Route path="agents" element={<Agents />} />
          <Route path="settings" element={<Settings />} />
          <Route path="coordination" element={<Coordination />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
