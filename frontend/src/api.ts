const BASE = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export type Verdict = "clean" | "flagged" | "blocked";
export type Route = "cheap" | "frontier";

export interface Signal {
  name: string;
  kind: "noul" | "score" | "choice";
  value: number;        // 0..1, comparable across primitives
  confidence: number;
  display: string;
  triggered: boolean;
  severity: Verdict;
  detail: string | null;
}

export interface InspectResult {
  id: string;
  ts: number;
  prompt: string;
  verdict: Verdict;
  route: Route | null;
  firewall: {
    verdict: Verdict;
    reasons: string[];
    signals: Signal[];
    confidences: Record<string, number>;
    topic: string | null;
    degraded: boolean;
  };
  router: {
    route: Route;
    reasons: string[];
    signals: Signal[];
    difficulty: number | null;
    difficulty_label: string | null;
    domain: string | null;
    confidence: number;
    degraded: boolean;
    applied: boolean;
  };
  timing: { laya_ms: number; total_ms: number };
  cost: {
    est_saved_usd: number;
    chosen_model: string | null;
    frontier_model: string;
  };
  degraded: boolean;
}

export interface Metrics {
  total: number;
  blocked: number;
  flagged: number;
  clean: number;
  cheap: number;
  frontier: number;
  pct_cheap: number;
  pct_blocked: number;
  avg_added_latency_ms: number;
  p95_added_latency_ms: number;
  est_cost_saved_usd: number;
  degraded: number;
}

export interface PolicyConfig {
  firewall: {
    block_threshold: number;
    flag_threshold: number;
    blocking_categories: string[];
    flag_only_categories: string[];
    harm_block_level: number;
    harm_flag_level: number;
    min_block_confidence: number;
  };
  router: {
    min_easy_mass: number;
    sensitive_to_frontier_at: number;
    needs_tools_to_frontier_at: number;
    frontier_when_uncertain: boolean;
    flagged_to_frontier: boolean;
  };
  models: {
    cheap: { id: string; usd_per_mtok: number };
    frontier: { id: string; usd_per_mtok: number };
  };
  harm_levels: string[];
  difficulty_levels: string[];
}

export interface Health {
  ok: boolean;
  model_ready: boolean;
  model: string;
  load_seconds: number;
  error: string | null;
  single_pass: boolean;
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json() as Promise<T>;
}

export const getHealth = () => get<Health>("/health");
export const getMetrics = () => get<Metrics>("/metrics");
export const getPolicy = () => get<PolicyConfig>("/policy");

export async function inspect(prompt: string): Promise<InspectResult> {
  const res = await fetch(`${BASE}/inspect`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? `inspect -> ${res.status}`);
  }
  return res.json();
}
