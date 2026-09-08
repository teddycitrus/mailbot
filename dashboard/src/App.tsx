import { useCallback, useEffect, useState } from "react";
import * as api from "./api";
import DashboardView from "./components/DashboardView";
import Sidebar, { type NavItem } from "./components/Sidebar";
import SetupView from "./components/SetupView";
import type { DashboardData, HealthCheck, SetupState } from "./types";

/** App shell.
 *
 *  Two states, decided by the server rather than by the client guessing: until
 *  the required configuration exists, the setup console is the whole app. Once
 *  it does, the progress dashboard takes over and setup stays reachable from
 *  the sidebar.
 */

type Theme = "light" | "dark" | "system";

function useTheme() {
  const [theme, setTheme] = useState<Theme>(
    () => (localStorage.getItem("mailbot-theme") as Theme) || "system",
  );
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    try {
      localStorage.setItem("mailbot-theme", theme);
    } catch {
      /* private browsing: the choice just does not persist */
    }
  }, [theme]);
  return { theme, setTheme };
}

function ThemeToggle({ theme, setTheme }: ReturnType<typeof useTheme>) {
  const order: Theme[] = ["system", "light", "dark"];
  const next = order[(order.indexOf(theme) + 1) % order.length];
  return (
    <button
      type="button"
      onClick={() => setTheme(next)}
      className="rounded-md border border-border px-3 py-1.5 text-xs font-medium
                 text-muted hover:text-text"
      aria-label={`Theme: ${theme}. Switch to ${next}.`}
    >
      {theme === "system" ? "Auto" : theme === "light" ? "Light" : "Dark"}
    </button>
  );
}

export default function App() {
  const themeState = useTheme();
  const [setup, setSetup] = useState<SetupState | null>(null);
  const [data, setData] = useState<DashboardData | null>(null);
  const [checks, setChecks] = useState<HealthCheck[]>([]);
  const [page, setPage] = useState("overview");
  const [navOpen, setNavOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const s = await api.getSetup();
      setSetup(s);
      setPage(s.configured ? "overview" : "setup");
      if (s.configured) {
        const [d, h] = await Promise.all([api.getDashboard(), api.getHealth()]);
        setData(d);
        setChecks(h.checks);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not reach the local service.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const save = async (values: Record<string, string>) => {
    setSaving(true);
    setError("");
    try {
      setSetup(await api.saveSetup(values));
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed.");
    } finally {
      setSaving(false);
    }
  };

  const upload = async (file: File) => {
    setError("");
    try {
      setSetup(await api.uploadResume(file));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Upload failed.");
    }
  };

  const nav: NavItem[] = setup?.configured
    ? [
        { id: "overview", label: "Overview" },
        {
          id: "pipeline",
          label: "Pipeline",
          children: [
            { id: "contacts", label: "Contacts", badge: data?.contacts.length },
            { id: "queue", label: "Queue" },
          ],
        },
        { id: "setup", label: "Configuration" },
      ]
    : [{ id: "setup", label: "Setup" }];

  return (
    <div className="grid min-h-full grid-cols-1 lg:grid-cols-[16rem_1fr]">
      <a href="#main" className="skip-link">
        Skip to content
      </a>

      <Sidebar
        items={nav}
        activeId={page}
        onSelect={(id) => {
          setPage(id);
          setNavOpen(false);
        }}
        open={navOpen}
        onClose={() => setNavOpen(false)}
      />

      <div className="flex min-w-0 flex-col">
        <header className="flex items-center gap-3 border-b border-border bg-surface px-4 py-3">
          <button
            type="button"
            onClick={() => setNavOpen((v) => !v)}
            aria-expanded={navOpen}
            aria-label="Toggle navigation"
            className="rounded-md border border-border p-1.5 lg:hidden"
          >
            <svg aria-hidden="true" viewBox="0 0 20 20" className="h-4 w-4" fill="currentColor">
              <path d="M3 5h14v2H3zM3 9h14v2H3zM3 13h14v2H3z" />
            </svg>
          </button>
          <h1 className="flex-1 truncate text-sm font-semibold">
            {page === "setup" ? "Configuration" : "Overview"}
          </h1>
          <ThemeToggle {...themeState} />
        </header>

        <main id="main" className="min-w-0 flex-1 p-4 lg:p-6">
          {loading && <p className="text-sm text-muted">Loading…</p>}

          {!loading && error && !setup && (
            <div role="alert" className="rounded-lg border border-negative/40 bg-negative/10 p-4">
              <p className="text-sm text-negative">{error}</p>
            </div>
          )}

          {!loading && setup && (page === "setup" || !setup.configured) && (
            <SetupView
              state={setup}
              onSave={save}
              onUploadResume={upload}
              saving={saving}
              error={error}
            />
          )}

          {!loading && setup?.configured && page !== "setup" && data && (
            <DashboardView data={data} checks={checks} />
          )}
        </main>
      </div>
    </div>
  );
}
