import { useCallback, useEffect, useRef, useState } from "react";
import {
  getHealth,
  getMetrics,
  getPolicy,
  inspect,
  type Health,
  type InspectResult,
  type Metrics,
  type PolicyConfig,
} from "./api";
import { MetricsBar, TrafficLog } from "./components/Bits";
import { Pipeline, type Phase } from "./components/Pipeline";
import { DEMO_PROMPTS, GROUP_LABEL } from "./demoPrompts";

/**
 * Phase durations. The request is fired immediately; the walkthrough only
 * advances past `scan` once the real result is in hand, so nothing on screen
 * is ever invented — the stagger just makes a ~300ms decision legible.
 */
const DUR: Record<Exclude<Phase, "idle" | "done">, number> = {
  travel: 620,
  scan: 420,   // minimum; extended until the response lands
  reveal: 900,
  verdict: 620,
  route: 900,
};

const EMPTY_METRICS: Metrics = {
  total: 0, blocked: 0, flagged: 0, clean: 0, cheap: 0, frontier: 0,
  pct_cheap: 0, pct_blocked: 0, avg_added_latency_ms: 0,
  p95_added_latency_ms: 0, est_cost_saved_usd: 0, degraded: 0,
};

export default function App() {
  const [prompt, setPrompt] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [result, setResult] = useState<InspectResult | null>(null);
  const [records, setRecords] = useState<InspectResult[]>([]);
  const [metrics, setMetrics] = useState<Metrics>(EMPTY_METRICS);
  const [health, setHealth] = useState<Health | null>(null);
  const [policy, setPolicy] = useState<PolicyConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timers = useRef<number[]>([]);

  const busy = phase !== "idle" && phase !== "done";

  // --- bootstrap: wake the backend, wait for the model ------------------
  useEffect(() => {
    let stop = false;
    const poll = async () => {
      try {
        const h = await getHealth();
        if (stop) return;
        setHealth(h);
        setError(null);
        if (!h.model_ready) {
          window.setTimeout(poll, 1500);
        } else if (!policy) {
          setPolicy(await getPolicy());
          setMetrics(await getMetrics());
        }
      } catch {
        if (stop) return;
        setHealth(null);
        setError("Cannot reach the gateway on :8000 — is the backend running?");
        window.setTimeout(poll, 2500);
      }
    };
    poll();
    return () => { stop = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => () => timers.current.forEach(clearTimeout), []);

  const run = useCallback(async (text: string) => {
    const value = text.trim();
    if (!value || busy) return;

    timers.current.forEach(clearTimeout);
    timers.current = [];
    setError(null);
    setResult(null);
    setPhase("travel");

    const startedAt = Date.now();
    const request = inspect(value);

    const step = (p: Phase, delay: number) =>
      timers.current.push(window.setTimeout(() => setPhase(p), delay));

    step("scan", DUR.travel);

    try {
      const res = await request;
      // Hold `scan` until the packet has actually arrived at the gate and the
      // gate has visibly pulsed, then walk through the rest.
      const elapsed = Date.now() - startedAt;
      const floor = DUR.travel + DUR.scan;
      const wait = Math.max(0, floor - elapsed);

      setResult(res);
      step("reveal", wait);
      step("verdict", wait + DUR.reveal);
      step("route", wait + DUR.reveal + DUR.verdict);
      timers.current.push(window.setTimeout(() => {
        setPhase("done");
        setRecords((prev) => [res, ...prev].slice(0, 60));
        getMetrics().then(setMetrics).catch(() => {});
      }, wait + DUR.reveal + DUR.verdict + DUR.route));
    } catch (e) {
      timers.current.forEach(clearTimeout);
      setPhase("idle");
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [busy]);

  const modelReady = health?.model_ready ?? false;
  const statusLabel = !health
    ? "gateway unreachable"
    : modelReady
      ? `Laya ready · loaded in ${health.load_seconds}s`
      : "waking the watchman…";
  const dotClass = !health ? "down" : modelReady ? "live" : "warn";

  return (
    <div className="shell">
      <header className="masthead">
        <div className="mark" aria-hidden>H</div>
        <div>
          <div className="wordmark">HEIMDALL</div>
          <div className="tagline">prompt firewall &amp; model router</div>
        </div>
        <div className="spacer" />
        <div className="status">
          <span className={`dot ${dotClass}`} />
          {statusLabel}
        </div>
      </header>

      <MetricsBar m={metrics} />

      <div className="composer">
        <div className="composer-row">
          <textarea
            className="prompt"
            value={prompt}
            placeholder="Type a prompt to send across the bridge…"
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) run(prompt);
            }}
            disabled={!modelReady}
          />
          <button
            className="inspect"
            onClick={() => run(prompt)}
            disabled={busy || !prompt.trim() || !modelReady}
          >
            {busy ? "Inspecting…" : "Inspect ⌘↵"}
          </button>
        </div>

        <div className="chips">
          <span className="chips-label">Try</span>
          {DEMO_PROMPTS.map((d) => (
            <button
              key={d.label}
              className={`chip ${d.group}`}
              title={`${GROUP_LABEL[d.group]} — ${d.text}`}
              disabled={busy || !modelReady}
              onClick={() => { setPrompt(d.text); run(d.text); }}
            >
              {d.label}
            </button>
          ))}
        </div>

        {error && <div className="error-bar">{error}</div>}
      </div>

      <Pipeline phase={phase} result={result} prompt={prompt} policy={policy} />

      <div className="pill-note">
        <span aria-hidden>⚡</span>
        inspected locally by an open-source System-1 model — no frontier call needed to screen or route
      </div>

      <TrafficLog records={records} />

      <footer className="footnote">
        <div className="lead">
          Heimdall is one layer of defense-in-depth, not a silver bullet.
        </div>
        A prompt firewall reduces risk; it does not eliminate it. Keep system-prompt
        hardening, output filtering, and least-privilege tool access in place. Verdicts
        are confidence-gated and every error path fails safe — toward review, and toward
        the frontier model. Thresholds shown are starting points; calibrate them against
        a labelled sample of your own traffic.
        <br />
        <br />
        Screening and routing by{" "}
        <a href="https://huggingface.co/convaiinnovations/laya" target="_blank" rel="noreferrer">
          Laya
        </a>{" "}
        — an open-source System-1 decision model by Convai Innovations, Apache-2.0.
      </footer>
    </div>
  );
}
