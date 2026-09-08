import type { DashboardData, HealthCheck, SetupState } from "./types";

/** Talks to the local Python process. Same origin in the packaged app, and a
 *  Vite proxy in development, so no base URL and no CORS either way. */

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(body || `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

export const getSetup = () => json<SetupState>("/api/setup");

export const saveSetup = (values: Record<string, string>) =>
  json<SetupState>("/api/setup", { method: "POST", body: JSON.stringify(values) });

export const getDashboard = () => json<DashboardData>("/api/dashboard");

export const getHealth = () => json<{ checks: HealthCheck[] }>("/api/health");

export async function uploadResume(file: File): Promise<SetupState> {
  const body = new FormData();
  body.append("resume", file);
  const res = await fetch("/api/resume", { method: "POST", body });
  if (!res.ok) throw new Error(await res.text());
  return (await res.json()) as SetupState;
}
