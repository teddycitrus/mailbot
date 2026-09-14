import { useCallback, useEffect, useRef, useState } from "react";
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

/** How often the running dashboard re-reads the database. The send and build
 *  jobs are separate processes, so a tab left open would otherwise keep showing
 *  whatever was true when it was opened. */
const POLL_MS = 15_000;

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

/** Age of the numbers on screen. Keeps its own timer so a ticking clock does
 *  not re-render the tables and charts every few seconds. */
function LastUpdated({ at, stale }: { at: Date | null; stale: boolean }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 5000);
    return () => window.clearInterval(id);
  }, []);
  if (!at) return null;
  const secs = Math.max(0, Math.round((now - at.getTime()) / 1000));
  const ago =
    secs < 60
      ? `${secs}s ago`
      : secs < 3600
        ? `${Math.floor(secs / 60)}m ago`
        : at.toLocaleTimeString();
  return (
    <span
      className={`hidden text-xs sm:inline ${stale ? "text-negative" : "text-muted"}`}
      title={at.toLocaleString()}
    >
      {stale ? "offline, last update " : "updated "}
      {ago}
    </span>
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
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [stale, setStale] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const inFlight = useRef(false);

  /** Pulls the numbers only. Deliberately leaves `page` alone: a background
   *  poll must never move the user off the page they are reading. */
  const loadData = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setRefreshing(true);
    try {
      const [d, h] = await Promise.all([api.getDashboard(), api.getHealth()]);
      setData(d);
      setChecks(h.checks);
      setLastUpdated(new Date());
      setStale(false);
    } catch {
      setStale(true); // keep the last good numbers up, but say they are stale
    } finally {
      inFlight.current = false;
      setRefreshing(false);
    }
  }, []);

  const refresh = useCallback(async () => {
    try {
      const s = await api.getSetup();
      setSetup(s);
      setPage(s.configured ? "overview" : "setup");
      if (s.configured) await loadData();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not reach the local service.");
    } finally {
      setLoading(false);
    }
  }, [loadData]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const configured = setup?.configured ?? false;

  useEffect(() => {
    if (!configured) return;
    const tick = () => {
      if (!document.hidden) void loadData();
    };
    const id = window.setInterval(tick, POLL_MS);
    // A hidden tab stops polling; the same handler catches it up on return.
    document.addEventListener("visibilitychange", tick);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [configured, loadData]);

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

  const showLive = configured && page !== "setup";

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
          {showLive && (
            <>
              <LastUpdated at={lastUpdated} stale={stale} />
              <button
                type="button"
                onClick={() => void loadData()}
                disabled={refreshing}
                className="rounded-md border border-border px-3 py-1.5 text-xs font-medium
                           text-muted hover:text-text disabled:opacity-50"
              >
                {refreshing ? "Refreshing" : "Refresh"}
              </button>
            </>
          )}
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
