# Stage 7 — AI engineering planner connected to the router

Version 0.7.0-stage7. The AI decides **what** to attempt; the deterministic router
decides **how**; the Stage 3 engine decides **legality**; the user decides
**acceptance**.

```
natural language ─▶ LLM JSON (schema v2) ─▶ schema + semantic/rule validation
   ─▶ user approval ─▶ ExecuteAIProposalCommand (bus) ─▶ route_bridge.plan_from_command
   ─▶ Router / BoardRouter / optimiser ─▶ validated candidates ─▶ preview
   ─▶ user Accept ─▶ working board (undoable)      ROUTER RESULT facts ─▶ next AI turn
```

* **No AI geometry.** The schema is strict (unknown fields such as `segments` or
  KiCad text are rejected); AI areas become *soft* corridors; widths / via limits /
  layers become request constraints the rule engine may only tighten.
* **Operations:** `route_net` (Route Review, candidates), `route_group` and
  `route_board` (Routing Jobs batch review), `optimize_net` / `reduce_vias`
  ("make it cleaner" → FEWER_BENDS; minimise vias → FEWER_VIAS; clearance →
  MORE_CLEARANCE; applied as one validated, undoable commit after approval).
* **Autonomy modes** (Settings ▸ Routing): ADVISORY (routing commands never run),
  APPROVAL REQUIRED (default), BATCH APPROVAL ("Approve & Run Plan" for the route
  commands of one answer, routed as one reviewable job). No fully autonomous mode.
* **Board fact tools** (`ai/fact_tools.py`): get_board_summary, get_net_info,
  get_component_info, get_rule_info, get_connectivity, get_congestion,
  get_route_failure, get_route_metrics. The model lists `fact_requests`; the user
  presses *Send Requested Facts*; answers go back as `FACT:` lines. Names are
  anonymised like the rest of the context.
* **Failure feedback:** every run records `ROUTER_RESULT:` facts (status, reason such
  as VIA_LIMIT with "a route exists with 2 via(s)", metrics, blocking statistics).
  *Ask AI to Revise* sends them back. The loop is bounded: 3 follow-up rounds per
  user prompt, each a user click; any new command needs approval again.
* **Provider independence:** all mapping lives in `route_bridge.py`; OpenAI,
  Anthropic and OpenAI-compatible adapters produce the same `AICommand` objects.
* The AI session follows the working board: after a commit or undo, open
  proposals made for the previous state expire; approved ones remain runnable.

Tests: `tests/unit/test_ai_planner.py`, `tests/integration/test_ai_routing_ui.py`
(offline mock provider; no API credits).
