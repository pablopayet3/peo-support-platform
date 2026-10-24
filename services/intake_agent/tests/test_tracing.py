"""LangSmith tracing must never change what the agent does, and must never record clients or raw text."""
import asyncio
from dataclasses import dataclass

from intake_agent import tracing
from intake_agent.llm import Extraction


@dataclass
class Out:
    action: str
    confidence: float


def test_outputs_are_plain_dicts():
    assert tracing._outputs(Out("answered", 0.9)) == {"action": "answered", "confidence": 0.9}
    ex = Extraction(is_life_event=True, event_type="newborn", confidence=0.92)
    assert tracing._outputs(ex)["event_type"] == "newborn"
    assert tracing._outputs("ok") == {"output": "ok"}


def test_traced_function_behaves_the_same(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)

    @tracing.traced("test step", keep=("ticket_id",))
    async def step(ticket_id: int, client: object) -> Out:
        return Out("handed_off", 0.95)

    assert asyncio.run(step(7, object())) == Out("handed_off", 0.95)


def test_only_kept_inputs_are_recorded(monkeypatch):
    seen = {}

    def fake_traceable(**kwargs):
        seen.update(kwargs)
        return lambda fn: fn

    monkeypatch.setattr(tracing, "_traceable", fake_traceable)
    tracing.traced("ticket intake", keep=("ticket_id", "request_id"))(lambda **k: None)
    recorded = seen["process_inputs"]({"ticket_id": 7, "request_id": "r1", "zd": object(), "integ": object()})
    assert recorded == {"ticket_id": 7, "request_id": "r1"}
    assert seen["name"] == "ticket intake"
