"""Post-run item-flow check: did each delivered item follow a proper route?

`verify_item_flow` replays each item's node sequence. Captured items live into
`model.item_paths` during the run. See `architecture/item_flow.md` for what the check means and why.
"""

from __future__ import annotations

from simtrace.model import FactoryModel
from simtrace.model import get_model as get_session_model


def _wired_adjacency(model: FactoryModel) -> dict[str, set[str]]:
    """Map each node id to the set of node ids directly downstream of it.

    Built from the wired edges only: an edge contributes `src -> dest` once both
    ends are connected. Unwired edges (src_node/dest_node still None) are skipped.
    This is the graph a proper item path must move along.
    """
    adjacency: dict[str, set[str]] = {node_id: set() for node_id in model.nodes}
    for edge in model.edges.values():
        src = getattr(edge, "src_node", None)
        dest = getattr(edge, "dest_node", None)
        if src is not None and dest is not None:
            adjacency.setdefault(src.id, set()).add(dest.id)
    return adjacency


def _first_bad_hop(
    path: list[str],
    adjacency: dict[str, set[str]],
) -> tuple[str, str] | None:
    """Return the first consecutive (a, b) in `path` with no wired edge a->b.

    None means every hop follows a wired edge (the interior is a connected route).
    A repeated node (a == b, e.g. an item narrated twice at the same node) is not
    a hop and is skipped rather than flagged.
    """
    for a, b in zip(path, path[1:]):
        if a == b:
            continue
        if b not in adjacency.get(a, set()):
            return (a, b)
    return None


def _bad_hop_reason(
    a: str,
    b: str,
    adjacency: dict[str, set[str]],
) -> str:
    """Explain why the hop a->b is invalid, naming where `a` actually connects.

    Points the reader at the wiring instead of just restating the bad hop: it
    lists the nodes `a` really connects to downstream (so the intended next node
    is obvious), or says it is a dead-end when `a` connects nowhere.
    """
    downstream = sorted(adjacency.get(a, set()))
    if downstream:
        connects = ", ".join(downstream)
        return f"{b} is not reachable from {a}; {a} connects to {connects}"
    return f"{b} is not reachable from {a}; {a} connects nowhere downstream"


def verify_item_flow(*, model: FactoryModel | None = None) -> dict:
    """Check that every delivered item followed a proper route through the graph.

    For each item that reached a sink in the last run, replays the node sequence
    captured on `model.item_paths` and checks it is a connected route along wired
    edges that ends at a sink. A clean item *passes* (counted, no detail); an item
    whose path takes a hop with no wired edge behind it is *improper* and reported
    as improper.

    Only *delivered* items (those whose path ends at a sink) are judged: items
    still stuck in an edge, mid-process, or discarded are the concern of
    `verify_conservation`, not this check. The path may start
    downstream of the source, so the route is validated as a connected sub-path ending at a
    sink, not required to start at a source.

    Requires a prior `run_simulation`.

    Returns a summary dict:
        {
          "now": <sim clock at end of last run>,
          "delivered": int,          # items that reached a sink
          "passed": int,             # delivered items whose route is proper
          "all_proper": bool,        # passed == delivered
          "improper": [              # only the failures carry detail
            {
              "item": item_id,
              "path": [node, ...],           # the observed node sequence
              "bad_hop": [from_node, to_node],  # first hop with no wired edge
              "reason": str,
            }, ...
          ],
        }
    """
    model = model if model is not None else get_session_model()

    if model.env.now == 0:
        raise ValueError(
            "No simulation has run yet. "
            "Call run_simulation before verify_item_flow."
        )

    adjacency = _wired_adjacency(model)
    sink_ids = {
        node_id
        for node_id, node in model.nodes.items()
        if type(node).__name__ == "Sink"
    }

    delivered = 0
    passed = 0
    improper: list[dict] = []

    for item_id, path in model.item_paths.items():
        # judge only items who arrived to sink
        if not path or path[-1] not in sink_ids:
            continue
        delivered += 1 # track how many items arrived

        bad_hop = _first_bad_hop(path, adjacency)
        if bad_hop is None:
            passed += 1
        else:
            a, b = bad_hop
            improper.append({
                "item": item_id,
                "path": list(path),
                "bad_hop": [a, b],
                "reason": _bad_hop_reason(a, b, adjacency),
            })

    return {
        "now": model.env.now,
        "delivered": delivered,
        "passed": passed,
        "all_proper": not improper,
        "improper": improper,
    }
