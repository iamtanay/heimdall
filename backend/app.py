"""Heimdall gateway - FastAPI app.

Endpoints:
    GET  /health    liveness + whether the model finished loading
    POST /inspect   run guard+router on a prompt, return the full verdict
    GET  /metrics   rolling counters over the last N inspections
    GET  /policy    the active thresholds, so the dashboard can show them

Laya is loaded exactly once, at startup, on CPU (CLAUDE.md 4.1/4.3).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, Deque, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import policy
from policy import (
    POLICY,
    estimated_cost_saved,
    evaluate_firewall,
    evaluate_router,
    firewall_failsafe,
    router_failsafe,
    signals_to_dicts,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")
log = logging.getLogger("heimdall")

MODEL_ID = "convaiinnovations/laya"
RING_SIZE = 500

# CLAUDE.md 8 suggests merging guard+router into a single predict() call to save
# a pass. Measured on this machine, that is wrong on both counts and must not be
# enabled - see backend/scripts/discovery_output.txt:
#
#   two passes   (5 + 4 questions) : 2799.8 ms
#   merged pass  (9 questions)     : 3865.2 ms   <- 38% SLOWER, not faster
#
# and far worse, the merged answers are badly corrupted. Feeding a state with
# both `prompt` and `request` produced 31 mismatches across 5 probe prompts,
# including "What's 2+2?" scoring jailbreak 0.0001 separately but 0.9955 merged
# - a benign prompt that the merged path would have blocked outright.
#
# Keep this False. scripts/discover.py re-runs the comparison.
SINGLE_PASS = False


# --------------------------------------------------------------------------
# App state
# --------------------------------------------------------------------------

class State:
    agent: Any = None
    guard_q: Dict[str, Any] = {}
    router_q: Dict[str, Any] = {}
    merged_q: Dict[str, Any] = {}
    ready: bool = False
    load_error: Optional[str] = None
    load_seconds: float = 0.0
    ring: Deque[Dict[str, Any]] = deque(maxlen=RING_SIZE)
    # Laya inference is synchronous and not thread-safe; serialise access so
    # concurrent requests queue rather than racing the same model.
    lock: asyncio.Lock = asyncio.Lock()


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    import laya  # imported here so a missing torch fails loudly at startup

    log.info("loading Laya (%s) on CPU - first run downloads weights...", MODEL_ID)
    t0 = time.perf_counter()
    try:
        state.agent = laya.load(MODEL_ID)  # CPU is the default; never pass cuda
        state.guard_q = laya.guard_questions()
        state.router_q = laya.router_questions()
        state.merged_q = {**state.guard_q, **state.router_q}
        if SINGLE_PASS and len(state.merged_q) != len(state.guard_q) + len(state.router_q):
            raise RuntimeError("guard/router preset keys collide - cannot merge passes")
        state.load_seconds = time.perf_counter() - t0
        state.ready = True
        log.info("Laya ready in %.1fs (%d guard + %d router questions)",
                 state.load_seconds, len(state.guard_q), len(state.router_q))
    except Exception as exc:  # noqa: BLE001
        state.load_error = f"{type(exc).__name__}: {exc}"
        log.exception("Laya failed to load - every request will fail safe")
    yield


app = FastAPI(title="Heimdall", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000", "http://127.0.0.1:3000",
        "http://localhost:5173", "http://127.0.0.1:5173",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class InspectRequest(BaseModel):
    prompt: str = Field(..., min_length=1)


# --------------------------------------------------------------------------
# Core inspection
# --------------------------------------------------------------------------

def _run_laya(prompt: str) -> Dict[str, Any]:
    """Run the preset questions. Returns {'guard': {...}, 'router': {...}}."""
    if SINGLE_PASS:
        res = state.agent.predict({"prompt": prompt, "request": prompt}, state.merged_q)
        answers = res.get("answers", {})
        return {
            "guard": {k: v for k, v in answers.items() if k in state.guard_q},
            "router": {k: v for k, v in answers.items() if k in state.router_q},
        }
    g = state.agent.predict({"prompt": prompt}, state.guard_q)
    r = state.agent.predict({"request": prompt}, state.router_q)
    return {"guard": g.get("answers", {}), "router": r.get("answers", {})}


async def inspect_prompt(prompt: str) -> Dict[str, Any]:
    """Guard + route one prompt and record it in the metrics ring."""
    started = time.perf_counter()
    laya_ms = 0.0

    if not state.ready:
        reason = state.load_error or "model still loading"
        fw, rt = firewall_failsafe(reason), router_failsafe(reason)
    else:
        try:
            t0 = time.perf_counter()
            async with state.lock:
                answers = await asyncio.to_thread(_run_laya, prompt)
            laya_ms = (time.perf_counter() - t0) * 1000
            fw = evaluate_firewall(answers["guard"])
            rt = evaluate_router(
                answers["router"],
                firewall_flagged=(fw.verdict == policy.FLAGGED),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("inspection failed - failing safe")
            reason = f"{type(exc).__name__}: {exc}"
            fw, rt = firewall_failsafe(reason), router_failsafe(reason)

    blocked = fw.verdict == policy.BLOCKED
    # A blocked prompt reaches no model at all, so it has no route.
    route = None if blocked else rt.route
    saved = estimated_cost_saved(rt.route, blocked, len(prompt))

    record = {
        "id": uuid.uuid4().hex[:12],
        "ts": time.time(),
        "prompt": prompt,
        "verdict": fw.verdict,
        "route": route,
        "firewall": {
            "verdict": fw.verdict,
            "reasons": fw.reasons,
            "signals": signals_to_dicts(fw.signals),
            "confidences": fw.confidences,
            "topic": fw.topic,
            "degraded": fw.degraded,
        },
        "router": {
            "route": rt.route,
            "reasons": rt.reasons,
            "signals": signals_to_dicts(rt.signals),
            "difficulty": rt.difficulty,
            "difficulty_label": rt.difficulty_label,
            "domain": rt.domain,
            "confidence": rt.confidence,
            "degraded": rt.degraded,
            "applied": not blocked,
        },
        "timing": {
            "laya_ms": round(laya_ms, 1),
            "total_ms": round((time.perf_counter() - started) * 1000, 1),
        },
        "cost": {
            "est_saved_usd": round(saved, 6),
            "chosen_model": None if blocked else POLICY["models"][rt.route]["id"],
            "frontier_model": POLICY["models"]["frontier"]["id"],
        },
        "degraded": fw.degraded or rt.degraded,
    }
    state.ring.append(record)
    return record


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "model_ready": state.ready,
        "model": MODEL_ID,
        "load_seconds": round(state.load_seconds, 1),
        "error": state.load_error,
        "single_pass": SINGLE_PASS,
    }


@app.get("/policy")
async def get_policy() -> Dict[str, Any]:
    """Expose the active thresholds so the dashboard can label its own gauges."""
    return {
        "firewall": {
            k: (list(v) if isinstance(v, tuple) else v)
            for k, v in POLICY["firewall"].items()
        },
        "router": POLICY["router"],
        "models": POLICY["models"],
        "harm_levels": list(policy.HARM_LEVELS),
        "difficulty_levels": list(policy.DIFFICULTY_LEVELS),
    }


@app.post("/inspect")
async def inspect(req: InspectRequest) -> Dict[str, Any]:
    prompt = req.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=422, detail="prompt is empty")
    limit = POLICY["limits"]["max_prompt_chars"]
    if len(prompt) > limit:
        raise HTTPException(status_code=413, detail=f"prompt exceeds {limit} characters")
    return await inspect_prompt(prompt)


@app.get("/metrics")
async def metrics() -> Dict[str, Any]:
    records: List[Dict[str, Any]] = list(state.ring)
    total = len(records)
    if not total:
        return {
            "total": 0, "blocked": 0, "flagged": 0, "clean": 0,
            "cheap": 0, "frontier": 0, "pct_cheap": 0.0, "pct_blocked": 0.0,
            "avg_added_latency_ms": 0.0, "p95_added_latency_ms": 0.0,
            "est_cost_saved_usd": 0.0, "degraded": 0,
        }

    blocked = sum(r["verdict"] == policy.BLOCKED for r in records)
    flagged = sum(r["verdict"] == policy.FLAGGED for r in records)
    cheap = sum(r["route"] == policy.CHEAP for r in records)
    frontier = sum(r["route"] == policy.FRONTIER for r in records)
    routed = cheap + frontier
    latencies = sorted(r["timing"]["total_ms"] for r in records)
    p95 = latencies[min(int(len(latencies) * 0.95), len(latencies) - 1)]

    return {
        "total": total,
        "blocked": blocked,
        "flagged": flagged,
        "clean": total - blocked - flagged,
        "cheap": cheap,
        "frontier": frontier,
        "pct_cheap": round(cheap / routed * 100, 1) if routed else 0.0,
        "pct_blocked": round(blocked / total * 100, 1),
        "avg_added_latency_ms": round(sum(latencies) / total, 1),
        "p95_added_latency_ms": round(p95, 1),
        "est_cost_saved_usd": round(sum(r["cost"]["est_saved_usd"] for r in records), 6),
        "degraded": sum(bool(r["degraded"]) for r in records),
    }


@app.get("/traffic")
async def traffic(limit: int = 50) -> Dict[str, Any]:
    """Most recent inspections, newest first."""
    records = list(state.ring)[-max(1, min(limit, RING_SIZE)):]
    return {"records": list(reversed(records))}
