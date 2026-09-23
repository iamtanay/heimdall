"""Heimdall decision policy: firewall + router.

Coded against the REAL shapes of laya 0.3.6's `guard_questions()` and
`router_questions()` (read from the package source; run scripts/discover.py to
re-verify against a live model). The preset keys and their primitive types:

    guard   jailbreak         noul    P(true)
            prompt_injection  noul    P(true)
            sensitive_data    noul    P(true)   <- FLAG ONLY, never blocks
            harm_severity     score   0..3 expected level
            topic             choice  6 options, context only

    router  difficulty        score   0..3 expected level
            domain            choice  6 options
            needs_tools       noul    P(true)
            is_sensitive      noul    P(true)

Three notes on why this file does not simply loop over "every threat category":

1. Only three guard questions expose a `noul` field. `harm_severity` is a
   `score` and `topic` is a `choice`; reading `["noul"]` off either raises a
   KeyError, so each primitive is handled on its own terms.
2. `sensitive_data` asks whether the prompt *contains* credentials or personal
   data. That fires on a user pasting their own API key into a debugging
   question, which is legitimate traffic. It may flag; it may never block.
3. Every auto-action is confidence-gated, and every failure path resolves
   toward the safe action rather than the permissive one.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# Policy configuration. Everything tunable lives here (CLAUDE.md 4.5).
# --------------------------------------------------------------------------

POLICY: Dict[str, Any] = {
    "firewall": {
        # noul probability at/above which a blocking category blocks outright.
        "block_threshold": 0.85,
        # noul probability at/above which any category flags for review.
        "flag_threshold": 0.55,
        # Categories permitted to BLOCK. Deliberately only the two that
        # describe an attack on the system rather than the content of an
        # otherwise legitimate request.
        "blocking_categories": ("jailbreak", "prompt_injection"),
        # Categories that may only ever flag. See note 2 above.
        "flag_only_categories": ("sensitive_data",),
        # harm_severity is a 4-level score: 0 none, 1 minor, 2 serious, 3 severe.
        "harm_block_level": 2.50,
        "harm_flag_level": 1.50,
        # A block is only automatic above this confidence; below it the
        # request is flagged for review instead.
        "min_block_confidence": 0.60,
    },
    "router": {
        # difficulty is a 4-level score: 0 trivial, 1 easy, 2 moderate, 3 hard.
        # Route cheap only when enough probability mass sits on levels 0-1.
        # Calibrated from measured distributions, see scripts/probe_output.txt:
        #
        #     trivial / easy / chitchat    p_easy  0.71 - 0.94
        #     moderate                     p_easy  0.43
        #     hard                         p_easy  0.11 - 0.19
        #
        # DO NOT gate on the score question's scalar `confidence` instead. A
        # score spreads its mass across four levels, so that number never rose
        # above 0.39 on any probe prompt - gating on it at 0.60 sent 13 of 13
        # prompts to frontier and the router stopped saving anything at all.
        "min_easy_mass": 0.65,
        # Money/legal/medical/safety goes frontier: the worst case for
        # over-routing is cost, for under-routing it is harm.
        "sensitive_to_frontier_at": 0.50,
        # Needing external tools or private data implies a harder job.
        "needs_tools_to_frontier_at": 0.60,
        # If difficulty is missing or unreadable, fail safe to frontier.
        "frontier_when_uncertain": True,
        # A flagged prompt gets the stronger model, not the cheaper one.
        "flagged_to_frontier": True,
    },
    "models": {
        # Illustrative ids + blended USD per 1M tokens, used only for the
        # "cost avoided" estimate. No provider is called in inspect-only mode.
        "cheap": {"id": "claude-haiku-4-5-20251001", "usd_per_mtok": 1.00},
        "frontier": {"id": "claude-opus-5", "usd_per_mtok": 15.00},
    },
    "limits": {
        # Laya's English checkpoint has a 512-token window; longer prompts are
        # truncated for inspection. Anything past this is refused outright.
        "max_prompt_chars": 20000,
    },
}

# Human-readable level names, mirroring the preset criteria order.
HARM_LEVELS = ("none", "minor", "serious", "severe")
DIFFICULTY_LEVELS = ("trivial", "easy", "moderate", "hard")

CLEAN, FLAGGED, BLOCKED = "clean", "flagged", "blocked"
CHEAP, FRONTIER = "cheap", "frontier"


# --------------------------------------------------------------------------
# Result types. Shaped for direct consumption by the dashboard.
# --------------------------------------------------------------------------

@dataclass
class Signal:
    """One question's answer, normalised so the UI can render any primitive."""

    name: str
    kind: str                     # "noul" | "score" | "choice"
    value: float                  # 0..1, comparable across primitives
    confidence: float
    display: str                  # human-readable answer
    triggered: bool = False       # did this contribute to a flag/block?
    severity: str = CLEAN         # clean | flagged | blocked
    detail: Optional[str] = None  # why it triggered


@dataclass
class FirewallVerdict:
    verdict: str                          # clean | flagged | blocked
    reasons: List[str] = field(default_factory=list)
    signals: List[Signal] = field(default_factory=list)
    confidences: Dict[str, float] = field(default_factory=dict)
    topic: Optional[str] = None
    degraded: bool = False                # Laya failed and we failed safe


@dataclass
class RouteVerdict:
    route: str                            # cheap | frontier
    reasons: List[str] = field(default_factory=list)
    signals: List[Signal] = field(default_factory=list)
    difficulty: Optional[float] = None
    difficulty_label: Optional[str] = None
    domain: Optional[str] = None
    confidence: float = 0.0
    degraded: bool = False


# --------------------------------------------------------------------------
# Primitive readers. Each tolerates a missing or malformed answer by returning
# None, so a partial Laya result degrades instead of raising.
# --------------------------------------------------------------------------

def _noul(answers: Dict[str, Any], key: str) -> Optional[Dict[str, float]]:
    a = answers.get(key)
    if not isinstance(a, dict) or "noul" not in a:
        return None
    return {"p": float(a["noul"]), "confidence": float(a.get("confidence", 0.0))}


def _score(answers: Dict[str, Any], key: str, n_levels: int) -> Optional[Dict[str, Any]]:
    """Read a `score` answer. `score` is the expected level, 0..n_levels-1.

    `levels` carries the per-level probabilities. They matter: a `score`
    question spreads its mass over every level, so its scalar `confidence` is
    structurally low and makes a poor gate. The mass on a *range* of levels is
    the calibrated quantity worth thresholding on.
    """
    a = answers.get(key)
    if not isinstance(a, dict) or "score" not in a:
        return None
    raw = float(a["score"])
    span = max(n_levels - 1, 1)
    probs = a.get("probabilities") or {}
    levels = [float(probs.get(str(i), 0.0)) for i in range(n_levels)]
    return {
        "level": raw,
        "normalised": min(max(raw / span, 0.0), 1.0),
        "confidence": float(a.get("confidence", 0.0)),
        "levels": levels,
    }


def _choice(answers: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
    a = answers.get(key)
    if not isinstance(a, dict) or "choice" not in a:
        return None
    probs = a.get("probabilities") or {}
    top = float(probs.get(a["choice"], 0.0)) if probs else 0.0
    return {
        "choice": str(a["choice"]),
        "top_probability": top,
        "confidence": float(a.get("confidence", 0.0)),
    }


def _label_for_level(level: float, names: tuple) -> str:
    idx = int(round(level))
    return names[min(max(idx, 0), len(names) - 1)]


# --------------------------------------------------------------------------
# Firewall
# --------------------------------------------------------------------------

def evaluate_firewall(guard_answers: Dict[str, Any]) -> FirewallVerdict:
    """Apply firewall policy to a guard preset result's `answers` dict."""
    cfg = POLICY["firewall"]
    block_at = cfg["block_threshold"]
    flag_at = cfg["flag_threshold"]
    min_conf = cfg["min_block_confidence"]

    signals: List[Signal] = []
    confidences: Dict[str, float] = {}
    reasons: List[str] = []
    blocked = False
    flagged = False

    # --- noul categories -------------------------------------------------
    noul_keys = tuple(cfg["blocking_categories"]) + tuple(cfg["flag_only_categories"])
    for key in noul_keys:
        got = _noul(guard_answers, key)
        if got is None:
            continue
        p, conf = got["p"], got["confidence"]
        confidences[key] = conf
        can_block = key in cfg["blocking_categories"]

        sig = Signal(name=key, kind="noul", value=p, confidence=conf, display=f"{p:.0%}")

        if can_block and p >= block_at:
            if conf >= min_conf:
                blocked = True
                sig.triggered, sig.severity = True, BLOCKED
                sig.detail = f"{p:.0%} at or above the {block_at:.0%} block threshold"
                reasons.append(f"{key}: {p:.0%} (blocks at {block_at:.0%})")
            else:
                # High probability but low confidence: flag for a human rather
                # than auto-blocking on a shaky verdict.
                flagged = True
                sig.triggered, sig.severity = True, FLAGGED
                sig.detail = f"{p:.0%} but confidence {conf:.0%} is under {min_conf:.0%}"
                reasons.append(f"{key}: {p:.0%} at low confidence - flagged, not blocked")
        elif p >= flag_at:
            flagged = True
            sig.triggered, sig.severity = True, FLAGGED
            note = " (flag-only category)" if not can_block else ""
            sig.detail = f"{p:.0%} at or above the {flag_at:.0%} flag threshold{note}"
            reasons.append(f"{key}: {p:.0%}{note}")

        signals.append(sig)

    # --- harm_severity (score, not noul) ---------------------------------
    harm = _score(guard_answers, "harm_severity", len(HARM_LEVELS))
    if harm is not None:
        level, conf = harm["level"], harm["confidence"]
        confidences["harm_severity"] = conf
        label = _label_for_level(level, HARM_LEVELS)
        sig = Signal(
            name="harm_severity",
            kind="score",
            value=harm["normalised"],
            confidence=conf,
            display=f"{label} ({level:.2f}/3)",
        )
        if level >= cfg["harm_block_level"] and conf >= min_conf:
            blocked = True
            sig.triggered, sig.severity = True, BLOCKED
            sig.detail = f"level {level:.2f} at or above {cfg['harm_block_level']}"
            reasons.append(f"harm_severity: {label} ({level:.2f}/3)")
        elif level >= cfg["harm_flag_level"]:
            flagged = True
            sig.triggered, sig.severity = True, FLAGGED
            sig.detail = f"level {level:.2f} at or above {cfg['harm_flag_level']}"
            reasons.append(f"harm_severity: {label} ({level:.2f}/3)")
        signals.append(sig)

    # --- topic (choice, context only, never triggers) --------------------
    topic_val = None
    topic = _choice(guard_answers, "topic")
    if topic is not None:
        topic_val = topic["choice"]
        confidences["topic"] = topic["confidence"]
        signals.append(Signal(
            name="topic",
            kind="choice",
            value=topic["top_probability"],
            confidence=topic["confidence"],
            display=topic_val.replace("_", " "),
        ))

    verdict = BLOCKED if blocked else (FLAGGED if flagged else CLEAN)
    return FirewallVerdict(
        verdict=verdict,
        reasons=reasons,
        signals=signals,
        confidences=confidences,
        topic=topic_val,
    )


def firewall_failsafe(error: str) -> FirewallVerdict:
    """Laya failed. Fail safe: mark for review rather than silently allowing."""
    return FirewallVerdict(
        verdict=FLAGGED,
        reasons=[f"inspection unavailable ({error}) - failed safe to review"],
        degraded=True,
    )


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------

def evaluate_router(
    router_answers: Dict[str, Any],
    *,
    firewall_flagged: bool = False,
) -> RouteVerdict:
    """Apply routing policy to a router preset result's `answers` dict."""
    cfg = POLICY["router"]
    signals: List[Signal] = []
    reasons: List[str] = []
    to_frontier = False
    confidences: List[float] = []

    # --- difficulty (score) ----------------------------------------------
    # The decision quantity is p_easy: the probability mass on levels 0-1.
    # The meter therefore shows its complement - how much mass sits on
    # moderate/hard - so the bar and its threshold tick explain the routing.
    difficulty = difficulty_label = None
    p_easy: Optional[float] = None
    diff = _score(router_answers, "difficulty", len(DIFFICULTY_LEVELS))
    if diff is not None:
        difficulty, conf = diff["level"], diff["confidence"]
        confidences.append(conf)
        difficulty_label = _label_for_level(difficulty, DIFFICULTY_LEVELS)
        levels = diff["levels"]
        p_easy = sum(levels[:2]) if len(levels) >= 2 else 0.0

        sig = Signal(
            name="difficulty",
            kind="score",
            value=1.0 - p_easy,
            confidence=conf,
            display=f"{difficulty_label} · p(easy)={p_easy:.0%}",
        )
        if p_easy < cfg["min_easy_mass"]:
            to_frontier = True
            sig.triggered, sig.severity = True, FLAGGED
            sig.detail = (
                f"only {p_easy:.0%} of the mass on trivial/easy, "
                f"under the {cfg['min_easy_mass']:.0%} needed for the cheap model"
            )
            reasons.append(
                f"difficulty {difficulty_label} - {p_easy:.0%} easy-mass "
                f"(needs {cfg['min_easy_mass']:.0%})"
            )
        signals.append(sig)
    elif cfg["frontier_when_uncertain"]:
        to_frontier = True
        reasons.append("difficulty unreadable - defaulted to frontier")

    # --- is_sensitive / needs_tools (noul) --------------------------------
    for key, threshold, why in (
        ("is_sensitive", cfg["sensitive_to_frontier_at"], "money/legal/medical/safety stakes"),
        ("needs_tools", cfg["needs_tools_to_frontier_at"], "needs external tools or private data"),
    ):
        got = _noul(router_answers, key)
        if got is None:
            continue
        p, conf = got["p"], got["confidence"]
        confidences.append(conf)
        sig = Signal(name=key, kind="noul", value=p, confidence=conf, display=f"{p:.0%}")
        if p >= threshold:
            to_frontier = True
            sig.triggered, sig.severity = True, FLAGGED
            sig.detail = f"{p:.0%} at or above {threshold:.0%}"
            reasons.append(f"{key}: {p:.0%} - {why}")
        signals.append(sig)

    # --- domain (choice, context only) ------------------------------------
    domain_val = None
    domain = _choice(router_answers, "domain")
    if domain is not None:
        domain_val = domain["choice"]
        confidences.append(domain["confidence"])
        signals.append(Signal(
            name="domain",
            kind="choice",
            value=domain["top_probability"],
            confidence=domain["confidence"],
            display=domain_val.replace("_", " "),
        ))

    # --- fail-safe gates ---------------------------------------------------
    # Reported for observability only. It is deliberately NOT a gate: see the
    # note on min_easy_mass in POLICY.
    route_confidence = min(confidences) if confidences else 0.0
    if firewall_flagged and cfg["flagged_to_frontier"]:
        to_frontier = True
        reasons.append("prompt was flagged - escalated to frontier")

    if not reasons:
        reasons.append("simple, low-stakes request - the small model is sufficient")

    return RouteVerdict(
        route=FRONTIER if to_frontier else CHEAP,
        reasons=reasons,
        signals=signals,
        difficulty=difficulty,
        difficulty_label=difficulty_label,
        domain=domain_val,
        confidence=route_confidence,
    )


def router_failsafe(error: str) -> RouteVerdict:
    """Laya failed. Fail safe: never downgrade quality on an error path."""
    return RouteVerdict(
        route=FRONTIER,
        reasons=[f"routing unavailable ({error}) - failed safe to frontier"],
        degraded=True,
    )


# --------------------------------------------------------------------------
# Cost model
# --------------------------------------------------------------------------

def estimated_cost_saved(route: str, blocked: bool, prompt_chars: int) -> float:
    """USD avoided versus sending every request to the frontier model.

    A deliberately rough estimate: ~4 chars per token, plus an assumed 500-token
    response. It exists to show the shape of the saving, not to bill on.
    """
    tokens = (prompt_chars / 4.0) + 500.0
    frontier = POLICY["models"]["frontier"]["usd_per_mtok"] * tokens / 1_000_000
    if blocked:
        return frontier  # nothing was spent at all
    if route == CHEAP:
        cheap = POLICY["models"]["cheap"]["usd_per_mtok"] * tokens / 1_000_000
        return max(frontier - cheap, 0.0)
    return 0.0


def signals_to_dicts(signals: List[Signal]) -> List[Dict[str, Any]]:
    return [asdict(s) for s in signals]
