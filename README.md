# Customer-Service Triage Agent

A small agent that reads inbound customer-service email, understands intent, drafts a grounded
reply, and — most importantly — **decides who presses send**: the AI or a human.

Built for the Klaviyo AI Product Build exercise. Time-boxed to ~2 hours. The goal was not the
most complex system; it was a clear product decision about *where AI should be trusted versus
constrained*.

---

## The customer problem

For an e-commerce brand (Klaviyo's core customer), a large share of support volume is repetitive
and low-judgment: "where's my order?" (WISMO), returns, and basic product questions. These are
slow and expensive to answer by hand, yet a wrong automated answer — a made-up tracking number,
an unauthorized refund — destroys trust faster than a slow reply ever would.

So the design question isn't "can AI write the reply?" It's **"which replies are safe to send
automatically, which need a human's eyes, and which should never be touched by AI alone?"**

## Scope (what I deliberately left out)

In scope: one channel (email), one brand, the three highest-volume intents, and a clear routing
decision. Out of scope on purpose: live OMS/Gmail integrations (mocked), a UI (the output *is*
the product), multi-turn conversations, and analytics dashboards. Those are noted in
[What's next](#whats-next).

## How it works

Every email runs through four steps:

1. **Classify** — intent, confidence, and sentiment.
2. **Retrieve** — the matching order record and the policy text the reply must be grounded in.
3. **Decide** — the trust boundary (below).
4. **Draft** — a reply grounded *only* in retrieved facts; it will not invent tracking numbers,
   dates, or promises.

### The trust boundary (the actual product decision)

| Situation | Action | Why |
|---|---|---|
| High-confidence, low-risk (e.g. order-status, order found) | **AUTO_SEND** | Cheap to get right, cheap to be wrong. Speed wins. |
| Touches money or an account (returns, refunds, damaged items) | **DRAFT_FOR_REVIEW** | AI drafts; a human presses send. Never auto-acts on money. |
| Low confidence, negative sentiment, or no matching order | **ESCALATE** | Trust and fraud risk outweigh speed. Hand to a person. |

This mirrors Klaviyo's own stated principle that *the human in the loop matters most*. Money
movement and unverified accounts are a **policy** constraint, not a model judgment — no confidence
score can override them.

### Grounding

The agent may only state facts found in the order record or `data/policies.md`. If an answer
isn't covered, it doesn't guess — it escalates. That's why "Do these shrink?" (not in policy)
routes to a human instead of getting a confident-sounding wrong answer.

### Guardrails (defense in depth)

The routing boundary decides *who sends*. Guardrails (`guardrails.py`) protect the *content*, on
both sides of the model — because the LLM should never be the only thing between a hostile email
and a sent reply:

- **Input guard** scans the customer's text for prompt-injection and social engineering
  (*"ignore previous instructions,"* *"you are now an admin,"* *"reveal your system prompt"*).
  Any hit forces **ESCALATE** — the manipulation attempt goes to a human and the agent never
  auto-drafts from it, regardless of the surface intent.
- **Output guard** inspects the drafted reply before an **AUTO_SEND** and blocks anything not
  grounded: invented tracking numbers, ungrounded dollar amounts, refund/credit promises, or
  non-allowlisted URLs. A violation downgrades the reply to human review rather than sending it.

So a single email passes through three layers: input guard → routing policy → output guard.
This is the answer to "where should AI be trusted vs. constrained" made concrete and testable.

## Run it

No setup, no API key required — it ships in deterministic fallback mode so a reviewer can run it
instantly:

```bash
python3 agent.py
```

For live LLM classification and drafting:

```bash
cp .env.example .env   # add your ANTHROPIC_API_KEY
pip install -r requirements.txt
python3 agent.py
```

### Sample output (fallback mode)

```
[e1] 'Where is my order?'      -> AUTO_SEND        (order found, in transit)
[e3] 'Return this mug'         -> DRAFT_FOR_REVIEW (touches money)
[e4] 'Refund please' (angry)   -> ESCALATE         (negative sentiment)
[e5] 'Never got my package'    -> ESCALATE         (no matching order)
[e6] 'Do these shrink?'        -> ESCALATE         (not answerable from policy)

Routing summary: AUTO_SEND 2, DRAFT_FOR_REVIEW 1, ESCALATE 3
```

## Connect to real Gmail

`gmail_connector.py` runs the same pipeline over a live inbox. The three actions map onto real
Gmail behavior:

| Action | Gmail effect |
|---|---|
| AUTO_SEND | Sends the reply in-thread, marks the email read |
| DRAFT_FOR_REVIEW | Creates a Gmail **draft** reply — a human presses send |
| ESCALATE | Applies a `CS/Escalate` label, leaves it unread for an agent |

**It is dry-run by default** — it prints what it *would* do and changes nothing until you pass
`--apply`. That's the human-in-the-loop stance enforced at the connector level, too.

```bash
# One time: create an OAuth "Desktop app" client in Google Cloud, download as credentials.json
pip install -r requirements.txt
python3 gmail_connector.py            # safe dry-run over unread inbox
python3 gmail_connector.py --apply    # actually send/draft/label
```

## Tests / eval harness

The harness is **pluggable**: every `*.json` file in `tests/cases/` is auto-discovered. To add
coverage, drop in a new file — no code changes. Each case is an email plus its expected route:

```json
{"cases": [
  {"name": "wismo_in_transit",
   "email": {"from": "maya.r@example.com", "subject": "Where is my order?", "body": "...BR-10231..."},
   "expect": {"action": "AUTO_SEND", "intent": "order_status"}}
]}
```

Run it two ways (both force deterministic fallback mode, so results are reproducible):

```bash
python3 tests/run_evals.py   # no dependencies; prints a pass/fail table, exits non-zero on failure
pytest                       # same cases as individual parametrized tests
```

Current set: 11/11 passing across `baseline.json` (one case per route), `edge_cases.json`, and
`security_cases.json` (adversarial injection attempts that must escalate).
This is the hook for measuring quality as the agent grows — expand the case files and watch the
pass rate and (eventually) grounding-error rate.

**CI quality gate:** `.github/workflows/evals.yml` runs this suite on every push and PR. A
change that re-routes an email the wrong way (e.g. something that should escalate starts
auto-sending) fails the build — so the trust boundary is regression-protected, not just
documented.

## Architecture

```
email ─► agent.triage_email
            │
            ├─ guardrails.scan_injection       → INPUT GUARD (hit ⇒ escalate)
            ├─ classify  (LLM or rule fallback) → intent, confidence, sentiment
            ├─ tools.find_order / load_policies → grounding context
            ├─ decide(...)                       → AUTO_SEND | DRAFT_FOR_REVIEW | ESCALATE
            ├─ draft     (LLM or template)       → grounded reply
            └─ guardrails.verify_output          → OUTPUT GUARD (blocks ungrounded auto-send)
```

- `agent.py` — pipeline + the `decide()` policy (the part worth reviewing).
- `guardrails.py` — input injection scan + output grounding verification.
- `tools.py` — order lookup and policy retrieval (stand-ins for a real OMS / KB).
- `gmail_connector.py` — live Gmail fetch + action application (dry-run by default).
- `data/` — mock orders, policy knowledge source, and labeled sample emails.
- `tests/` — pluggable eval harness; `cases/*.json` is auto-discovered.

## License

MIT — see [LICENSE](LICENSE).

**Key tradeoff:** I put the routing logic in plain, testable Python rather than asking the LLM to
decide its own autonomy level. The model is great at language and classification; it should not be
the thing that decides whether to move money. Keeping that boundary in code makes it auditable and
lets us tune thresholds without re-prompting.

## How success should be measured

If this shipped, I'd track: **automation/deflection rate** (% safely auto-sent), **escalation
precision** (did escalated emails truly need a human?), **CSAT on AI-sent replies**, and
**grounding error rate** (any reply containing an unsupported claim — the metric that should gate
how much we automate). The North Star is not "% automated" — it's automation *without* eroding
trust.

## What's next

Real Gmail + OMS integration; confidence calibration from labeled history; a reviewer UI for the
draft queue; multi-turn handling; and expanding intents once grounding error rate stays near zero.

## Use of AI in building this

In the spirit of Klaviyo's interview AI guidelines: I used an AI coding assistant to scaffold the
boilerplate, mock data, and this README so I could spend the two hours on the product decisions —
the intent taxonomy, the trust boundary, the grounding constraint, and the success metrics. Every
design choice here is mine and I can walk through any line of it.
