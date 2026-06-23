"""Gmail connector — fetch real unread email, triage it, and apply the decision.

Maps the agent's three actions onto real Gmail behavior:
    AUTO_SEND        -> send a reply in the thread
    DRAFT_FOR_REVIEW -> create a Gmail *draft* reply (a human presses send)
    ESCALATE         -> apply a "CS/Escalate" label and leave it unread for an agent

SAFETY: runs in --dry-run by default. It will not send or create anything until you pass
--apply. This mirrors the product's human-in-the-loop stance: the system never acts on its
own without an explicit go-ahead.

Setup (one time):
    pip install -r requirements.txt
    # Create an OAuth "Desktop app" client in Google Cloud, download as credentials.json.
    # First run opens a browser to authorize; a token.json is cached afterward.

Scopes: gmail.modify covers read, label, draft, and send.
"""
import argparse
import base64
import os
from email.mime.text import MIMEText

import agent

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
ESCALATE_LABEL = "CS/Escalate"


# --------------------------------------------------------------------------- auth + client
def get_service():
    """Returns an authorized Gmail API client. Imports are local so the rest of the repo
    (and the tests) run without google libraries installed."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


# ------------------------------------------------------------------------------- read side
def _header(payload, name):
    for h in payload.get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _extract_body(payload) -> str:
    """Walk the MIME tree and return the first text/plain part (falls back to snippet)."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        text = _extract_body(part)
        if text:
            return text
    return ""


def fetch_unread(service, max_results=10):
    """Return unread inbox messages in the agent's email shape."""
    resp = service.users().messages().list(
        userId="me", q="is:unread in:inbox", maxResults=max_results
    ).execute()
    emails = []
    for ref in resp.get("messages", []):
        msg = service.users().messages().get(userId="me", id=ref["id"], format="full").execute()
        payload = msg["payload"]
        sender = _header(payload, "From")
        # "Name <addr@x.com>" -> "addr@x.com"
        if "<" in sender:
            sender = sender.split("<")[1].rstrip(">").strip()
        emails.append({
            "id": msg["id"],
            "thread_id": msg["threadId"],
            "from": sender,
            "subject": _header(payload, "Subject"),
            "body": _extract_body(payload) or msg.get("snippet", ""),
        })
    return emails


# ------------------------------------------------------------------------------ write side
def _raw_reply(to, subject, body, thread_headers):
    mime = MIMEText(body)
    mime["To"] = to
    mime["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    for k, v in thread_headers.items():
        if v:
            mime[k] = v
    return {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode()}


def _ensure_label(service):
    existing = service.users().labels().list(userId="me").execute().get("labels", [])
    for lab in existing:
        if lab["name"] == ESCALATE_LABEL:
            return lab["id"]
    created = service.users().labels().create(
        userId="me", body={"name": ESCALATE_LABEL}
    ).execute()
    return created["id"]


def apply_action(service, email, triage, apply: bool):
    """Carry out the triage decision. No-op (prints only) unless apply=True."""
    tag = "APPLY" if apply else "DRY-RUN"
    if triage.action == "ESCALATE":
        print(f"  [{tag}] label '{ESCALATE_LABEL}', leave unread")
        if apply:
            label_id = _ensure_label(service)
            service.users().messages().modify(
                userId="me", id=email["id"], body={"addLabelIds": [label_id]}
            ).execute()
        return

    reply = _raw_reply(email["from"], email["subject"], triage.draft, {})
    reply["threadId"] = email["thread_id"]

    if triage.action == "DRAFT_FOR_REVIEW":
        print(f"  [{tag}] create Gmail DRAFT for human review")
        if apply:
            service.users().drafts().create(userId="me", body={"message": reply}).execute()
    elif triage.action == "AUTO_SEND":
        print(f"  [{tag}] SEND reply, mark read")
        if apply:
            service.users().messages().send(userId="me", body=reply).execute()
            service.users().messages().modify(
                userId="me", id=email["id"], body={"removeLabelIds": ["UNREAD"]}
            ).execute()


# ------------------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="Triage unread Gmail with the CS agent.")
    ap.add_argument("--apply", action="store_true",
                    help="Actually send/draft/label. Omit for a safe dry-run.")
    ap.add_argument("--max", type=int, default=10)
    args = ap.parse_args()

    service = get_service()
    emails = fetch_unread(service, args.max)
    print(f"Fetched {len(emails)} unread email(s). Mode: {'APPLY' if args.apply else 'DRY-RUN'}\n")
    for email in emails:
        t = agent.triage_email(email)
        print(f"[{email['id']}] {email['subject']!r} from {email['from']}")
        print(f"  intent={t.intent} conf={t.confidence:.2f} -> {t.action}: {t.reason}")
        apply_action(service, email, t, args.apply)
        print()


if __name__ == "__main__":
    main()
