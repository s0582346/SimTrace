"""Post-run verification tools that check a *completed* simulation.

Where `simulation.run_simulation` executes the model, these tools inspect what
happened, each in its own submodule with its own helpers:

  - `conservation.verify_conservation` — "are all generated items accounted
    for?" via a mass-balance over the nodes' and edges' ground-truth counters.
  - `item_flow.verify_item_flow` — "did each delivered item follow a proper
    route?" by checking every item that reached a sink against the wired graph.

Both are *dynamic* checks (they need a run to have happened), distinct from the
static graph checks a future `validate_model` will do *before* a run. This
package re-exports both so `from simgen.tools.validation import verify_*` and
`validation.verify_*` keep working as a single tool surface.
"""

from __future__ import annotations

from simgen.tools.validation.conservation import verify_conservation
from simgen.tools.validation.item_flow import verify_item_flow

__all__ = ["verify_conservation", "verify_item_flow"]
