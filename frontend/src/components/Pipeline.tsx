import type { InspectResult, PolicyConfig } from "../api";
import { SignalMeter } from "./Bits";

export type Phase = "idle" | "travel" | "scan" | "reveal" | "verdict" | "route" | "done";

const ORDER: Phase[] = ["idle", "travel", "scan", "reveal", "verdict", "route", "done"];
const at = (phase: Phase, min: Phase) => ORDER.indexOf(phase) >= ORDER.indexOf(min);

/**
 * Where to draw the threshold ticks on a meter, in the meter's own 0..1 space.
 * Score signals are normalised by the policy's level count, so a level-based
 * threshold has to be normalised the same way to line up.
 */
function ticksFor(name: string, kind: string, p: PolicyConfig | null) {
  if (!p) return undefined;
  const fw = p.firewall;
  const rt = p.router;
  const scoreSpan = 3; // both score questions have 4 levels: 0..3

  if (kind === "noul") {
    // Both threat heads are drawn against the same thresholds. jailbreak only
    // blocks when prompt_injection corroborates it, but the tick still marks
    // where its own block threshold sits.
    if (name === fw.decisive_category || name === fw.corroborated_category) {
      return { flag: fw.flag_threshold, block: fw.block_threshold };
    }
    if ((fw.flag_only_categories ?? []).includes(name)) return { flag: fw.flag_threshold };
    if (name === "is_sensitive") return { flag: rt.sensitive_to_frontier_at };
    if (name === "needs_tools") return { flag: rt.needs_tools_to_frontier_at };
  }
  if (kind === "score") {
    if (name === "harm_severity") {
      return { flag: fw.harm_flag_level / scoreSpan, block: fw.harm_block_level / scoreSpan };
    }
    // The difficulty meter shows mass on moderate/hard, i.e. 1 - p_easy, so
    // its tick is the complement of the easy-mass the cheap model requires.
    if (name === "difficulty") return { flag: 1 - rt.min_easy_mass };
  }
  return undefined;
}

export function Pipeline({
  phase,
  result,
  prompt,
  policy,
}: {
  phase: Phase;
  result: InspectResult | null;
  prompt: string;
  policy: PolicyConfig | null;
}) {
  const scanning = phase === "scan" || phase === "travel";
  const revealed = at(phase, "reveal");
  const showStamp = at(phase, "verdict") && !!result;
  const routing = at(phase, "route") && !!result;

  const verdict = result?.verdict ?? null;
  const route = result?.route ?? null;
  const blocked = verdict === "blocked";

  // Packet position along the rail.
  let packetPos = "at-start";
  if (phase === "travel") packetPos = "at-gate";
  else if (at(phase, "scan")) packetPos = routing && !blocked ? "at-end" : "at-gate";

  const packetClasses = [
    "packet",
    phase !== "idle" ? "visible" : "",
    packetPos,
    routing && !blocked && route ? `to-${route}` : "",
    routing && blocked ? "rejected" : "",
  ].filter(Boolean).join(" ");

  // Once a result is in, show the prompt that was actually inspected rather
  // than whatever the user may have typed since.
  const inFlight = result?.prompt ?? prompt;

  const cheapModel = policy?.models.cheap.id ?? "small model";
  const frontierModel = policy?.models.frontier.id ?? "frontier model";

  return (
    <div className="pipeline">
      <div className="section-title">
        Inspection pipeline
        {result && (
          <span style={{ color: "var(--dim)", fontFamily: "var(--mono)", textTransform: "none", letterSpacing: 0 }}>
            {result.timing.laya_ms}ms in Laya
          </span>
        )}
      </div>

      <div className="bridge">
        {/* ---- origin ---- */}
        <div className="station">
          <div className="glyph" aria-hidden>▤</div>
          <div className="name">Prompt</div>
          {inFlight.trim() && (
            <div className="station-sub" title={inFlight}>{inFlight.trim().length} chars</div>
          )}
        </div>

        {/* ---- the crossing ---- */}
        <div className={`track${scanning ? " scanning" : ""}${routing ? " routing" : ""}${phase !== "idle" ? " active" : ""}`}>
          <div className="rail" />

          <div className="fork" aria-hidden>
            <svg viewBox="0 0 100 108" preserveAspectRatio="none">
              <path
                className={`cheap ${route === "cheap" && !blocked ? "taken" : "dimmed"}`}
                d="M0,54 C40,54 55,26 100,26"
                vectorEffect="non-scaling-stroke"
              />
              <path
                className={`frontier ${route === "frontier" && !blocked ? "taken" : "dimmed"}`}
                d="M0,54 C40,54 55,82 100,82"
                vectorEffect="non-scaling-stroke"
              />
            </svg>
          </div>

          {/* the gate sits on the rail, at the midpoint of the crossing */}
          <div
            className={`gate-node${scanning ? " scanning" : ""}${
              verdict && at(phase, "verdict") ? ` verdict-${verdict}` : ""
            }`}
          >
            <div className="glyph" aria-hidden>⌖</div>
            <div className="name">Laya gate</div>
          </div>

          <div className={packetClasses} aria-hidden>◆</div>

          {showStamp && verdict && (
            <div className={`stamp ${verdict} show`}>{verdict}</div>
          )}
        </div>

        {/* ---- the gate sits visually over the middle of the track ---- */}
        <div className="destinations">
          <div className={`dest cheap${route === "cheap" && routing && !blocked ? " chosen" : ""}${blocked && routing ? " blocked-out" : ""}`}>
            <div className="t"><span className="dot" style={{ background: "var(--cheap)" }} /> Cheap</div>
            <div className="m">{cheapModel}</div>
          </div>
          <div className={`dest frontier${route === "frontier" && routing && !blocked ? " chosen" : ""}${blocked && routing ? " blocked-out" : ""}`}>
            <div className="t"><span className="dot" style={{ background: "var(--frontier)" }} /> Frontier</div>
            <div className="m">{frontierModel}</div>
          </div>
        </div>
      </div>

      {/* ---- readouts ---- */}
      <div className="readout">
        <div>
          <div className="panel-head">
            <h3>Firewall</h3>
            <span className="sub">guard_questions() · 5 questions</span>
          </div>
          {result ? (
            <>
              {result.firewall.signals.map((s, i) => (
                <SignalMeter
                  key={s.name}
                  signal={s}
                  revealed={revealed}
                  delay={i * 90}
                  ticks={ticksFor(s.name, s.kind, policy)}
                />
              ))}
              {at(phase, "verdict") && result.firewall.reasons.length > 0 && (
                <div className="reasons">
                  {result.firewall.reasons.map((r) => (
                    <div className="reason" key={r}>{r}</div>
                  ))}
                </div>
              )}
            </>
          ) : (
            <div className="empty" style={{ padding: "18px 0", textAlign: "left" }}>
              awaiting inspection…
            </div>
          )}
        </div>

        <div>
          <div className="panel-head">
            <h3>Router</h3>
            <span className="sub">router_questions() · 4 questions</span>
          </div>
          {result ? (
            <>
              {result.router.signals.map((s, i) => (
                <SignalMeter
                  key={s.name}
                  signal={s}
                  revealed={revealed}
                  delay={450 + i * 90}
                  ticks={ticksFor(s.name, s.kind, policy)}
                />
              ))}
              {at(phase, "route") && (
                <div className="reasons">
                  {blocked && (
                    <div className="reason" style={{ color: "var(--blocked)" }}>
                      blocked upstream — no model was called
                    </div>
                  )}
                  {result.router.reasons.map((r) => (
                    <div className="reason" key={r}>{r}</div>
                  ))}
                </div>
              )}
            </>
          ) : (
            <div className="empty" style={{ padding: "18px 0", textAlign: "left" }}>
              awaiting inspection…
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
