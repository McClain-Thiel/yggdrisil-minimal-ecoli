import gzip
import json
from pathlib import Path

import pandas as pd
import pytest

from yggdrisil_ecoli.data.evidence import validate_genes
from yggdrisil_ecoli.data.gff import parse_ncbi_gff


@pytest.fixture
def genes() -> pd.DataFrame:
    frame, _ = parse_ncbi_gff(Path(__file__).parent / "fixtures/mg1655_excerpt.gff3")
    return validate_genes(
        frame.assign(lb_call_raw="NE", lb_ecipkm=3.0, m9_call_raw="NE", m9_ecipkm=3.0)
    )


@pytest.fixture
def edison_service(monkeypatch):
    import httpx

    calls = {
        "submit": [],
        "poll": [],
        "clients": [],
        "status": "success",
        "lost": False,
        "blob": False,
        "refresh": False,
        "logins": 0,
    }
    state = {
        "state": {
            "response": {
                "answer": {
                    "formatted_answer": "Cited evidence [1]. Reference: doi:10.example/test",
                    "has_successful_answer": True,
                }
            },
            "info": {"usage": {"total_cost": 0.25, "total_queries": 4}},
        }
    }

    def handle(request):
        if request.url.path == "/auth/login":
            assert json.loads(request.content)["api_key"] == "test-placeholder"
            calls["logins"] += 1
            return httpx.Response(200, json={"access_token": "test-session"})
        if request.url.host == "storage.example.org":
            assert "Authorization" not in request.headers
            return httpx.Response(
                200, content=gzip.compress(json.dumps(state).encode())
            )
        assert request.headers["Authorization"] == "Bearer test-session"
        if request.url.path == "/v0.1/crows":
            body = json.loads(request.content)
            calls["submit"].append(body)
            if calls["lost"]:
                raise httpx.ConnectError("Submission response lost", request=request)
            return httpx.Response(200, json={"trajectory_id": body["id"]})
        assert request.url.path.startswith("/v0.2/trajectories/")
        if calls["refresh"]:
            calls["refresh"] = False
            return httpx.Response(401)
        lite = request.url.params["lite"] == "true"
        calls["poll"].append((request.url.path.rsplit("/", 1)[1], lite))
        if lite:
            return httpx.Response(200, json={"status": calls["status"]})
        frame = (
            {"state": None, "download_url": "https://storage.example.org/result"}
            if calls["blob"]
            else {"state": state}
        )
        return httpx.Response(
            200, json={"environment_frame": frame, "crow": "paperqa3"}
        )

    original = httpx.AsyncClient

    class Client(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, transport=httpx.MockTransport(handle))
            calls["clients"].append(self)

    monkeypatch.setenv("EDISON_PLATFORM_API_KEY", "test-placeholder")
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    return calls


@pytest.fixture
def adk_model(monkeypatch):
    """Native ADK runners use scripted models; network calls cannot occur."""
    from typing import Any

    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types
    from yggdrisil.agents import adk

    from yggdrisil_ecoli import research

    class ScriptedModel(BaseLlm):
        respond: Any
        requests: list[Any] = []

        async def generate_content_async(self, llm_request, stream=False):
            self.requests.append(llm_request.model_copy(deep=True))
            parts = self.respond(llm_request)
            yield LlmResponse(
                content=types.Content(role="model", parts=parts),
                usage_metadata=types.GenerateContentResponseUsageMetadata(
                    prompt_token_count=10, candidates_token_count=5
                ),
                custom_metadata={"cost_usd": "0.001"},
            )

    def install(respond):
        model = ScriptedModel(model="fixture", respond=respond)
        monkeypatch.setattr(adk, "make_model", lambda *args, **kwargs: model)
        monkeypatch.setattr(research, "make_model", lambda *args, **kwargs: model)
        return model

    return install
