"""Customer-service triage agent.

Pipeline for every inbound email:
    1. CLASSIFY  -> intent + confidence + sentiment
    2. RETRIEVE  -> the order record and policy text the answer must be grounded in
    3. DECIDE    -> auto-send / draft-for-review / escalate  (the trust boundary)
    4. DRAFT     -> a grounded reply (only if we're sending or drafting)

Design principle (matches Klaviyo's own stated value): the human stays in the loop.
The agent never *acts* on money or unmatched accounts on its own — it can only ever
draft those for a person. Confidence and risk decide who presses send, not the model.

Runs in two modes:
  - LLM mode  (ANTHROPIC_API_KEY set): real classification + drafting.
  - Fallback mode (no key): deterministic rules so the repo is runnable with zero setup
    and so the decision logic can be unit-tested without network flakiness.
"""
import json
import os
import re
from dataclasses import dataclass, asdict

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import guardrails
import tools

MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")
AUTOSEND_THRESHOLD = float(os.environ.get("AUTOSEND_THRESHOLD", "0.75"))

INTENTS = ["order_status", "return_refund", "product_question", "damaged_wrong", "other"]

# Intents that move money or change an account never auto-send. They can only be drafted
# for a human, no matter how confident the model is. This is a policy choice, not a model one.
HUMAN_REQUIRED_INTENTS = {"return_refund", "damaged_wrong", "other"}


@dataclass
class Triage:
    email_id: str
    intent: str
    confidence: float
    sentiment: str
    order_found: bool
    action: str          # AUTO_SEND | DRAFT_FOR_REVIEW | ESCALATE
    reason: str
    draft: str
    guardrail_flags: list  # injection flags and/or output-guard violations


# --------------------------------------------------------------------------- LLM helpers
def _client():
    try:
        import anthropic
    except ImportError:
        return None
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    return anthropic.Anthropic()


def _llm_classify(client, email):
    prompt = (
        "Classify this customer-service email. Respond with ONLY JSON: "
        '{"intent": one of ' + str(INTENTS) + ', '
        '"confidence": 0..1, "sentiment": "positive"|"neutral"|"negative"}.\n\n'
        f"Subject: {email['subject']}\nBody: {email['body']}"
    )
    msg = client.messages.create(
        model=MODEL, max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(re.search(r"\{.*\}", msg.content[0].text, re.S).group(0))


def _llm_draft(client, email, order, policies):
    context = f"ORDER RECORD:\n{json.dumps(order, indent=2) if order else 'None found'}\n\nPOLICIES:\n{policies}"
    prompt = (
        "You are a customer-service agent for Bright & Co. Write a short, warm reply.\n"
        "STRICT RULE: only state facts grounded in the ORDER RECORD or POLICIES below. "
        "Never invent tracking numbers, dates, or promises. If you cannot answer from the "
        "context, say a teammate will follow up.\n\n"
        f"{context}\n\nCUSTOMER EMAIL:\n{email['body']}\n\nReply:"
    )
    msg = client.messages.create(
        model=MODEL, max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


# ---------------------------------------------------------------- deterministic fallback
def _fallback_classify(email):
    """Keyword rules used when no API key is present. Good enough to demo the flow."""
    text = f"{email['subject']} {email['body']}".lower()
    neg = any(w in text for w in ["ridiculous", "angry", "terrible", "refund right now", "unacceptable"])
    sentiment = "negative" if neg else "neutral"

    def hit(words):
        return any(w in text for w in words)

    if hit(["refund", "return", "money back", "cancel"]):
        return {"intent": "return_refund", "confidence": 0.82, "sentiment": sentiment}
    if hit(["damaged", "broken", "wrong item", "defective"]):
        return {"intent": "damaged_wrong", "confidence": 0.8, "sentiment": sentiment}
    # Product questions are checked before order-status: an email can mention "arrive"
    # while really asking about the product (e.g. "before my tees arrive, do they shrink?").
    if hit(["shrink", "fit", "sizing", "material", "fabric", "pre-shrunk", "wash"]):
        return {"intent": "product_question", "confidence": 0.55, "sentiment": sentiment}
    if hit(["where is", "wismo", "arrive", "shipped", "tracking", "order status", "status"]):
        return {"intent": "order_status", "confidence": 0.88, "sentiment": sentiment}
    return {"intent": "other", "confidence": 0.4, "sentiment": sentiment}


def _fallback_draft(email, order, _policies):
    name = order["customer_name"].split()[0] if order else "there"
    if order and order["status"] == "shipped":
        return (f"Hi {name}, thanks for reaching out! Your order {order['order_id']} is on its "
                f"way via {order['carrier']} (tracking {order['tracking']}) and is expected by "
                f"{order['expected_delivery']}. Let us know if you need anything else!")
    if order and order["status"] == "processing":
        return (f"Hi {name}, thanks for checking in! Order {order['order_id']} is still being "
                f"prepared in our warehouse and should ship shortly — you'll get tracking by email. "
                f"Estimated delivery is {order['expected_delivery']}.")
    return f"Hi {name}, thanks for reaching out — a teammate will follow up shortly."


# --------------------------------------------------------------------------- the policy
def decide(intent, confidence, sentiment, order_found):
    """The trust boundary. Returns (action, reason)."""
    if not order_found and intent in {"order_status", "return_refund", "damaged_wrong"}:
        return "ESCALATE", "No matching order — needs a human to verify the customer."
    if confidence < 0.6:
        return "ESCALATE", f"Low classification confidence ({confidence:.2f})."
    if sentiment == "negative":
        return "ESCALATE", "Negative sentiment — route to a person to keep trust."
    if intent in HUMAN_REQUIRED_INTENTS:
        return "DRAFT_FOR_REVIEW", "Touches money/account — draft prepared, human presses send."
    if confidence >= AUTOSEND_THRESHOLD:
        return "AUTO_SEND", f"High-confidence, low-risk intent ({confidence:.2f})."
    return "DRAFT_FOR_REVIEW", f"Confidence {confidence:.2f} below auto-send threshold."


def triage_email(email) -> Triage:
    client = _client()

    # INPUT GUARD (layer 1): scan the customer's text before trusting the model with it.
    # A manipulation attempt always goes to a human and never auto-drafts.
    inj_flags = guardrails.scan_injection(f"{email.get('subject', '')} {email['body']}")
    if inj_flags:
        return Triage(email["id"], "other", 0.0, "neutral",
                      tools.find_order(email["from"], email["body"]) is not None,
                      "ESCALATE", f"Possible prompt-injection/manipulation: {', '.join(inj_flags)}",
                      "", inj_flags)

    cls = _llm_classify(client, email) if client else _fallback_classify(email)
    intent, confidence, sentiment = cls["intent"], float(cls["confidence"]), cls["sentiment"]

    order = tools.find_order(email["from"], email["body"])
    action, reason = decide(intent, confidence, sentiment, order is not None)

    draft, flags = "", []
    if action in ("AUTO_SEND", "DRAFT_FOR_REVIEW"):
        policies = tools.load_policies()
        draft = (_llm_draft(client, email, order, policies) if client
                 else _fallback_draft(email, order, policies))

        # OUTPUT GUARD (layer 3): never auto-send a reply that isn't grounded.
        if action == "AUTO_SEND":
            flags = guardrails.verify_output(draft, order, policies)
            if flags:
                action = "DRAFT_FOR_REVIEW"
                reason = f"Output guard blocked auto-send: {'; '.join(flags)}"

    return Triage(email["id"], intent, confidence, sentiment,
                  order is not None, action, reason, draft, flags)


def main():
    mode = "LLM" if _client() else "FALLBACK (no API key — deterministic rules)"
    print(f"Mode: {mode}\nAuto-send threshold: {AUTOSEND_THRESHOLD}\n" + "=" * 70)
    emails = json.loads((tools.DATA / "sample_emails.json").read_text())["emails"]
    counts = {}
    for email in emails:
        t = triage_email(email)
        counts[t.action] = counts.get(t.action, 0) + 1
        print(f"\n[{t.email_id}] {email['subject']!r} from {email['from']}")
        print(f"  intent={t.intent} conf={t.confidence:.2f} sentiment={t.sentiment} "
              f"order_found={t.order_found}")
        print(f"  -> {t.action}: {t.reason}")
        if t.guardrail_flags:
            print(f"  guardrail_flags: {t.guardrail_flags}")
        if t.draft:
            print(f"  draft: {t.draft}")
    print("\n" + "=" * 70)
    print("Routing summary:", json.dumps(counts))


if __name__ == "__main__":
    main()
