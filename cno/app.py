"""
Strata Core — FastAPI surface for the NLP inference pipeline. Exposes the
5-stage pipeline + audit log + per-stage endpoints.

The route namespace and Python package are `cno` (the service's internal
module name); the project/product name is Strata Core.

Run:
    uvicorn cno.app:app --reload
    open http://localhost:8000/docs
"""

from __future__ import annotations
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import SETTINGS
from .envelope import REGISTRY, envelope_to_dict, wrap
from .logging_config import configure_logging
from .metadata import make_metadata, render_artifact, to_dict as metadata_to_dict
from .middleware import APIKeyMiddleware, RateLimitMiddleware
from .nodes import Modality, PersonaStyle, RequestType, Sublayer
from .nodes.input_node import InputClassification, Tone
from .nodes.persona_node import PersonaSelection
from .nodes.router_node import RoutingDecision
from .pipeline import PIPELINE


configure_logging()
log = logging.getLogger("cno.app")


app = FastAPI(
    title="Strata Core — local-first NLP inference pipeline",
    version="0.3.0",
    description=(
        "A modular, backend-agnostic text-processing pipeline: input "
        "classification, routing, context management, style conditioning, and "
        "response synthesis, with per-stage observability. Inference backends "
        "are pluggable (heuristics by default; a local LLM via Ollama when "
        "enabled). Routes live under the /cno namespace."
    ),
)

# Middleware order matters: outermost = last added in Starlette.
# We want CORS -> auth -> rate-limit (rate-limit closest to handler).
app.add_middleware(
    RateLimitMiddleware,
    limit_per_minute=SETTINGS.rate_limit_per_minute,
    bypass_paths=SETTINGS.auth_bypass_paths,
)
app.add_middleware(
    APIKeyMiddleware,
    api_key=SETTINGS.api_key,
    bypass_paths=SETTINGS.auth_bypass_paths,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=SETTINGS.allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ProcessRequest(BaseModel):
    request:        str = Field(..., min_length=1, max_length=10_000)
    draft_response: Optional[str] = ""
    modality_hint:  Optional[str] = None


@app.get("/healthz", tags=["meta"])
def healthz():
    return {
        "status": "ok",
        "service": "cno",
        "version": "0.3.0",
        "auth_enabled":  SETTINGS.api_key is not None,
        "rate_limit_per_minute": SETTINGS.rate_limit_per_minute,
    }


def _payload_dict(result) -> dict:
    """CNOPipelineResult -> JSON-safe dict (FastAPI normally does this for us)."""
    return {
        "run_id":            result.run_id,
        "request":           result.request,
        "classification":    result.classification,
        "routing":           result.routing,
        "memory_anchor":     result.memory_anchor,
        "persona_selection": result.persona_selection,
        "synthesis":         result.synthesis,
        "module_tags":       result.module_tags,
        "timestamp":         result.timestamp,
        "glyph_pipeline":    result.glyph_pipeline,
    }


@app.post("/cno/process", tags=["pipeline"])
def cno_process(req: ProcessRequest, x_cno_session_id: str | None = Header(default=None)):
    modality = None
    if req.modality_hint:
        try:
            modality = Modality(req.modality_hint)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"unknown modality: {req.modality_hint}")
    result = PIPELINE.process(req.request, req.draft_response, modality)

    session_id = REGISTRY.session_for(x_cno_session_id)
    prior      = REGISTRY.append_run(session_id, result.run_id)

    payload = _payload_dict(result)
    payload["metadata"] = metadata_to_dict(make_metadata(
        run_id=result.run_id, ts=result.timestamp,
        session_id=session_id,
    ))

    envelope = wrap(
        payload        = payload,
        run_id         = result.run_id,
        ts             = result.timestamp,
        glyph_pipeline = result.glyph_pipeline,
        session_id     = session_id,
        prior_run_ids  = prior,
    )

    log.info(
        "pipeline_run",
        extra={
            "run_id":     result.run_id,
            "session_id": session_id,
            "sublayer":   result.routing["sublayer"],
            "modality":   result.classification["modality"],
        },
    )
    return envelope_to_dict(envelope)


@app.get("/cno/state", tags=["state"])
def get_state():
    """Inspect current memory anchors (live context stack)."""
    anchors = PIPELINE.memory.recent(20)
    return {
        "anchor_count": len(anchors),
        "recent": [
            {"timestamp": a.timestamp, "summary": a.summary, "type": a.request_type, "tone": a.tone}
            for a in anchors
        ],
    }


@app.post("/cno/state/reset", tags=["state"])
def reset_state():
    PIPELINE.memory.reset()
    return {"status": "reset"}


@app.post("/cno/node/{node_name}", tags=["pipeline"])
def run_single_node(node_name: str, body: dict[str, Any]):
    """
    Run exactly one node in isolation. Body shape varies by node:
      input:   {request: str, modality_hint?: str}
      router:  {modality, request_type, tone, raw_length?}
      memory:  {summary: str, request_type?: str, tone?: str}
      persona: {classification: {...}, routing: {...}}
      synth:   {draft: str, persona_style: str}
    """
    try:
        if node_name == "input":
            modality = Modality(body["modality_hint"]) if body.get("modality_hint") else None
            cls = PIPELINE.input.classify(body["request"], modality_hint=modality)
            return {"node": "input", "glyph": "📥", "output": _classification_dict(cls)}

        if node_name == "router":
            cls = InputClassification(
                modality=Modality(body["modality"]),
                request_type=RequestType(body["request_type"]),
                tone=Tone(body["tone"]),
                raw_length=int(body.get("raw_length", 0)),
            )
            decision = PIPELINE.router.route(cls)
            return {"node": "router", "glyph": "🔄", "output": {"sublayer": decision.sublayer.value, "rationale": decision.rationale}}

        if node_name == "memory":
            anchor = PIPELINE.memory.anchor(
                summary=body["summary"],
                request_type=body.get("request_type", "unknown"),
                tone=body.get("tone", "neutral"),
            )
            return {"node": "memory", "glyph": "🧊", "output": {
                "timestamp": anchor.timestamp, "summary": anchor.summary,
                "request_type": anchor.request_type, "tone": anchor.tone,
            }}

        if node_name == "persona":
            c = body["classification"]
            r = body["routing"]
            cls = InputClassification(
                modality=Modality(c["modality"]),
                request_type=RequestType(c["request_type"]),
                tone=Tone(c["tone"]),
                raw_length=int(c.get("raw_length", 0)),
            )
            decision = RoutingDecision(sublayer=Sublayer(r["sublayer"]), rationale=r.get("rationale", ""))
            persona = PIPELINE.persona.select(cls, decision)
            return {"node": "persona", "glyph": "🥥", "output": {"style": persona.style.value, "rationale": persona.rationale}}

        if node_name == "synth":
            persona = PersonaSelection(
                style=PersonaStyle(body["persona_style"]),
                rationale=body.get("rationale", ""),
            )
            synth = PIPELINE.synth.synthesize(body["draft"], persona)
            return {"node": "synth", "glyph": "📤", "output": {
                "body": synth.body, "clarity_score": synth.clarity_score, "style_applied": synth.style_applied,
            }}

        raise HTTPException(status_code=404, detail=f"unknown node: {node_name}")

    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"invalid payload for node '{node_name}': {e}")


def _classification_dict(cls: InputClassification) -> dict:
    return {
        "modality":                cls.modality.value,
        "request_type":            cls.request_type.value,
        "tone":                    cls.tone.value,
        "raw_length":              cls.raw_length,
        "modality_confidence":     cls.modality_confidence,
        "request_type_confidence": cls.request_type_confidence,
        "tone_confidence":         cls.tone_confidence,
    }


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode("utf-8")


@app.post("/cno/process/stream", tags=["pipeline"])
def cno_process_stream(req: ProcessRequest, x_cno_session_id: str | None = Header(default=None)):
    """
    Server-Sent Events stream. Emits one `node` event per node crossing as the
    pipeline runs, then a final `complete` event carrying the CSTM §6 envelope.
    """
    modality = None
    if req.modality_hint:
        try:
            modality = Modality(req.modality_hint)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"unknown modality: {req.modality_hint}")

    session_id = REGISTRY.session_for(x_cno_session_id)

    def gen():
        run_id = uuid.uuid4().hex
        yield _sse("start", {"run_id": run_id, "session_id": session_id, "request": req.request[:500]})

        t0 = time.perf_counter()
        cls = PIPELINE.input.classify(req.request, modality_hint=modality)
        cls_payload = _classification_dict(cls)
        yield _sse("node", {"step": 1, "node": "input", "glyph": "📥",
                            "elapsed_ms": _ms_since(t0), "output": cls_payload})
        if PIPELINE.audit:
            PIPELINE.audit.record_node_crossing(run_id, 1, "input", {"request": req.request[:200]}, cls_payload)

        t1 = time.perf_counter()
        route = PIPELINE.router.route(cls)
        route_payload = {"sublayer": route.sublayer.value, "rationale": route.rationale}
        yield _sse("node", {"step": 2, "node": "router", "glyph": "🔄",
                            "elapsed_ms": _ms_since(t1), "output": route_payload})
        if PIPELINE.audit:
            PIPELINE.audit.record_node_crossing(run_id, 2, "router", cls_payload, route_payload)

        t2 = time.perf_counter()
        anchor = PIPELINE.memory.anchor(summary=req.request[:200],
                                        request_type=cls.request_type.value, tone=cls.tone.value)
        anchor_payload = {"timestamp": anchor.timestamp, "summary": anchor.summary}
        yield _sse("node", {"step": 3, "node": "memory", "glyph": "🧊",
                            "elapsed_ms": _ms_since(t2), "output": anchor_payload})
        if PIPELINE.audit:
            PIPELINE.audit.record_node_crossing(run_id, 3, "memory", {"summary": anchor.summary}, anchor_payload)

        t3 = time.perf_counter()
        persona = PIPELINE.persona.select(cls, route)
        persona_payload = {
            "style": persona.style.value,
            "rationale": persona.rationale,
            "confidence": persona.confidence,
        }
        yield _sse("node", {"step": 4, "node": "persona", "glyph": "🥥",
                            "elapsed_ms": _ms_since(t3), "output": persona_payload})
        if PIPELINE.audit:
            PIPELINE.audit.record_node_crossing(run_id, 4, "persona",
                                                {"classification": cls_payload, "routing": route_payload},
                                                persona_payload)

        t4 = time.perf_counter()
        draft = req.draft_response or f"[draft based on {route.sublayer.value} routing of: {req.request[:100]}]"
        synth = PIPELINE.synth.synthesize(draft, persona)
        synth_payload = {
            "body": synth.body[:500],
            "clarity_score": synth.clarity_score,
            "style_applied": synth.style_applied,
            "transforms": list(synth.transforms),
        }
        yield _sse("node", {"step": 5, "node": "synth", "glyph": "📤",
                            "elapsed_ms": _ms_since(t4), "output": synth_payload})
        if PIPELINE.audit:
            PIPELINE.audit.record_node_crossing(run_id, 5, "synth",
                                                {"draft": draft[:200], "persona": persona_payload},
                                                synth_payload)
            PIPELINE.audit.record_run_header(
                run_id=run_id, request=req.request,
                modality=cls.modality.value, request_type=cls.request_type.value, tone=cls.tone.value,
                sublayer=route.sublayer.value, persona_style=persona.style.value,
                clarity_score=synth.clarity_score, synthesis_body=synth.body,
                total_ms=_ms_since(t0),
            )

        prior = REGISTRY.append_run(session_id, run_id)
        envelope = wrap(
            payload={
                "run_id":            run_id,
                "request":           req.request[:500],
                "classification":    cls_payload,
                "routing":           route_payload,
                "memory_anchor":     anchor_payload,
                "persona_selection": persona_payload,
                "synthesis":         synth_payload,
                "total_ms":          _ms_since(t0),
            },
            run_id=run_id,
            ts=anchor_payload["timestamp"],
            glyph_pipeline="📥 → 🔄 → 🧊 → 🥥 → 📤",
            session_id=session_id,
            prior_run_ids=prior,
        )
        yield _sse("complete", envelope_to_dict(envelope))

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


def _ms_since(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


@app.get("/cno/audit", tags=["audit"])
def list_audit_runs(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    """List recent runs (header summary). Most recent first."""
    if PIPELINE.audit is None:
        raise HTTPException(status_code=503, detail="audit log not configured")
    runs = PIPELINE.audit.list_runs(limit=limit, offset=offset)
    return {
        "count": len(runs),
        "runs": [r.__dict__ for r in runs],
    }


@app.get("/cno/audit/stats", tags=["audit"])
def get_audit_stats(window: int = Query(50, ge=1, le=500)):
    """
    Aggregated stats for dashboard widgets:
      - latency_series: total_ms over the most recent `window` runs
      - clarity_series: clarity_score over the same window
      - sublayer_distribution: counts by sublayer
      - persona_modality_matrix: counts by (modality, persona_style)
    """
    if PIPELINE.audit is None:
        raise HTTPException(status_code=503, detail="audit log not configured")
    return PIPELINE.audit.get_stats(window=window)


@app.post("/cno/audit/backup", tags=["audit"])
def backup_audit_db(dest: str | None = Query(None, description="optional destination filename")):
    """
    Online snapshot of the SQLite audit DB. Safe to call while the DB is being
    written. Default destination: backups/cno_audit-<UTCtimestamp>.db next to
    the live DB. Returns the absolute path of the snapshot.
    """
    if PIPELINE.audit is None:
        raise HTTPException(status_code=503, detail="audit log not configured")

    from datetime import datetime, timezone
    from pathlib import Path

    live = PIPELINE.audit.db_path
    if dest:
        target = Path(dest)
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = live.parent / "backups" / f"{live.stem}-{stamp}{live.suffix}"

    written = PIPELINE.audit.backup(target)
    return {"backup_path": str(written.resolve()), "size_bytes": written.stat().st_size}


@app.get("/cno/artifact/{run_id}", tags=["audit"], response_class=PlainTextResponse)
def get_artifact(run_id: str):
    """
    CSTM §4 emitted artifact: markdown body with 10-field YAML frontmatter.
    Suitable for archival, downstream ingestion, or pasting into a wiki.
    """
    if PIPELINE.audit is None:
        raise HTTPException(status_code=503, detail="audit log not configured")
    header = PIPELINE.audit.get_run(run_id)
    if header is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")

    md = make_metadata(run_id=header.run_id, ts=header.ts)
    body = (
        f"# CNO pipeline run\n\n"
        f"**Request:** {header.request}\n\n"
        f"**Routing:** {header.sublayer} (modality={header.modality}, "
        f"type={header.request_type}, tone={header.tone})\n\n"
        f"**Persona:** {header.persona_style} · "
        f"**Clarity:** {header.clarity_score} · "
        f"**Total:** {header.total_ms or '—'} ms\n\n"
        f"## Synthesis\n\n{header.synthesis_body}\n"
    )
    return render_artifact(md, body)


@app.get("/cno/audit/{run_id}", tags=["audit"])
def get_audit_run(run_id: str):
    """Drill-down: full per-node trace for a single run."""
    if PIPELINE.audit is None:
        raise HTTPException(status_code=503, detail="audit log not configured")
    header = PIPELINE.audit.get_run(run_id)
    if header is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    crossings = PIPELINE.audit.get_crossings(run_id)
    return {
        "header": header.__dict__,
        "crossings": [c.__dict__ for c in crossings],
    }


@app.get("/", tags=["meta"])
def root():
    return {
        "service":     "Strata Core — local-first NLP inference pipeline",
        "namespace":   "cno",
        "stages":      "input → router → memory → persona → output_synth",
        "endpoints":   [
            "POST /cno/process",
            "POST /cno/process/stream",
            "POST /cno/node/{node_name}",
            "GET /cno/state",
            "POST /cno/state/reset",
            "GET /cno/audit",
            "GET /cno/audit/stats",
            "GET /cno/audit/{run_id}",
            "GET /healthz",
            "GET /docs",
            "GET /ui",
        ],
    }


# --- Static frontend mount (only if built) ---
_static_dir = Path(__file__).parent / "static"
if _static_dir.exists() and (_static_dir / "index.html").exists():
    app.mount("/ui", StaticFiles(directory=_static_dir, html=True), name="ui")
