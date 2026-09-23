import type { InspectResult, Metrics, Signal, Verdict } from "../api";

export function VerdictBadge({ verdict }: { verdict: Verdict }) {
  const icon = verdict === "blocked" ? "⨯" : verdict === "flagged" ? "!" : "✓";
  return (
    <span className={`badge ${verdict}`}>
      <span aria-hidden>{icon}</span>
      {verdict}
    </span>
  );
}

export function RouteTag({ route }: { route: InspectResult["route"] }) {
  if (!route) return <span className="tag none">— not routed</span>;
  return (
    <span className={`tag ${route}`}>
      {route === "cheap" ? "↓" : "↑"} {route}
    </span>
  );
}

/**
 * One question's answer as a meter. `revealed` drives the fill animation, so
 * the parent can stagger a whole panel into view.
 */
export function SignalMeter({
  signal,
  revealed,
  delay,
  ticks,
}: {
  signal: Signal;
  revealed: boolean;
  delay: number;
  ticks?: { flag?: number; block?: number };
}) {
  return (
    <div className={`signal${revealed ? " revealed" : ""}`}>
      <div className="signal-top">
        <span className="signal-name">{signal.name}</span>
        <span className="signal-kind">{signal.kind}</span>
        <span className="spacer" />
        <span className="signal-val">{signal.display}</span>
      </div>
      <div className={`meter${revealed ? " revealed" : ""}`}>
        <div
          className={`fill ${signal.severity}`}
          style={{
            // @ts-expect-error custom property
            "--w": `${Math.round(signal.value * 100)}%`,
            transitionDelay: `${delay}ms`,
          }}
        />
        {ticks?.flag !== undefined && (
          <div className="tick flag" style={{ left: `${ticks.flag * 100}%` }} />
        )}
        {ticks?.block !== undefined && (
          <div className="tick block" style={{ left: `${ticks.block * 100}%` }} />
        )}
      </div>
      {signal.detail && <div className="signal-detail">{signal.detail}</div>}
    </div>
  );
}

export function MetricsBar({ m }: { m: Metrics }) {
  const cells: { k: string; v: string; s?: string; cls?: string }[] = [
    { k: "Inspected", v: String(m.total), s: "prompts seen" },
    { k: "Blocked", v: String(m.blocked), s: `${m.pct_blocked}% of traffic`, cls: "blocked" },
    { k: "Flagged", v: String(m.flagged), s: "allowed, logged", cls: "flagged" },
    { k: "Routed cheap", v: `${m.pct_cheap}%`, s: `${m.cheap} of ${m.cheap + m.frontier}`, cls: "cheap" },
    { k: "Cost avoided", v: `$${m.est_cost_saved_usd.toFixed(4)}`, s: "vs frontier-on-all", cls: "clean" },
    { k: "Added latency", v: `${Math.round(m.avg_added_latency_ms)}ms`, s: `p95 ${Math.round(m.p95_added_latency_ms)}ms` },
  ];
  return (
    <div className="metrics">
      {cells.map((c) => (
        <div className="metric" key={c.k}>
          <div className="k">{c.k}</div>
          <div className={`v ${c.cls ?? ""}`}>{c.v}</div>
          {c.s && <div className="s">{c.s}</div>}
        </div>
      ))}
    </div>
  );
}

export function TrafficLog({ records }: { records: InspectResult[] }) {
  return (
    <div className="traffic">
      <div className="section-title">Live traffic</div>
      <div className="log">
        <div className="log-head">
          <div className="t">Time</div>
          <div>Prompt</div>
          <div>Firewall</div>
          <div>Route</div>
          <div className="lat">Latency</div>
          <div className="saved">Saved</div>
        </div>
        {records.length === 0 ? (
          <div className="empty">No traffic yet — inspect a prompt to begin.</div>
        ) : (
          records.map((r) => (
            <div className="row" key={r.id}>
              <div className="t">
                {new Date(r.ts * 1000).toLocaleTimeString([], {
                  hour: "2-digit",
                  minute: "2-digit",
                  second: "2-digit",
                })}
              </div>
              <div className="p" title={r.prompt}>{r.prompt}</div>
              <div><VerdictBadge verdict={r.verdict} /></div>
              <div><RouteTag route={r.route} /></div>
              <div className="lat">{Math.round(r.timing.total_ms)}ms</div>
              <div className="saved">
                {r.cost.est_saved_usd > 0 ? `$${r.cost.est_saved_usd.toFixed(4)}` : "—"}
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
