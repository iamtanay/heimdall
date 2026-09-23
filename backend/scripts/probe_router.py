"""Dump the router's raw distributions so the routing threshold is calibrated
from data rather than guessed.

The first cut of evaluate_router() gated on min(confidence) across all four
questions and sent 13/13 prompts to frontier. `score` questions spread their
mass over 4 levels, so their confidence is structurally low and that gate
always fired. The better signal is the probability mass on the easy levels of
`difficulty` (0=trivial, 1=easy), which is directly calibrated.

Run:  .venv/Scripts/python backend/scripts/probe_router.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import laya  # noqa: E402

PROMPTS = [
    ("trivial", "What's the capital of Norway?"),
    ("trivial", "What's 2+2?"),
    ("chitchat", "Morning! How's it going today?"),
    ("easy", "Write a haiku about the sea."),
    ("easy", "Summarise what HTTP 404 means in one sentence."),
    ("moderate", "Write a Python function that merges two sorted lists."),
    ("hard", "Derive the Black-Scholes PDE from a delta-hedged portfolio, and "
             "explain every assumption you rely on along the way."),
    ("hard", "My Postgres query planner switched from an index scan to a seq scan "
             "after an ANALYZE and p99 latency tripled. Walk me through diagnosing why."),
    ("hard", "Design a rate limiter for a distributed API gateway that stays correct "
             "under clock skew between nodes."),
    ("sensitive", "I've been taking 400mg of ibuprofen four times a day for two "
                  "weeks for back pain. Is that safe to keep doing?"),
]


def main() -> None:
    agent = laya.load("convaiinnovations/laya")
    router_q = laya.router_questions()

    print(f"{'expect':10} {'score':>5} {'p0':>5} {'p1':>5} {'p2':>5} {'p3':>5} "
          f"{'p_easy':>7} {'conf':>5} {'sens':>5} {'tool':>5}  prompt")
    print("-" * 104)

    for expect, prompt in PROMPTS:
        a = agent.predict({"request": prompt}, router_q)["answers"]
        d = a["difficulty"]
        p = d["probabilities"]
        p_easy = float(p["0"]) + float(p["1"])
        print(
            f"{expect:10} {d['score']:5.2f} "
            f"{float(p['0']):5.2f} {float(p['1']):5.2f} {float(p['2']):5.2f} {float(p['3']):5.2f} "
            f"{p_easy:7.2f} {d['confidence']:5.2f} "
            f"{a['is_sensitive']['noul']:5.2f} {a['needs_tools']['noul']:5.2f}  {prompt[:38]}"
        )

    print("\n# Pick min_easy_mass so the trivial/easy/chitchat rows land above it")
    print("# and the hard rows land below it.")


if __name__ == "__main__":
    main()
