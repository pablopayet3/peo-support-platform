"""LangSmith tracing: every AI decision recorded as a trace, so any case can be replayed and checked.

One trace per request (an email, a chat message), with the model call nested inside it: what the model
read (the redacted conversation, never the raw text), what it extracted, its confidence, which model
answered (or the rules fallback), what the code then decided, and how long each step took.

Tracing is off unless the environment turns it on (LANGSMITH_TRACING=true and LANGSMITH_API_KEY,
optionally LANGSMITH_PROJECT). Off, the decorator does nothing, so tests and local runs send nothing.
A tracing failure never stops a request: the LangSmith client sends in the background and logs errors.
"""
from dataclasses import asdict, is_dataclass
from typing import Any, Callable

try:
    from langsmith import traceable as _traceable
except ImportError:  # the agent still runs without the package
    _traceable = None


def _outputs(out: Any) -> dict:
    if is_dataclass(out):
        return asdict(out)
    if hasattr(out, "model_dump"):
        return out.model_dump()
    return out if isinstance(out, dict) else {"output": out}


def traced(name: str, run_type: str = "chain", keep: tuple[str, ...] | None = None) -> Callable:
    """Trace a function. keep: the only inputs recorded (clients and API objects are left out)."""
    def wrap(fn: Callable) -> Callable:
        if _traceable is None:
            return fn
        kwargs: dict = {"name": name, "run_type": run_type, "process_outputs": _outputs,
                        "tags": ["northwind", "intake-agent"]}
        if keep is not None:
            kwargs["process_inputs"] = lambda inputs: {k: v for k, v in inputs.items() if k in keep}
        return _traceable(**kwargs)(fn)
    return wrap
