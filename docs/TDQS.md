# MCP tool quality — TDQS run (pre-publication)

Records a **TDQS** (Tool Definition Quality Score) pass over swap's MCP exchanger
tools before listing the server publicly (e.g. the Glama registry). TDQS grades how
well each tool *definition* tells an agent what the tool does and when to use it —
it drives tool-selection quality, not runtime behaviour.

> Spec: <https://github.com/glama-ai/tool-definition-quality-score> (the repo
> publishes the **methodology**, not a runnable CLI). The pipeline is
> `context signals → hard gates → LLM rubric → aggregation`. We reproduced it
> locally against the live `tools/list` output: the deterministic parts (weights,
> tiers, context signals, hard gates) by script (§5), the per-dimension rubric
> grades by the published anchors (§2, justified per tool in §4). Treat absolute
> numbers as ±0.1–0.3 vs the
> official Glama scorer; the tier outcome (all A) is robust. Once listed, compare
> against the official score at `glama.ai/mcp`.

Last run: **2026-06-15**, against `swap/mcp_app.py` (4 tools).

## 1. Result

All four tools score tier **A** (passing bar is **B ≥ 3.0**); server average
**4.75 / 5** after the fixes in §3. No smells (dimension < 3), no hard-gate flags.

| Tool | TDQS | Tier | Smells |
|------|------|------|--------|
| `buy_emc` | 5.0 | A | — |
| `cancel_order` | 4.8 | A | — |
| `get_order_status` | 4.7 | A | — |
| `get_swap_config` | 4.5 | A | — |
| **server avg** | **4.75** | — | — |

Baseline (before §3) was avg **4.62** — already all-A; the only systematic weakness
was *Conciseness & Structure*, capped at 4 by docstring-indentation leaking into the
published descriptions.

## 2. Rubric (six dimensions, weighted)

| Dimension | Weight | What earns a 5 |
|-----------|:------:|----------------|
| Purpose Clarity | 25% | specific verb + resource; distinguishes the tool from siblings |
| Usage Guidelines | 20% | explicit when / when-not + named alternative tools |
| Behavioral Transparency | 20% | discloses side effects / prerequisites / constraints beyond annotations (1 if it **contradicts** annotations) |
| Parameter Semantics | 15% | adds meaning beyond the schema; baseline 4 for zero-param, 3 if schema description coverage > 80% |
| Conciseness & Structure | 10% | front-loaded, every sentence earns its place, no noise |
| Contextual Completeness | 10% | complete for the tool's complexity; an output schema removes the return-value burden |

`TDQS = Σ(score × weight)`, score scale 1–5. **Tiers:** A ≥ 3.5 · B ≥ 3.0 (passing)
· C ≥ 2.0 · D ≥ 1.0 · F < 1.0. **Hard gates:** empty/whitespace description →
all 1s (`No Description`); description equal to the name/title → all 1s
(`Tautological Description`); description contradicting annotations → Behavioral
Transparency = 1 (`Annotation Contradiction`). Our tools trip none.

## 3. Design choices that score well (and the fixes applied)

What was already in place and why it scores:

- **`verb_noun` names** that distinguish siblings: `get_swap_config`, `buy_emc`,
  `get_order_status`, `cancel_order`. (`swap_config`/`order_status` were renamed to
  the `get_*` form for this.)
- **A `title` per tool**, longer than and distinct from the name.
- **Behavior annotations** so descriptions needn't restate them and can't contradict
  them: `readOnlyHint` (config/status), `destructiveHint` (cancel), `idempotentHint`,
  `openWorldHint` (buy reaches the chain/adapter).
- **Pydantic return types** (`WebConfigResponse` / `WebOrderResponse` /
  `WebStatusResponse`) so each tool emits an **output schema** → no return-value prose
  needed (Contextual Completeness).
- **When / when-not + named siblings** in every description (e.g. buy_emc points at
  `get_swap_config` first and `get_order_status` / `cancel_order` after; status and
  cancel cross-reference each other).
- **Full parameter descriptions** with formats (amount range, EMC address forms).

Two fixes made during this run:

1. **Strip docstring indentation from published descriptions.** A `tool()` wrapper
   feeds each docstring through `inspect.cleandoc`, so the docstring stays the single
   source of truth in the source while the emitted description has no leading
   whitespace. → *Conciseness & Structure* 4 → 5 across all four tools.
2. **Enrich the `token` param**: "opaque order handle returned by buy_emc; pass it
   back unchanged".

## 4. Per-dimension justifications

Grades are post-fix (§3). Each line is `dimension — score: rationale`.

### `buy_emc` — 5.0 (A)

- **Purpose Clarity — 5:** Opens with a specific verb+resource ("Open an order to buy
  EMC with USDT … get back a `deposit_address` plus the EXACT `amount_usdt`") and is
  unambiguously the action tool against its read-only/cancel siblings, which it names.
- **Usage Guidelines — 5:** Spells out the full flow (use `get_swap_config` first,
  poll `get_order_status`, `cancel_order` to abandon) and the when-not signal that each
  call opens a *new* order; alternatives are named throughout.
- **Behavioral Transparency — 5:** Discloses the non-obvious behaviour annotations
  can't carry — it does **not** move funds, the exact amount is the matching tag,
  one-way/no-refund, not idempotent, delivery is automatic on confirmation — and stays
  consistent with `openWorldHint`.
- **Parameter Semantics — 5:** 100% schema coverage; both params add real meaning (the
  valid range pointer for the amount, the legacy/bech32 forms for the address).
- **Conciseness & Structure — 5:** Long but every sentence carries a distinct,
  load-bearing step or warning; front-loaded on the primary action and its output.
- **Contextual Completeness — 5:** The most complex tool, fully covered (flow,
  constraints, follow-ups), with an output schema removing return-value prose.

### `cancel_order` — 4.8 (A)

- **Purpose Clarity — 5:** Verb+resource ("Cancel a still-unpaid buy_emc order by its
  `token`, expiring it now and freeing its slot") — distinct, and names
  `get_order_status` as the read-only alternative.
- **Usage Guidelines — 5:** Explicit when ("only before you pay") and when-not ("once a
  payment is in flight … too late and this errors"), plus the named alternative for
  inspection.
- **Behavioral Transparency — 5:** `destructiveHint` matches "expiring it"; the prose
  adds what annotations can't — that it errors once paid, and that a post-cancellation
  payment matches nothing and is **not** refunded.
- **Parameter Semantics — 4:** One `token` param at 100% coverage; correct but with
  limited extra semantics beyond "the handle from buy_emc".
- **Conciseness & Structure — 5:** Tight, front-loaded on the destructive action and
  its precondition.
- **Contextual Completeness — 5:** Output schema present; the description covers the
  precondition, the failure mode and the irreversible-payment caveat.

### `get_order_status` — 4.7 (A)

- **Purpose Clarity — 5:** Specific verb+resource ("Return the current status of a
  buy_emc order by its `token`"), naming the fields returned; clearly distinct from the
  action tools and references `buy_emc`.
- **Usage Guidelines — 5:** Says when ("poll after buy_emc") and when-not (to cancel,
  use `cancel_order` instead) with the named alternative.
- **Behavioral Transparency — 4:** Read-only is already in annotations; the prose adds
  the state-machine progression (`awaiting_payment → … → notified`), which is useful,
  but there are no side effects or prerequisites to disclose.
- **Parameter Semantics — 4:** Single `token` param, 100% covered; accurate but the
  token carries limited semantics beyond being the handle from `buy_emc`.
- **Conciseness & Structure — 5:** Three compact sentences, front-loaded, no noise.
- **Contextual Completeness — 5:** Output schema present and the status fields are still
  summarised; complete for a simple read.

### `get_swap_config` — 4.5 (A)

- **Purpose Clarity — 5:** First sentence is a specific verb+resource ("Return the
  current order limits and fixed rate for buying EMC with USDT"), distinct from the
  buy/status/cancel siblings, and points the agent at `buy_emc` next.
- **Usage Guidelines — 4:** States when to use it ("Call this first to choose a valid
  amount for buy_emc") and names the sibling, but doesn't spell out an explicit
  when-not exclusion — being read-only, there is little to exclude.
- **Behavioral Transparency — 4:** Annotations declare read-only/idempotent; the prose
  adds the rate formula (`EMC = amount_usdt × emc_per_usdt`) and that it neither creates
  nor changes an order. No side effects or prerequisites to disclose.
- **Parameter Semantics — 4:** Zero-parameter tool → baseline 4; nothing to document.
- **Conciseness & Structure — 5:** Four tight sentences, front-loaded on purpose, no
  filler or indentation noise.
- **Contextual Completeness — 5:** Simple tool with an output schema, so returns need no
  prose; the description covers everything an agent needs.

## 5. How to re-run

Extract the live definitions + deterministic context signals (run from the repo
root in the project venv):

```bash
.venv/bin/python - <<'PY'
import asyncio, json
from swap import mcp_app

async def main():
    rows = []
    for t in await mcp_app.mcp.list_tools():
        props = (t.inputSchema or {}).get("properties", {})
        described = [k for k, v in props.items() if isinstance(v, dict) and v.get("description")]
        rows.append({
            "name": t.name,
            "title": t.title,
            "description": (t.description or "").strip(),
            "annotations": t.annotations and t.annotations.model_dump(exclude_none=True),
            "params": {k: props[k].get("description", "") for k in props},
            "schemaDescriptionCoverage": (len(described) / len(props)) if props else None,
            "hasOutputSchema": bool(t.outputSchema),
            "indentLeak": "\n    " in (t.description or ""),     # should be False
            "tautologyGate": (t.description or "").strip().lower()
                              in {t.name.lower(), (t.title or "").lower()},
        })
    print(json.dumps(rows, indent=2, ensure_ascii=False))

asyncio.run(main())
PY
```

Then grade each tool on the §2 anchors and aggregate:

```bash
.venv/bin/python - <<'PY'
W = dict(purpose_clarity=.25, usage_guidelines=.20, behavioral_transparency=.20,
         parameter_semantics=.15, conciseness_structure=.10, contextual_completeness=.10)
tier = lambda x: "A" if x >= 3.5 else "B" if x >= 3.0 else "C" if x >= 2.0 else "D" if x >= 1.0 else "F"
tdqs = lambda s: round(sum(s[k] * W[k] for k in W), 1)

# fill from the §2 grading (1–5 per dimension)
scores = {
    "get_swap_config":  dict(purpose_clarity=5, usage_guidelines=4, behavioral_transparency=4,
                             parameter_semantics=4, conciseness_structure=5, contextual_completeness=5),
    "buy_emc":          dict(purpose_clarity=5, usage_guidelines=5, behavioral_transparency=5,
                             parameter_semantics=5, conciseness_structure=5, contextual_completeness=5),
    "get_order_status": dict(purpose_clarity=5, usage_guidelines=5, behavioral_transparency=4,
                             parameter_semantics=4, conciseness_structure=5, contextual_completeness=5),
    "cancel_order":     dict(purpose_clarity=5, usage_guidelines=5, behavioral_transparency=5,
                             parameter_semantics=4, conciseness_structure=5, contextual_completeness=5),
}
tot = 0
for name, s in scores.items():
    v = tdqs(s); tot += v
    print(f"{name:18} TDQS {v}  tier {tier(v)}  smells={[k for k, x in s.items() if x < 3] or '—'}")
print(f"{'server avg':18} {round(tot / len(scores), 2)}")
PY
```

The first script also re-checks the cheap regressions (`indentLeak` False,
`tautologyGate` False, every tool `hasOutputSchema` True), which the test suite
asserts too (`tests/test_mcp.py::test_streamable_http_transport`).
