"""End-to-end FastAPI tests for CNO."""
from fastapi.testclient import TestClient
from cno.app import app

client = TestClient(app)


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_root_advertises_5_node_pipeline():
    r = client.get("/")
    body = r.json()
    assert "Strata Core" in body["service"]
    # advertises all five pipeline stages
    for stage in ("input", "router", "memory", "persona", "output_synth"):
        assert stage in body["stages"]


def test_process_question_routes_to_analysis():
    r = client.post("/cno/process", json={"request": "What is the meaning of canon #18?"})
    assert r.status_code == 200
    env = r.json()
    assert env["envelope_version"] == "1.0"
    assert env["spec_ref"].startswith("CSTM_Lattice")
    payload = env["payload"]
    assert payload["classification"]["request_type"] == "question"
    assert payload["routing"]["sublayer"] == "Analytical Layer"
    assert len(payload["module_tags"]) == 5


def test_process_command_routes_to_output():
    r = client.post("/cno/process", json={"request": "Build the codebase now."})
    payload = r.json()["payload"]
    assert payload["classification"]["request_type"] == "command"
    assert payload["routing"]["sublayer"] == "Output Layer"


def test_state_endpoint_returns_anchors():
    client.post("/cno/state/reset")
    client.post("/cno/process", json={"request": "first request"})
    client.post("/cno/process", json={"request": "second request"})
    r = client.get("/cno/state")
    body = r.json()
    assert body["anchor_count"] == 2


def test_modality_hint_accepted():
    r = client.post("/cno/process", json={"request": "test", "modality_hint": "voice"})
    assert r.status_code == 200
    assert r.json()["payload"]["classification"]["modality"] == "voice"


def test_modality_hint_invalid_returns_422():
    r = client.post("/cno/process", json={"request": "test", "modality_hint": "telepathy"})
    assert r.status_code == 422


def test_openapi_schema_includes_cno_endpoints():
    r = client.get("/openapi.json")
    paths = r.json()["paths"]
    assert "/cno/process" in paths
    assert "/cno/state" in paths
