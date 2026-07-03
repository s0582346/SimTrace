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
missing or appeared from nowhere — and that is the alarm to raise.

## The check, in full

The three easy places to see are what was **delivered**, what is still **waiting**
between stations, and what was **thrown away** — each can be counted where it
sits. The subtle one is what is still **being worked on** inside a station, and
whether we can see it directly changes what the check can prove.

```mermaid
flowchart TD
    start(["the run has finished"]) --> mixed{"does the line bundle or<br/>unbundle items anywhere?"}

    mixed -->|yes| mixedout(["counts are in mixed units —<br/>no clean verdict; name those stations"])

    mixed -->|no| made["count everything that was made"]
    made --> visible["count the three we can always see:<br/>delivered, waiting, thrown away"]
    visible --> seen{"can we also see directly<br/>what is being worked on?"}

    seen -->|yes| sum["add up all four places"]
    sum --> cmp{"compare that sum<br/>with what was made"}
    cmp -->|equal| okA(["every item accounted for"])
    cmp -->|sum is smaller| lost(["items went missing —<br/>raise the alarm"])
    cmp -->|sum is larger| moreA(["items appeared from nowhere —<br/>raise the alarm"])

    seen -->|no| left["take the leftover:<br/>made, minus the three we can see"]
    left --> neg{"is that leftover<br/>below zero?"}
    neg -->|yes| moreB(["more counted than was made —<br/>impossible: raise the alarm"])
    neg -->|no| okB(["consistent — but read the caveat below:<br/>a silent loss could hide in this leftover"])

    classDef ok fill:#e6f4ea,stroke:#137333,color:#0b3d1f;
    classDef alarm fill:#fce8e6,stroke:#c5221f,color:#5c0d0a;
    classDef caution fill:#fef7e0,stroke:#b06000,color:#4d2c00;
    class okA,okB ok;
    class lost,moreA,moreB alarm;
    class mixedout caution;
```

## The asymmetry worth knowing

Made-up items and missing items are **not** equally easy to catch.

- **Items appearing from nowhere** is always caught. It is logically impossible
  for the four places to hold more than was ever made, so if the numbers say they
  do, something is definitely wrong — no judgement call needed.

- **Items going missing** is only caught if we can see, on its own, what is being
  worked on. If we can, a shortfall in the total is a true signal of loss. If we
  **can't** — and instead call "whatever is unaccounted for" the work in progress
  — then a genuinely lost item quietly gets folded into that leftover and looks
  like normal work still on the bench. The books balance, but only because the
  missing item was silently reclassified rather than found.

So a clean pass means *consistent*, which is not quite the same as *nothing was
lost*. The stronger guarantee needs an independent count of what is on the bench,
not a leftover.

## When a simple head-count isn't enough

The whole check assumes one item in means one item out at every station. Most
stations honour that. But some deliberately change how many items there are:

- a **bundling** station takes several items and combines them into a single one,
- an **unbundling** station takes one item and separates it back into several.

Where that happens, a plain head-count can't be reconciled: the very same goods
are counted as one number before the station and a different number after it.
Rather than guess, the check declines a clean verdict and names the stations
responsible. For lines with no bundling or unbundling, it gives a definite
answer.

---

*The concept behind the `verify_conservation` check in
`src/simtrace/tools/validation.py`.*
