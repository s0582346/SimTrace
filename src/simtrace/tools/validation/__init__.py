"""Verification tools that check the model, each in its own submodule.

One is static and runs *before* a simulation:

  - `model_check.validate_model` — "is the graph wired the way it has to be?"
    by reading the wired graph and the recorded build spec: edge cardinality,
    unconnected edges, Source-to-Sink reachability, and the parameter
    mismatches that only become visible once wiring is done.

The other two are dynamic and inspect what a *completed* run did:

  - `conservation.verify_conservation` — "are all generated items accounted
    for?" via a mass-balance over the nodes' and edges' ground-truth counters.
  - `item_flow.verify_item_flow` — "did each delivered item travel a complete
    wired route?" by replaying the node/edge trail of every item that reached a
    sink against the wiring, Source to Sink.

This package re-exports all three so `from simtrace.tools.validation import ...`
and `validation.<tool>` keep working as a single tool surface.
"""

from __future__ import annotations

from simtrace.tools.validation.conservation import verify_conservation
from simtrace.tools.validation.item_flow import verify_item_flow
from simtrace.tools.validation.model_check import validate_model

__all__ = ["validate_model", "verify_conservation", "verify_item_flow"]
