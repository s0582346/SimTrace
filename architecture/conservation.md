# Conservation check — nothing gets lost

Everything the line makes has to end up somewhere. When the run stops, every
item that was ever made sits in exactly one of four places:

```mermaid
flowchart LR
    made(["everything<br/>the line made"])

    subgraph fate["every item is in exactly one of these"]
        direction TB
        delivered["<b>delivered</b><br/><i>reached the end</i>"]
        waiting["<b>waiting</b><br/><i>between two stations</i>"]
        working["<b>being worked on</b><br/><i>inside a station</i>"]
        thrown["<b>thrown away</b><br/><i>dropped, nowhere to put it</i>"]
    end

    made ==> delivered
    made ==> waiting
    made ==> working
    made ==> thrown

    classDef good fill:#e6f4ea,stroke:#137333,color:#0b3d1f;
    classDef hold fill:#e8f0fe,stroke:#1967d2,color:#0b2a5b;
    classDef drop fill:#fce8e6,stroke:#c5221f,color:#5c0d0a;
    class delivered good;
    class waiting,working hold;
    class thrown drop;
```

Nothing else can happen to an item. So the amount that was made must equal the
amount spread across those four places:

> **made  =  delivered  +  waiting  +  being worked on  +  thrown away**

If the two sides match, every item is accounted for. If they don't, items went
missing or appeared from nowhere — and that is the alarm to raise. The check also
reports the four numbers themselves and says what they show: what was thrown
away, and how much never left the line.

## The check, in full

Each of the four places is counted on its own, not derived from the others:

- **delivered** is counted by the sinks,
- **waiting** is the level of every buffer, conveyor and fleet,
- **thrown away** is counted by every node that drops an item,
- **being worked on** is counted directly from the nodes: each machine keeps a
  list of the items it is currently working on or waiting to hand over, and a
  source holds the one item it generated but could not hand over yet.

```mermaid
flowchart TD
    start(["the run has finished"]) --> mixed{"does the line bundle or<br/>unbundle items anywhere?"}

    mixed -->|yes| mixedout(["counts are in mixed units —<br/>no comparison; name those stations"])

    mixed -->|no| made["count everything that was made"]
    made --> four["count the four places,<br/>each on its own"]
    four --> sum["add up all four places"]
    sum --> cmp{"compare that sum<br/>with what was made"}
    cmp -->|equal| okA(["every item accounted for"])
    cmp -->|sum is smaller| lost(["items went missing —<br/>raise the alarm"])
    cmp -->|sum is larger| moreA(["items appeared from nowhere —<br/>raise the alarm"])

    classDef ok fill:#e6f4ea,stroke:#137333,color:#0b3d1f;
    classDef alarm fill:#fce8e6,stroke:#c5221f,color:#5c0d0a;
    classDef caution fill:#fef7e0,stroke:#b06000,color:#4d2c00;
    class okA ok;
    class lost,moreA alarm;
    class mixedout caution;
```

## Why counting the fourth place matters

Made-up items and missing items are **not** equally easy to catch, and how the
fourth place is obtained decides it.

If **being worked on** were taken as "whatever is left over" — made, minus the
three we can see — the two sides would match by construction. A genuinely lost
item would quietly be folded into that leftover and look like normal work still
on the bench. The books would balance only because the missing item was
reclassified rather than found, and a pass would mean *consistent*, not *nothing
was lost*.

Counting it directly removes that escape. An item that vanishes is then in none
of the four places, and the gap between the two sides is exactly the number lost.
The check was compared against the leftover on 140 runs of lines with several
workers per machine, conveyors, fleets, all three selection rules and blocking
and non-blocking nodes, and the two agreed every time. Removing items from a
buffer unseen made them differ by exactly the number removed.

A model built through the tools cannot produce such a gap, because every node
counts what it drops. A non-zero gap therefore points at a fault in the simulation
itself, not at the model. What a model *can* show is the split: items dropped at a
non-blocking node, and how much of the output never left the line. The check
reports both as findings and leaves the judgement to whoever knows how the line
was meant to work.

## When a simple head-count isn't enough

The whole check assumes one item in means one item out at every station. Most
stations honour that. But some deliberately change how many items there are:

- a **bundling** station takes several items and combines them into a single one,
- an **unbundling** station takes one item and separates it back into several.

Where that happens, a plain head-count can't be reconciled: the very same goods
are counted as one number before the station and a different number after it.
Rather than guess, the check reports no comparison for such lines and names the
stations responsible, so a reader knows that counts taken before and after them
are not comparable.

---

*The concept behind the `verify_conservation` check in
`src/simtrace/tools/validation/conservation.py`.*
