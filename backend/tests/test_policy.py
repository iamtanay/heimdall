"""Unit tests for the firewall/router policy.

These use synthetic Laya answer dicts shaped exactly like laya 0.3.6 returns,
so they run without torch or the model. Run either way:

    pytest backend/tests/test_policy.py
    python backend/tests/test_policy.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from policy import (  # noqa: E402
    BLOCKED,
    CHEAP,
    CLEAN,
    FLAGGED,
    FRONTIER,
    estimated_cost_saved,
    evaluate_firewall,
    evaluate_router,
    firewall_failsafe,
    router_failsafe,
)


# --- builders mirroring laya's real answer shapes -------------------------

def noul(p, confidence=None):
    return {"type": "noul", "noul": p, "confidence": confidence if confidence is not None else max(p, 1 - p)}


def score(level, confidence=0.80, n=4, probs=None):
    """A `score` answer whose distribution actually has expected value `level`.

    Real score answers spread mass across levels, and policy reads that mass
    (not just the scalar), so a uniform stub would not exercise the routing
    rule. Splitting weight between the two levels either side of `level`
    reproduces the expectation exactly.
    """
    if probs is None:
        lo = max(0, min(int(level), n - 1))
        hi = min(lo + 1, n - 1)
        frac = level - lo
        probs = {str(i): 0.0 for i in range(n)}
        if lo == hi:
            probs[str(lo)] = 1.0
        else:
            probs[str(lo)] = 1.0 - frac
            probs[str(hi)] = frac
    return {
        "type": "score",
        "score": level,
        "probabilities": probs,
        "confidence": confidence,
    }


def choice(key, p=0.75, confidence=0.75):
    return {"type": "choice", "choice": key, "probabilities": {key: p}, "confidence": confidence}


def guard(jailbreak=0.02, injection=0.02, sensitive=0.02, harm=0.1, topic_key="general_knowledge",
          jb_conf=None, inj_conf=None, harm_conf=0.80):
    return {
        "jailbreak": noul(jailbreak, jb_conf),
        "prompt_injection": noul(injection, inj_conf),
        "sensitive_data": noul(sensitive),
        "harm_severity": score(harm, harm_conf),
        "topic": choice(topic_key),
    }


def router(difficulty=0.3, domain_key="factual_lookup", needs_tools=0.05, is_sensitive=0.05,
           diff_conf=0.85, domain_conf=0.85):
    return {
        "difficulty": score(difficulty, diff_conf),
        "domain": choice(domain_key, confidence=domain_conf),
        "needs_tools": noul(needs_tools),
        "is_sensitive": noul(is_sensitive),
    }


# --- firewall -------------------------------------------------------------

def test_benign_prompt_is_clean():
    v = evaluate_firewall(guard())
    assert v.verdict == CLEAN, v.reasons
    assert v.reasons == []
    assert v.topic == "general_knowledge"


def test_high_confidence_injection_blocks():
    v = evaluate_firewall(guard(injection=0.96, inj_conf=0.96))
    assert v.verdict == BLOCKED, v.reasons
    assert any("prompt_injection" in r for r in v.reasons)


def test_jailbreak_blocks():
    v = evaluate_firewall(guard(jailbreak=0.91, jb_conf=0.91))
    assert v.verdict == BLOCKED, v.reasons


def test_ambiguous_prompt_flags_not_blocks():
    v = evaluate_firewall(guard(jailbreak=0.65))
    assert v.verdict == FLAGGED, v.reasons


def test_sensitive_data_never_blocks_even_at_certainty():
    """Regression: a user pasting their own API key is legitimate traffic."""
    v = evaluate_firewall(guard(sensitive=0.99))
    assert v.verdict == FLAGGED, v.reasons
    assert v.verdict != BLOCKED
    assert any("flag-only" in r for r in v.reasons)


def test_harm_severity_score_blocks_without_a_noul_field():
    """harm_severity is a `score`; reading ['noul'] off it would KeyError."""
    v = evaluate_firewall(guard(harm=2.8))
    assert v.verdict == BLOCKED, v.reasons
    assert any("harm_severity" in r for r in v.reasons)


def test_harm_severity_mid_level_flags():
    v = evaluate_firewall(guard(harm=1.7))
    assert v.verdict == FLAGGED, v.reasons


def test_high_probability_but_low_confidence_flags_not_blocks():
    v = evaluate_firewall(guard(injection=0.93, inj_conf=0.41))
    assert v.verdict == FLAGGED, v.reasons
    assert any("low confidence" in r for r in v.reasons)


def test_missing_and_malformed_answers_do_not_raise():
    assert evaluate_firewall({}).verdict == CLEAN
    assert evaluate_firewall({"jailbreak": None, "harm_severity": "nonsense"}).verdict == CLEAN


def test_firewall_failsafe_flags_and_marks_degraded():
    v = firewall_failsafe("model timeout")
    assert v.verdict == FLAGGED
    assert v.degraded is True


def test_signals_cover_every_guard_question():
    v = evaluate_firewall(guard())
    assert {s.name for s in v.signals} == {
        "jailbreak", "prompt_injection", "sensitive_data", "harm_severity", "topic",
    }


# --- router ---------------------------------------------------------------

def test_trivial_request_routes_cheap():
    r = evaluate_router(router())
    assert r.route == CHEAP, r.reasons
    assert r.difficulty_label == "trivial"


def test_hard_request_routes_frontier():
    r = evaluate_router(router(difficulty=2.6, domain_key="math_or_logic"))
    assert r.route == FRONTIER, r.reasons
    assert r.difficulty_label == "hard"


def test_sensitive_request_routes_frontier_even_when_easy():
    r = evaluate_router(router(difficulty=0.2, is_sensitive=0.80))
    assert r.route == FRONTIER, r.reasons
    assert any("is_sensitive" in x for x in r.reasons)


def test_low_scalar_confidence_alone_does_not_force_frontier():
    """Regression: score confidence is structurally low (never above 0.39 on
    any probe prompt). Gating on it sent 13/13 prompts to frontier."""
    r = evaluate_router(router(difficulty=0.2, diff_conf=0.11))
    assert r.route == CHEAP, r.reasons


def test_moderate_difficulty_routes_frontier():
    """p_easy around 0.4 is below the 0.65 needed to trust the cheap model."""
    r = evaluate_router(router(difficulty=1.6))
    assert r.route == FRONTIER, r.reasons
    assert any("easy-mass" in x for x in r.reasons)


def test_unreadable_difficulty_fails_safe_to_frontier():
    r = evaluate_router({"domain": choice("code"), "needs_tools": noul(0.01)})
    assert r.route == FRONTIER, r.reasons
    assert any("unreadable" in x for x in r.reasons)


def test_flagged_prompt_escalates_to_frontier():
    r = evaluate_router(router(), firewall_flagged=True)
    assert r.route == FRONTIER, r.reasons
    assert any("escalated" in x for x in r.reasons)


def test_router_failsafe_routes_frontier():
    r = router_failsafe("model timeout")
    assert r.route == FRONTIER
    assert r.degraded is True


def test_router_handles_empty_answers():
    r = evaluate_router({})
    # No signals at all means zero confidence, which must fail safe.
    assert r.route == FRONTIER


# --- cost model -----------------------------------------------------------

def test_cheap_route_saves_money():
    assert estimated_cost_saved(CHEAP, False, 400) > 0


def test_frontier_route_saves_nothing():
    assert estimated_cost_saved(FRONTIER, False, 400) == 0.0


def test_blocked_request_saves_the_whole_frontier_call():
    blocked = estimated_cost_saved(FRONTIER, True, 400)
    cheap = estimated_cost_saved(CHEAP, False, 400)
    assert blocked > cheap > 0


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            passed += 1
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
