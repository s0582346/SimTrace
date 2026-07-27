"""Rebuild a fresh `FactoryModel` from a recorded build spec.

Live components can't be re-instantiated: a delay string like "exp(5)" is
resolved to a sampler before it reaches the node, leaving the original string
unrecoverable, and every node is bound to one `simpy.Environment` that SimPy
will not re-run to an `until` at or below its current clock. So
`FactoryModel.record` logs each create_*/connect call's raw arguments, and
`build_from_spec` replays that log into a new model — which is how
`run_replications` gets a genuinely independent model per run.

Replay goes through the same builders the original calls used, so validation,
defaults, and construction stay in one place.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from simtrace.model import FactoryModel
from simtrace.tools.builders import (
    create_buffer,
    create_combiner,
    create_conveyor,
    create_fleet,
    create_machine,
    create_sink,
    create_source,
    create_splitter,
)

# Spec op name -> the builder that replays it. `connect` is resolved lazily in
# build_from_spec to avoid a circular import (graph imports model, not builders).
_BUILDERS: Dict[str, Callable[..., dict]] = {
    "create_source": create_source,
    "create_sink": create_sink,
    "create_machine": create_machine,
    "create_splitter": create_splitter,
    "create_combiner": create_combiner,
    "create_buffer": create_buffer,
    "create_conveyor": create_conveyor,
    "create_fleet": create_fleet,
}


def build_from_spec(spec: List[Dict[str, Any]]) -> FactoryModel:
    """Replay a recorded build spec into a new, unrun `FactoryModel`.

    Args:
        spec: the ordered build log from `FactoryModel.spec`. Order is
            preserved: in-edge priority under "FIRST_AVAILABLE" follows connect
            order.

    Returns:
        A fresh model with the same graph and a clock at 0 — never the session
        singleton, so the caller can run it without touching session state.

    Raises:
        ValueError: if the spec is empty or names an unknown op.
    """
    from simtrace.tools.simulation.graph import connect  # lazy: see _BUILDERS

    if not spec:
        raise ValueError(
            "Cannot rebuild an empty model: no components have been created. "
            "Build the model with the create_* tools first."
        )

    model = FactoryModel()
    for step in spec:
        op = step["op"]
        kwargs = step["kwargs"]
        if op == "connect":
            connect(model=model, **kwargs)
            continue
        builder = _BUILDERS.get(op)
        if builder is None:
            raise ValueError(f"Unknown build step '{op}' in spec.")
        builder(model=model, **kwargs)

    return model
