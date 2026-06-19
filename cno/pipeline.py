"""
CNO Pipeline — drives a request through all 5 nodes sequentially.

Per Sentinel Forge System Core Directive: linear flow
    Input -> Router -> Memory -> Persona -> OutputSynth

Each crossing is logged with module tags for symbolic traceability and (when an
AuditLog is attached) persisted as one row per node crossing in SQLite.
"""

from __future__ import annotations
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .nodes import (
    InputNode, RouterNode, MemoryNode, PersonaNode, OutputSynthNode,
    Modality,
)
from .audit import AuditLog
from .sinks import load_sink_from_env
from .llm import load_classifier_from_env


@dataclass
class CNOPipelineResult:
    run_id:            str
    request:           str
    classification:    dict
    routing:           dict
    memory_anchor:     dict
    persona_selection: dict
    synthesis:         dict
    module_tags:       list[str] = field(default_factory=list)
    timestamp:         str = ""
    glyph_pipeline:    str = "📥 → 🔄 → 🧊 → 🥥 → 📤"


class CNOPipeline:
    """Linear 5-node CNO pipeline. Singleton-friendly."""

    def __init__(self, audit: Optional[AuditLog] = None, memory: Optional[MemoryNode] = None):
        # Optional local-LLM backend for the input classifier; None → heuristics
        # (default). Enable with STRATA_LLM_BACKEND=local. See cno/llm.py.
        self.input    = InputNode(llm=load_classifier_from_env())
        self.router   = RouterNode()
        self.memory   = memory if memory is not None else MemoryNode()
        self.persona  = PersonaNode()
        self.synth    = OutputSynthNode()
        self.audit    = audit

    def process(self, request: str, draft_response: str = "", modality_hint: Modality | None = None) -> CNOPipelineResult:
        run_id = uuid.uuid4().hex
        t0 = time.perf_counter()
        tags: list[str] = []

        # 1. INPUT
        cls = self.input.classify(request, modality_hint=modality_hint)
        cls_payload = {
            "modality": cls.modality.value,
            "request_type": cls.request_type.value,
            "tone": cls.tone.value,
            "raw_length": cls.raw_length,
            "modality_confidence":     cls.modality_confidence,
            "request_type_confidence": cls.request_type_confidence,
            "tone_confidence":         cls.tone_confidence,
        }
        tags.append(f"[Input Node: modality={cls.modality.value}, type={cls.request_type.value}, tone={cls.tone.value}]")
        self._audit(run_id, 1, "input", {"request": request[:200]}, cls_payload)

        # 2. ROUTER
        route = self.router.route(cls)
        route_payload = {"sublayer": route.sublayer.value, "rationale": route.rationale}
        tags.append(f"[Router Node: Activated → {route.sublayer.value}]")
        self._audit(run_id, 2, "router", cls_payload, route_payload)

        # 3. MEMORY
        anchor = self.memory.anchor(
            summary=request[:200],
            request_type=cls.request_type.value,
            tone=cls.tone.value,
        )
        anchor_payload = {"timestamp": anchor.timestamp, "summary": anchor.summary}
        tags.append(f'[Memory Node: Context Anchor = "{anchor.summary[:60]}..."]')
        self._audit(run_id, 3, "memory", {"summary": anchor.summary}, anchor_payload)

        # 4. PERSONA
        persona = self.persona.select(cls, route)
        persona_payload = {
            "style": persona.style.value,
            "rationale": persona.rationale,
            "confidence": persona.confidence,
        }
        tags.append(f"[Persona Node: style={persona.style.value} ({persona.rationale})]")
        self._audit(run_id, 4, "persona", {"classification": cls_payload, "routing": route_payload}, persona_payload)

        # 5. OUTPUT SYNTH
        if not draft_response:
            draft_response = f"[draft based on {route.sublayer.value} routing of: {request[:100]}]"
        synth = self.synth.synthesize(draft_response, persona)
        synth_payload = {
            "body": synth.body[:500],
            "clarity_score": synth.clarity_score,
            "style_applied": synth.style_applied,
            "transforms": list(synth.transforms),
        }
        tags.append(f"[Output Synth Node: clarity={synth.clarity_score}, style={synth.style_applied}]")
        self._audit(run_id, 5, "synth", {"draft": draft_response[:200], "persona": persona_payload}, synth_payload)

        total_ms = int((time.perf_counter() - t0) * 1000)

        # Run header (one row per /cno/process call)
        if self.audit is not None:
            self.audit.record_run_header(
                run_id=run_id,
                request=request,
                modality=cls.modality.value,
                request_type=cls.request_type.value,
                tone=cls.tone.value,
                sublayer=route.sublayer.value,
                persona_style=persona.style.value,
                clarity_score=synth.clarity_score,
                synthesis_body=synth.body,
                total_ms=total_ms,
            )

        return CNOPipelineResult(
            run_id=run_id,
            request=request[:500],
            classification=cls_payload,
            routing=route_payload,
            memory_anchor=anchor_payload,
            persona_selection=persona_payload,
            synthesis=synth_payload,
            module_tags=tags,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def _audit(self, run_id: str, step: int, node: str, payload_in: dict, payload_out: dict) -> None:
        if self.audit is None:
            return
        self.audit.record_node_crossing(run_id, step, node, payload_in, payload_out)


PIPELINE = CNOPipeline(
    audit=AuditLog(),
    memory=MemoryNode(sink=load_sink_from_env()),
)
