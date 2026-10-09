import os
import json
import re
import time
from datetime import datetime, timedelta, timezone
import html as html_lib
import requests
import anthropic
try:
    import emoji as emoji_lib
except ImportError:  # pragma: no cover
    emoji_lib = None
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# --- Config ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ATTIO_API_KEY = os.getenv("ATTIO_API_KEY")        # leave unset to disable the Attio sync
ATTIO_OWNER_ID = os.getenv("ATTIO_OWNER_ID", "5df13879-f0c5-4967-8982-23a2ca25b8de")  # default: devon@hitch-advisors.com
ATTIO_DEAL_STAGE = os.getenv("ATTIO_DEAL_STAGE", "Replied")
ATTIO_DEAL_SOURCE = os.getenv("ATTIO_DEAL_SOURCE", "SmartLead")
# campaign-name keyword (lowercase substring) -> Attio Mandate record id. Override with env MANDATE_MAP (JSON).
_DEFAULT_MANDATE_MAP = {
    "carda": "189f99ad-e497-4ab5-a300-18c586cfb7f7",        # Carda Alliance
    "pella/marvin": "189f99ad-e497-4ab5-a300-18c586cfb7f7", # Carda Alliance
    "fenc": "64527d31-d1fa-4d3b-bf60-526ef9c63e04",         # Unified Fencing Group
    "ufg": "64527d31-d1fa-4d3b-bf60-526ef9c63e04",          # Unified Fencing Group (campaign "UFG GROUP")
    "unified": "64527d31-d1fa-4d3b-bf60-526ef9c63e04",      # Unified Fencing Group
}
try:
    MANDATE_MAP = {k.lower(): v for k, v in json.loads(os.getenv("MANDATE_MAP", "") or "{}").items()} or _DEFAULT_MANDATE_MAP
except Exception:
    MANDATE_MAP = _DEFAULT_MANDATE_MAP
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SMARTLEAD_API_KEY = os.getenv("SMARTLEAD_API_KEY")
# Optional: if set, incoming webhooks must carry a matching "secret_key" field.
SMARTLEAD_WEBHOOK_SECRET = os.getenv("SMARTLEAD_WEBHOOK_SECRET", "")
SMARTLEAD_BASE = "https://server.smartlead.ai/api/v1"
# Any email address whose domain contains one of these keywords is one of OUR mailboxes
# (hitch-guide.com, hitch-advisors.com, hitch-ventures.com, ...). Comma-separated.
OWN_DOMAIN_KEYWORDS = [k.strip().lower() for k in os.getenv("OWN_DOMAIN_KEYWORDS", "hitch").split(",") if k.strip()]
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL")
# ---- Calendly (disabled for now; no booking links go out until Hitch's link is provided) ----
# CALENDLY_API_KEY = os.getenv("CALENDLY_API_KEY")
# # Event type URIs from Calendly (found via GET /event_types)
# CALENDLY_O2E_EVENT_TYPE = os.getenv(
#     "CALENDLY_O2E_EVENT_TYPE",
#     "https://api.calendly.com/event_types/fcf75643-7fb6-4072-b1e0-5dba5ce49c1d",
# )
# CALENDLY_STATE17_EVENT_TYPE = os.getenv("CALENDLY_STATE17_EVENT_TYPE", "")
# # Fallback booking page URLs (used when API fails or event type not configured)
# CALENDLY_O2E_URL = os.getenv("CALENDLY_O2E_URL", "https://calendly.com/gdavidson-options2exit/introcall")
# CALENDLY_STATE17_URL = os.getenv("CALENDLY_STATE17_URL", "https://calendly.com/team-state17/30min")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Track sent replies to prevent Slack event retries from sending duplicates
_sent_replies = set()  # set of "<stats_id>:<suffix>" keys that have already been sent


# ============================================================
# HELPERS
# ============================================================

def clean_slack_email(email: str) -> str:
    """Strip Slack's auto-linking from email addresses.

    Slack auto-formats domains and emails into markup like:
      <http://claygeni.us|claygeni.us>
      <mailto:john@claygeni.us|john@claygeni.us>

    This function reverses all known Slack mangling patterns.
    """
    if not email:
        return ""
    # Reverse the [at] workaround first
    email = email.replace("[at]", "@")
    # Handle <http://domain.com|domain.com> and <https://...> format
    email = re.sub(r'<https?://([^|>]+)\|[^>]+>', r'\1', email)
    email = re.sub(r'<https?://([^>]+)>', r'\1', email)
    # Handle <mailto:email|email> format
    email = re.sub(r'<mailto:([^|>]+)\|[^>]+>', r'\1', email)
    return email


def extract_lead_response(reply_text: str, reply_snippet: str, campaign_name: str) -> str:
    """Use Claude to extract the lead's most recent complete response from an email thread."""
    if not reply_text:
        return reply_snippet

    prompt = f"""You are an email thread parser. You will receive a full email thread in HTML format.

This thread is from an outbound sales campaign sent by Hitch. Our sending mailboxes are on domains containing "hitch" (for example hitch-guide.com, hitch-team.com, hitch-counsel.com) and any email address associated with Hitch.

The campaign name is: {campaign_name}

Your job: Extract ONLY the lead's most recent response. The lead is the person who is NOT from our brands. Their reply is the newest message in the thread that was NOT sent by us.

Rules:
- Strip all HTML tags and return clean plain text only
- Do NOT include any quoted replies, forwarded content, or prior messages
- Do NOT include any "On [date] [person] wrote:" lines
- Do NOT include our original outbound email or any part of it
- Do NOT include email signatures from our team
- If the lead's response includes their own signature (name, title, phone), keep it
- Return ONLY the lead's message text, nothing else
- No labels, no headers, no explanations. Just the raw message content.

Here is the full email thread:

{reply_text}"""

    try:
        msg = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        extracted = msg.content[0].text.strip()
        if extracted and len(extracted) > 2:
            print(f"[extract] Successfully extracted lead response: {len(extracted)} chars")
            return extracted
        print(f"[extract] Extraction returned empty/short result, falling back to snippet")
        return reply_snippet
    except Exception as e:
        print(f"[extract] Failed to extract lead response: {e}. Falling back to snippet.")
        return reply_snippet


_AUTO_REPLY_RE = re.compile(
    r"out of (the )?office|out-of-office|\bOOO\b|auto(matic|mated)?[- ]?(reply|response)|away from (the |my )?(office|desk)"
    r"|on (vacation|holiday|leave|PTO)|limited access to (my )?e-?mail|(will|I'll|I will) be (out|away|back)|I am (currently )?(out|away)"
    r"|I'?m (currently )?(out|away) (of|from|until|through|on)|will (be )?return(ing)? on|back in the office|delivery (has )?failed|undeliverable",
    re.I)
_INTEREST_RE = re.compile(r"\b(interested|let'?s (talk|chat)|call me|give me a call|sounds good|happy to (talk|chat)|tell me more|set up a (call|time))\b", re.I)


def is_auto_reply(text: str) -> bool:
    """Out-of-office / auto-reply / bounce with no sign of real interest."""
    t = text or ""
    return bool(_AUTO_REPLY_RE.search(t)) and not _INTEREST_RE.search(t)


def classify_sentiment(lead_response: str, campaign_name: str = "") -> str:
    """Use Claude to label the lead's reply as Positive, Negative, or Neutral."""
    if not lead_response or not lead_response.strip():
        return "Neutral"
    if is_auto_reply(lead_response):
        print("[sentiment] Neutral (auto-reply / out-of-office)")
        return "Neutral"

    prompt = f"""You are classifying a reply to a cold outreach email sent by Hitch.

Campaign: {campaign_name}

Classify the lead's reply into exactly one label:
- Positive: interested, wants to talk, asks for more info, proposes a time, shares a phone number, open to a conversation
- Negative: not interested, asks to stop/unsubscribe, hostile, already sold, "remove me", wrong person and no referral
- Neutral: out-of-office, auto-reply, bounce notice, asks a clarifying question with no clear intent, forwards to someone else
A phone number or alternate contact inside an out-of-office or auto-reply is NOT interest. Label those Neutral.

Reply with ONLY one word: Positive, Negative, or Neutral.

Lead's reply:
{lead_response}"""

    try:
        msg = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": prompt}],
        )
        label = msg.content[0].text.strip().split()[0].strip(".,").capitalize()
        if label in ("Positive", "Negative", "Neutral"):
            print(f"[sentiment] {label}")
            return label
        print(f"[sentiment] Unexpected label {label!r}, defaulting to Neutral")
        return "Neutral"
    except Exception as e:
        print(f"[sentiment] Failed: {e}. Defaulting to Neutral.")
        return "Neutral"


BLOCKED_DRAFT_TERMS = ("calendly", "state17", "state 17", "options2exit", "o2e", "http://", "https://", "www.")


def scrub_draft(draft: str) -> str:
    """Remove any line that carries a link or an old-brand reference. Safety net only."""
    if not draft or draft.strip().upper().startswith("NO RESPONSE"):
        return draft
    kept, dropped = [], []
    for line in draft.splitlines():
        if any(t in line.lower() for t in BLOCKED_DRAFT_TERMS):
            dropped.append(line)
        else:
            kept.append(line)
    if dropped:
        print(f"[scrub] Removed {len(dropped)} line(s) from draft: {dropped}")
    cleaned = "\n".join(kept).strip()
    return cleaned or "NO RESPONSE"


def draft_reply(sender_name: str, eaccount: str, lead_email: str,
                campaign_name: str, reply_text: str) -> str:
    """Use Claude to draft an email reply."""
    prompt = """
# SYSTEM PROMPT: Email Reply Engine for Hitch

---

## YOUR ROLE

You are a human sales associate responding to inbound email replies on behalf of **Hitch**. You are NOT an AI assistant. You are a real person having a real email conversation. Every reply you write must sound like it came from a busy, friendly, competent person -- not from a language model.

You will receive:
1. The **campaign name**
2. The **original outbound email** that was sent (with the sender's signature)
3. The **prospect's reply**

Your job: Read the reply, classify it, and either draft a response or take no action.

---

## WHAT YOU MAY SAY ABOUT HITCH

You have NOT been given any facts about Hitch's history, team, portfolio, fees, deal sizes, or track record. Therefore:
- Do NOT invent or imply any company facts, numbers, past deals, or credentials
- Do NOT contradict anything said in the original outbound email; you may restate what it already said
- If the prospect asks a question you cannot answer from the outbound email, say you are happy to cover it on a quick call
- Keep every reply focused on one thing: setting up a short call

---

## SENDER IDENTITY

You sign every email as the person whose name appears in the signature of the original outbound email. Pull the name directly from the outbound email signature block. Match their sign-off style.

- If the outbound was signed "Best, Sarah Miller / Hitch" then you ARE Sarah Miller
- If it was signed just "Sarah" then sign as "Sarah"

Match the formality of the original signature. If they used just a first name, use just a first name. If they used full name and title, do the same.

---

## SCHEDULING (NO LINKS)

There is NO calendar or booking link available right now. Never include any URL in your reply. Never mention Calendly or any scheduling page.

To set up a call, ask for their availability instead. Keep it casual:
- "What does your calendar look like this week or next? Happy to work around you."
- "Do you have 15 minutes later this week? Let me know a couple of times that work."
- If they proposed a time: confirm it and stop.
- If they shared a phone number: confirm you will call and ask which day suits them.

---

## REPLY CLASSIFICATION

Read every inbound reply and classify it into ONE of the following categories. Then follow the corresponding action.

### CATEGORY 1: INTERESTED / READY TO TALK
**Signals:** "sure," "I am interested," "let's talk," "sounds good," "I am free," "yes," "tell me more," "what is the best way to connect," "when works," "let's schedule a call," "tomorrow works," "call me at [number]," prospect proposes a meeting time, prospect shares their phone number

**Action:** Respond. Keep it short. Confirm a time or ask for their availability. Do not re-pitch. They already said yes.

**Response examples:**
- If they say "Sure": "Great. What does your week look like? Happy to grab 15 minutes whenever suits you."
- If they propose a time: "That works. I will give you a call then. Looking forward to it."
- If they share a phone number: "Got it. I will call you at [number]. Does [tomorrow/today] work or is there a better day?"
- If they say "tell me more": Give 1-2 sentences restating what the outbound email said (no new claims), then steer to the call. "Happy to walk you through it. Easiest thing would be a quick 15 minute call. When are you free this week?"

### CATEGORY 2: NOT INTERESTED / STOP
**Signals:** "not interested," "remove me," "unsubscribe," "stop emailing," "already sold," hostile tone, "no thanks"

**Action:** NO RESPONSE. Never argue or try to re-engage.

### CATEGORY 3: AUTO-REPLY / OUT OF OFFICE / BOUNCE / UNCLEAR

**Action:** NO RESPONSE.

## OUTPUT FORMAT

Output ONLY the email reply text -- nothing else. No headers, no labels, no metadata. The output should be ready to send as-is. Include the greeting, body, sign-off, and signature.

Example of correct output:

Hi Joe,

Happy to send over more info. Easiest thing would be a quick 15 minute call. What does your calendar look like this week?

Best,
Sarah

Do NOT include lines like "CAMPAIGN:", "CLASSIFICATION:", "SIGN AS:", "RESPONSE:", or any other labels. Just the email copy.

If the reply should NOT receive a response, output only: NO RESPONSE

---

## FINAL REMINDERS

1. You are a person, not an AI. Write like one.
2. Short is almost always better. When in doubt, write less.
3. The goal is to set up a call. That is it.
4. Never argue with a prospect who says no.
5. Never invent facts about Hitch. Never include any link.
6. Match the prospect's energy. Mirror their tone.
7. If something feels off or you are unsure, output NO RESPONSE rather than guessing.
8. Read the original outbound email carefully. Do not contradict anything that was said in it.
"""

    full_prompt = (
        prompt
        + "\n\nSign off with this name exactly: "
        + sender_name
        + "\nCampaign: "
        + campaign_name
        + "\nFull email thread: "
        + reply_text
    )

    msg = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": full_prompt}],
    )
    return msg.content[0].text.strip()


# ---- Calendly routing (disabled for now) ----
# def get_calendly_info(email_account: str, campaign_name: str = "") -> dict:
#     """Return the correct Calendly event type URI and fallback URL based on domain."""
#     combined = (email_account + " " + campaign_name).lower()
#     if any(kw in combined for kw in ("options2exit", "o2e")):
#         return {"event_type": CALENDLY_O2E_EVENT_TYPE, "fallback_url": CALENDLY_O2E_URL}
#     if any(kw in combined for kw in ("state17", "findstate17", "state 17")):
#         if CALENDLY_STATE17_EVENT_TYPE:
#             return {"event_type": CALENDLY_STATE17_EVENT_TYPE, "fallback_url": CALENDLY_STATE17_URL}
#         return {"event_type": CALENDLY_O2E_EVENT_TYPE, "fallback_url": CALENDLY_O2E_URL}
#     return {"event_type": CALENDLY_O2E_EVENT_TYPE, "fallback_url": CALENDLY_O2E_URL}


# ---- Calendly available-slots (disabled for now) ----
# def fetch_available_slots(event_type_uri: str, num_days: int = 3) -> dict:
#     """Fetch available time slots from Calendly, grouped by day."""
#     if not event_type_uri or not CALENDLY_API_KEY:
#         return {}
#
#     now = datetime.now(timezone.utc)
#     start = (now + timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
#     end = (now + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
#
#     try:
#         resp = requests.get(
#             "https://api.calendly.com/event_type_available_times",
#             headers={"Authorization": f"Bearer {CALENDLY_API_KEY}"},
#             params={
#                 "event_type": event_type_uri,
#                 "start_time": start,
#                 "end_time": end,
#             },
#         )
#         resp.raise_for_status()
#         all_slots = resp.json().get("collection", [])
#     except Exception as e:
#         print(f"[calendly] Failed to fetch available times: {e}")
#         return {}
#
#     if not all_slots:
#         return {}
#
#     days = {}
#     for slot in all_slots:
#         if slot.get("status") != "available":
#             continue
#         dt = datetime.fromisoformat(slot["start_time"].replace("Z", "+00:00"))
#         et_dt = dt - timedelta(hours=4)
#         day_label = f"{et_dt.strftime('%A')}, {et_dt.strftime('%B')} {et_dt.day}"
#         time_label = et_dt.strftime("%I:%M %p").lstrip("0").lower()
#         if day_label not in days:
#             days[day_label] = []
#         days[day_label].append({
#             "time": time_label,
#             "url": slot["scheduling_url"],
#         })
#
#     sorted_days = dict(list(days.items())[:num_days])
#     total = sum(len(v) for v in sorted_days.values())
#     print(f"[calendly] Fetched {total} slots across {len(sorted_days)} days from {len(all_slots)} total available")
#     return sorted_days
#
#
# def format_slots_for_email(slots_by_day: dict, fallback_url: str) -> str:
#     """Format grouped slots as a text block for email drafts."""
#     if not slots_by_day:
#         return f"Book a time that works for you here: {fallback_url}"
#
#     lines = []
#     for day_label, times in slots_by_day.items():
#         lines.append(f"{day_label}")
#         time_strs = [f"{t['time']} - {t['url']}" for t in times]
#         lines.append("  " + "  |  ".join(time_strs))
#         lines.append("")
#     lines.append(f"Don't see a time that works? Pick any open slot here: {fallback_url}")
#     return "\n".join(lines)
#
#
# def format_slots_for_slack(slots_by_day: dict, fallback_url: str) -> str:
#     """Format grouped slots as a Slack mrkdwn block."""
#     if not slots_by_day:
#         return f"<{fallback_url}|Book a time>"
#
#     lines = []
#     for day_label, times in slots_by_day.items():
#         lines.append(f"*{day_label}*")
#         time_strs = [f"<{t['url']}|{t['time']}>" for t in times]
#         lines.append("  " + "  |  ".join(time_strs))
#     lines.append(f"\n<{fallback_url}|See all available times>")
#     return "\n".join(lines)


def extract_sender_name(email_account: str) -> str:
    """Extract first name from email, e.g. john.tanner@x.com -> John"""
    local = email_account.split("@")[0]
    first = local.split(".")[0]
    return first.capitalize()


# ---- Attio ----

ATTIO_API = "https://api.attio.com/v2"
REPLY_STATUS_SLUG = "reply_status"
REPLY_STATUS_OPTIONS = ("Positive", "Negative", "Neutral")
_reply_status_ready = {"ok": False}   # cached after the attribute is confirmed/created


def _attio_headers() -> dict:
    return {"Authorization": f"Bearer {ATTIO_API_KEY}", "Content-Type": "application/json"}


def _attio(method: str, path: str, **kw):
    kw.setdefault("timeout", 20)
    resp = requests.request(method, f"{ATTIO_API}{path}", headers=_attio_headers(), **kw)
    resp.raise_for_status()
    return resp.json() if resp.text else {}


def _record_id(record: dict):
    return (record or {}).get("data", {}).get("id", {}).get("record_id")


FIRST_MAILBOX_SLUG = "first_outreach_mailbox"
ATTIO_OBJECTS = ("people", "companies", "deals")
# (slug, title, type, select options)
FIRST_SENT_SLUG = "first_email_sent_date"
ATTIO_CUSTOM_ATTRS = (
    (REPLY_STATUS_SLUG, "Reply status", "select", REPLY_STATUS_OPTIONS),
    (FIRST_MAILBOX_SLUG, "First email sent from", "text", None),
    (FIRST_SENT_SLUG, "First email sent on", "date", None),
    # deals already have Source (select); people/companies get one too so the channel shows on every record
    ("source", "Source", "select", ("SmartLead",)),
)
REPLY_STATUS_OBJECTS = ATTIO_OBJECTS  # kept for /attio/check


def ensure_reply_status_attribute() -> dict:
    """
    Make sure People, Companies and Deals all have:
      - 'Reply status' (select: Positive / Negative / Neutral)
      - 'First email sent from' (text: the mailbox that sent the first outreach email)
    Idempotent; result is cached for the life of the process.
    """
    if _reply_status_ready["ok"]:
        return {"ok": True, "created": []}
    created = []
    for obj in ATTIO_OBJECTS:
        attrs = _attio("GET", f"/objects/{obj}/attributes").get("data", [])
        have = {a.get("api_slug") for a in attrs}
        for slug, title, typ, options in ATTIO_CUSTOM_ATTRS:
            if slug not in have:
                _attio("POST", f"/objects/{obj}/attributes", json={"data": {
                    "title": title,
                    "description": "Set automatically by the Smartlead reply bot",
                    "api_slug": slug,
                    "type": typ,
                    "is_required": False,
                    "is_unique": False,
                    "is_multiselect": False,
                    "config": {},
                }})
                created.append(f"{obj}.{slug}")
            if options:
                existing = {o.get("title") for o in
                            _attio("GET", f"/objects/{obj}/attributes/{slug}/options").get("data", [])}
                for t in options:
                    if t not in existing:
                        _attio("POST", f"/objects/{obj}/attributes/{slug}/options", json={"data": {"title": t}})
    _reply_status_ready["ok"] = True
    return {"ok": True, "created": created}


def _custom_values(sentiment: str = "", first_mailbox: str = "", first_sent: str = "") -> dict:
    values = {}
    if sentiment in REPLY_STATUS_OPTIONS:
        values[REPLY_STATUS_SLUG] = [{"option": sentiment}]
    if first_mailbox:
        values[FIRST_MAILBOX_SLUG] = [{"value": first_mailbox}]
    if first_sent:
        values[FIRST_SENT_SLUG] = [{"value": first_sent}]
    return values


def upsert_attio_company(domain: str, sentiment: str = "", first_mailbox: str = "", first_sent: str = "") -> dict:
    values = {"domains": [{"domain": domain}], **_custom_values(sentiment, first_mailbox, first_sent)}
    return _attio("PUT", "/objects/companies/records", params={"matching_attribute": "domains"},
                  json={"data": {"values": values}})


def ensure_source(obj: str, record_id: str, source: str = "SmartLead") -> bool:
    """Set Source on a person/company only when it is empty (never overwrite Referral, Inbound, ...)."""
    if not record_id or not source:
        return False
    try:
        vals = _attio("GET", f"/objects/{obj}/records/{record_id}").get("data", {}).get("values", {})
        if vals.get("source"):
            return False
        _attio("PATCH", f"/objects/{obj}/records/{record_id}",
               json={"data": {"values": {"source": [{"option": source}]}}})
        return True
    except Exception as e:
        print(f"[attio] Could not set source on {obj} {record_id}: {e}")
        return False


_campaign_cache = {}


def campaign_record_for(campaign_id, campaign_name: str = "", mandate: dict = None) -> str:
    """
    Attio Campaign record id for a Smartlead campaign: match on Smartlead Campaign ID,
    then on Campaign Name; create a minimal record (name, Smartlead ID, mandate) if none exists.
    """
    cid = str(campaign_id or "").strip()
    key = cid or (campaign_name or "").strip().lower()
    if not key:
        return ""
    if key in _campaign_cache:
        return _campaign_cache[key]
    rid = ""
    try:
        if cid:
            rows = _attio("POST", "/objects/campaigns/records/query",
                          json={"filter": {"smartlead_campaign_id": cid}, "limit": 1}).get("data", [])
            rid = rows[0]["id"]["record_id"] if rows else ""
        if not rid and campaign_name:
            rows = _attio("POST", "/objects/campaigns/records/query",
                          json={"filter": {"campaign_name": campaign_name}, "limit": 1}).get("data", [])
            rid = rows[0]["id"]["record_id"] if rows else ""
            if rid and cid:
                _attio("PATCH", f"/objects/campaigns/records/{rid}",
                       json={"data": {"values": {"smartlead_campaign_id": [{"value": cid}]}}})
        if not rid and campaign_name:
            values = {"campaign_name": [{"value": campaign_name}]}
            if cid:
                values["smartlead_campaign_id"] = [{"value": cid}]
            if mandate and mandate.get("id"):
                values["mandate"] = [{"target_object": "mandates", "target_record_id": mandate["id"]}]
            rid = _record_id(_attio("POST", "/objects/campaigns/records", json={"data": {"values": values}})) or ""
            print(f"[attio] Created campaign record {rid} for {campaign_name!r} ({cid})")
    except Exception as e:
        print(f"[attio] Campaign lookup failed for {campaign_name!r} ({cid}): {e}")
        return ""
    if rid:
        _campaign_cache[key] = rid
    return rid


def first_sent_date(messages: list) -> str:
    """YYYY-MM-DD of the first email we sent in the thread, or ''."""
    for m in messages or []:
        if m.get("dir") == "outbound" and m.get("time"):
            iso = _iso(m["time"])
            return iso[:10] if iso else ""
    return ""


def upsert_attio_person(lead_email: str, sentiment: str = "", first_mailbox: str = "", first_sent: str = "") -> dict:
    values = {"email_addresses": [{"email_address": lead_email}], **_custom_values(sentiment, first_mailbox, first_sent)}
    return _attio("PUT", "/objects/people/records", params={"matching_attribute": "email_addresses"},
                  json={"data": {"values": values}})


_mandate_cache = {}


def mandate_for_campaign(campaign_name: str) -> dict:
    """Map a campaign name to {"id", "name", "buyer_id"} or {} if unmapped."""
    cname = (campaign_name or "").lower()
    mid = next((v for k, v in MANDATE_MAP.items() if k in cname), None)
    if not mid:
        return {}
    if mid in _mandate_cache:
        return _mandate_cache[mid]
    info = {"id": mid, "name": "", "buyer_id": None}
    try:
        vals = _attio("GET", f"/objects/mandates/records/{mid}").get("data", {}).get("values", {})
        names = vals.get("mandate_name") or vals.get("name") or []
        info["name"] = (names[0].get("value") if names else "") or ""
        buyers = vals.get("buyer") or []
        info["buyer_id"] = buyers[0].get("target_record_id") if buyers else None
    except Exception as e:
        print(f"[attio] Mandate {mid} lookup failed: {e}")
    _mandate_cache[mid] = info
    return info


def first_reply_date(messages: list) -> str:
    """YYYY-MM-DD of the lead's first inbound message, or ''."""
    for m in messages or []:
        if m.get("dir") == "inbound" and m.get("time"):
            iso = _iso(m["time"])
            return iso[:10] if iso else ""
    return ""


def ensure_company_hitch_role(company_id: str, role: str = "Seller") -> bool:
    """Add `role` to the company's multi-select hitch_role without dropping others."""
    if not company_id:
        return False
    try:
        vals = _attio("GET", f"/objects/companies/records/{company_id}").get("data", {}).get("values", {})
        have = {(v.get("option") or {}).get("title") for v in vals.get("hitch_role", [])}
        if role in have:
            return False
        _attio("PATCH", f"/objects/companies/records/{company_id}",
               json={"data": {"values": {"hitch_role": [{"option": role}]}}})
        return True
    except Exception as e:
        print(f"[attio] hitch_role update failed for {company_id}: {e}")
        return False


def ensure_person_email(person_id: str, extra_email: str) -> bool:
    """Append a second address to the person (PATCH appends; Smartlead address stays first)."""
    extra = _bare_email(extra_email)
    if not person_id or not extra:
        return False
    try:
        vals = _attio("GET", f"/objects/people/records/{person_id}").get("data", {}).get("values", {})
        have = {(v.get("email_address") or "").lower() for v in vals.get("email_addresses", [])}
        if extra in have:
            return False
        _attio("PATCH", f"/objects/people/records/{person_id}",
               json={"data": {"values": {"email_addresses": [{"email_address": extra}]}}})
        return True
    except Exception as e:
        print(f"[attio] Could not add {extra} to person {person_id}: {e}")
        return False


def smartlead_lead_profile(lead_email: str) -> dict:
    """{"first_name","last_name","company_name"} from Smartlead, or {}."""
    if not SMARTLEAD_API_KEY or not lead_email:
        return {}
    try:
        d = requests.get(f"{SMARTLEAD_BASE}/leads/", params={"api_key": SMARTLEAD_API_KEY, "email": lead_email},
                         timeout=15).json() or {}
        return {k: (d.get(k) or "").strip() for k in ("first_name", "last_name", "company_name")}
    except Exception as e:
        print(f"[smartlead] lead profile failed for {lead_email}: {e}")
        return {}


def _split_name(full: str) -> tuple:
    parts = (full or "").strip().split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def ensure_person_name(person_id: str, first: str = "", last: str = "", full: str = "") -> bool:
    """Set the person's name if Attio has none. Never overwrites an existing name."""
    if not person_id:
        return False
    if not (first or last) and full:
        first, last = _split_name(full)
    if not (first or last):
        return False
    try:
        vals = _attio("GET", f"/objects/people/records/{person_id}").get("data", {}).get("values", {})
        if any((n.get("full_name") or "").strip() for n in vals.get("name", [])):
            return False
        _attio("PATCH", f"/objects/people/records/{person_id}", json={"data": {"values": {
            "name": [{"first_name": first, "last_name": last, "full_name": (first + " " + last).strip()}]}}})
        return True
    except Exception as e:
        print(f"[attio] Could not set name on person {person_id}: {e}")
        return False


def ensure_company_name(company_id: str, name: str) -> bool:
    """Set the company name if Attio has none (Attio usually enriches it from the domain)."""
    if not company_id or not (name or "").strip():
        return False
    try:
        vals = _attio("GET", f"/objects/companies/records/{company_id}").get("data", {}).get("values", {})
        if any((n.get("value") or "").strip() for n in vals.get("name", [])):
            return False
        _attio("PATCH", f"/objects/companies/records/{company_id}",
               json={"data": {"values": {"name": [{"value": name.strip()}]}}})
        return True
    except Exception as e:
        print(f"[attio] Could not set name on company {company_id}: {e}")
        return False


def _deal_values(sentiment: str = "", first_mailbox: str = "", mandate: dict = None,
                 first_reply: str = "", campaign_name: str = "", first_sent: str = "",
                 campaign_rec: str = "") -> dict:
    """Deal fields the bot owns (not stage/name, which are only set on create)."""
    values = _custom_values(sentiment, first_mailbox, first_sent)
    if campaign_rec:
        values["campaign"] = [{"target_object": "campaigns", "target_record_id": campaign_rec}]
    if ATTIO_DEAL_SOURCE:
        values["source"] = [{"option": ATTIO_DEAL_SOURCE}]
    if first_reply:
        values["first_reply_date"] = [{"value": first_reply}]
    if mandate and mandate.get("id"):
        values["mandate"] = [{"target_object": "mandates", "target_record_id": mandate["id"]}]
        if mandate.get("buyer_id"):
            values["buyer"] = [{"target_object": "companies", "target_record_id": mandate["buyer_id"]}]
    if campaign_name:
        values["campaign_old"] = [{"value": campaign_name}]
    return values


def deal_name(lead_email: str, sentiment: str = "", campaign_name: str = "") -> str:
    """'<email> - <latest reply status> (<campaign>)'. Every reply gets a deal now, so the
    name must follow the reply status instead of always saying 'Interested'."""
    status = sentiment if sentiment in REPLY_STATUS_OPTIONS else "Replied"
    return f"{lead_email} - {status}" + (f" ({campaign_name})" if campaign_name else "")


def create_attio_deal(lead_email: str, campaign_name: str = "",
                      person_id: str = None, company_id: str = None,
                      sentiment: str = "", first_mailbox: str = "",
                      mandate: dict = None, first_reply: str = "", first_sent: str = "",
                      campaign_rec: str = "") -> dict:
    values = {
        "name": [{"value": deal_name(lead_email, sentiment, campaign_name)}],
        "stage": [{"status": ATTIO_DEAL_STAGE}],
        **_deal_values(sentiment, first_mailbox, mandate, first_reply, campaign_name, first_sent, campaign_rec),
    }
    if ATTIO_OWNER_ID:
        values["owner"] = [{"referenced_actor_type": "workspace-member",
                            "referenced_actor_id": ATTIO_OWNER_ID}]
    if person_id:
        values["associated_people"] = [{"target_object": "people", "target_record_id": person_id}]
    if company_id:
        values["associated_company"] = [{"target_object": "companies", "target_record_id": company_id}]
    return _attio("POST", "/objects/deals/records", json={"data": {"values": values}})


def add_attio_note(parent_object: str, record_id: str, title: str, content: str,
                   created_at: str = None) -> dict:
    data = {
        "parent_object": parent_object,
        "parent_record_id": record_id,
        "title": title[:200],
        "format": "plaintext",
        "content": content,
    }
    if created_at:
        data["created_at"] = created_at
    return _attio("POST", "/notes", json={"data": data})


def _bot_named_deal(deal_id: str, lead_email: str) -> bool:
    """True if the deal still has a name the bot generated ('<email> - ...'). Deals the team
    renamed by hand (e.g. to the company name) are never renamed by the bot."""
    try:
        rec = _attio("GET", f"/objects/deals/records/{deal_id}").get("data", {})
        name = ((rec.get("values") or {}).get("name") or [{}])[0].get("value") or ""
    except Exception as e:
        print(f"[attio] Could not read deal name {deal_id}: {e}")
        return False
    return name.lower().startswith(f"{lead_email.lower()} - ")


def link_attio_deal(deal_id: str, person_id: str = None, company_id: str = None,
                    sentiment: str = "", first_mailbox: str = "",
                    mandate: dict = None, first_reply: str = "", campaign_name: str = "",
                    first_sent: str = "", campaign_rec: str = "", lead_email: str = "") -> None:
    """Attach person/company to a deal and refresh the bot-owned fields, incl. the name's reply status (PATCH; stage untouched)."""
    values = _deal_values(sentiment, first_mailbox, mandate, first_reply, campaign_name, first_sent, campaign_rec)
    if lead_email and sentiment in REPLY_STATUS_OPTIONS and _bot_named_deal(deal_id, lead_email):
        values["name"] = [{"value": deal_name(lead_email, sentiment, campaign_name)}]
    if person_id:
        values["associated_people"] = [{"target_object": "people", "target_record_id": person_id}]
    if company_id:
        values["associated_company"] = [{"target_object": "companies", "target_record_id": company_id}]
    if values:
        _attio("PATCH", f"/objects/deals/records/{deal_id}", json={"data": {"values": values}})


def _iso(ts: str):
    """Normalise a Smartlead/ISO timestamp for Attio's created_at."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except Exception:
        return None


def find_attio_deal_for_person(person_id: str, lead_email: str = ""):
    """
    Return the most recent deal for this lead, or None.
    Looks for deals linked to the person first, then falls back to deals whose
    name contains the lead email (deals made by earlier builds were not linked).
    """
    filters = []
    if person_id:
        filters.append({"associated_people": {"target_object": "people", "target_record_id": person_id}})
    if lead_email:
        filters.append({"name": {"$contains": lead_email}})
    for flt in filters:
        try:
            res = _attio("POST", "/objects/deals/records/query", json={
                "filter": flt,
                "sorts": [{"attribute": "created_at", "direction": "desc"}],
                "limit": 1,
            })
            rows = res.get("data", [])
            if rows:
                return rows[0].get("id", {}).get("record_id")
        except Exception as e:
            print(f"[attio] Deal lookup failed ({list(flt)[0]}): {e}")
    return None


def _trim_quoted(text: str) -> str:
    """Keep only the new part of an email body, dropping quoted history."""
    if not text:
        return ""
    cut = len(text)
    for pat in (r"\n\s*On .{5,120}wrote:", r"\n\s*From:\s", r"-{3,}\s*Original Message\s*-{3,}",
                r"\n\s*Sent from my ", r"\n>\s"):
        m = re.search(pat, text)
        if m and m.start() < cut:
            cut = m.start()
    return text[:cut].strip()


def _trim_signature(text: str) -> str:
    """Drop everything from a sign-off line onward (for compact timeline entries)."""
    if not text:
        return ""
    m = re.search(r"(?:^|\n)\s*(best regards|kind regards|warm regards|regards|best|thanks|thank you|cheers|sincerely|all the best)\s*[,!.]?\s*(?:\n|$)",
                  text, re.I)
    return text[:m.start()].strip() if m and m.start() > 0 else text.strip()


SUMMARY_NOTE_TITLE = "Conversation summary"


def _fmt_time(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc)
        return dt.strftime("%b %d, %Y %I:%M %p UTC").replace(" 0", " ")
    except Exception:
        return str(ts or "-")


def fetch_thread_messages(campaign_id, lead_id) -> tuple:
    """
    Pull the whole thread from Smartlead, oldest first.
    Returns (our_mailbox, [ {dir, who, time, text, kind} ]).
    """
    resp = requests.get(
        f"{SMARTLEAD_BASE}/campaigns/{campaign_id}/leads/{lead_id}/message-history",
        params={"api_key": SMARTLEAD_API_KEY}, timeout=20,
    )
    resp.raise_for_status()
    data = resp.json() or {}
    history = data.get("history", []) if isinstance(data, dict) else []
    mailbox = data.get("from", "") if isinstance(data, dict) else ""
    msgs = []
    for m in history:
        who = _bare_email(m.get("from"))
        text = _trim_quoted(_strip_html(m.get("email_body") or ""))
        direction = "outbound" if is_own_address(who) else "inbound"
        kind = "campaign email" if (direction == "outbound" and str(m.get("type", "")).upper() == "SENT"
                                    and not m.get("reply_details") and m.get("email_seq_number")) else ""
        msgs.append({"dir": direction, "who": who, "time": m.get("time", ""), "text": text, "kind": kind})
    msgs.sort(key=lambda x: str(x.get("time", "")))
    return mailbox, msgs


def first_outreach_mailbox(messages: list, fallback: str = "") -> str:
    """The mailbox that sent the very first email in the thread."""
    for m in messages:
        if m["dir"] == "outbound" and m.get("who"):
            return m["who"]
    return _bare_email(fallback)


def summarize_conversation(messages: list, campaign_name: str, lead_email: str) -> dict:
    """Ask Claude for status / summary / key details / next step. Facts only."""
    out = {"status": "", "summary": "", "details": [], "next_step": ""}
    if not messages:
        return out
    thread = "\n\n".join(
        f"[{i+1}] {_fmt_time(m['time'])} | {'PROSPECT' if m['dir']=='inbound' else 'OUR TEAM (' + m['who'] + ')'}:\n{m['text'][:1500]}"
        for i, m in enumerate(messages)
    )
    prompt = f"""You maintain a CRM note for a sales conversation between our team at Hitch and a prospect ({lead_email}), campaign "{campaign_name}".
Below is the full email thread, oldest first.

Write exactly these four sections, plain text, no markdown:
STATUS: one line on where things stand right now (e.g. "Call proposed for Thursday early afternoon, time not yet confirmed")
SUMMARY: 2-4 sentences: what the prospect wants or asked, their concerns, what we have told them so far
KEY DETAILS: one fact per line starting with "- ": phone numbers, proposed times, names and roles, requests (NDA, documents), objections, any numbers mentioned. Write "- none" if nothing.
NEXT STEP: one line, a concrete action for our team

Rules: use only facts that appear in the thread. Never invent. Be brief.

THREAD:
{thread}"""
    try:
        msg = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
        txt = msg.content[0].text.strip()
        sec = {"STATUS": "", "SUMMARY": "", "KEY DETAILS": "", "NEXT STEP": ""}
        current = None
        for line in txt.splitlines():
            m = re.match(r"^\s*(STATUS|SUMMARY|KEY DETAILS|NEXT STEP)\s*:\s*(.*)$", line, re.I)
            if m:
                current = m.group(1).upper()
                sec[current] = m.group(2).strip()
            elif current:
                sec[current] += ("\n" if sec[current] else "") + line.rstrip()
        out["status"] = sec["STATUS"].strip()
        out["summary"] = sec["SUMMARY"].strip()
        out["details"] = [l.strip().lstrip("-").strip() for l in sec["KEY DETAILS"].splitlines() if l.strip()]
        out["next_step"] = sec["NEXT STEP"].strip()
    except Exception as e:
        print(f"[summary] Claude failed: {e}")
    return out


def build_summary_note(lead_email: str, campaign_name: str, sentiment: str, mailbox: str,
                       messages: list, ai: dict, inbox_link: str = "") -> tuple:
    inbound = [m for m in messages if m["dir"] == "inbound"]
    last = messages[-1] if messages else None
    now = datetime.now(timezone.utc)
    title = f"{SUMMARY_NOTE_TITLE} - {campaign_name or 'Smartlead'} ({sentiment}) - updated {now.strftime('%b %d, %Y %H:%M')} UTC"

    lines = [
        f"Status: {ai.get('status') or '-'}",
        f"Sentiment: {sentiment} (latest reply)",
        f"Campaign: {campaign_name or '-'}",
        f"Lead: {lead_email}",
        f"First email sent from: {first_outreach_mailbox(messages, mailbox) or '-'}",
        f"Campaign mailbox: {mailbox or '-'}",
        f"Messages: {len(messages)} ({len(inbound)} from lead)",
        f"Last message: {_fmt_time(last['time']) if last else '-'}" + (f" ({last['dir']})" if last else ""),
    ]
    if inbox_link:
        lines.append(f"Smartlead thread: {inbox_link}")
    lines += ["", "SUMMARY", ai.get("summary") or "(summary unavailable)", "", "KEY DETAILS"]
    lines += [f"- {d}" for d in (ai.get("details") or ["none"])]
    lines += ["", "NEXT STEP", ai.get("next_step") or "-", "", "TIMELINE"]
    for m in messages:
        who = "Lead" if m["dir"] == "inbound" else (m["who"] or "Hitch")
        label = f"{who} ({m['dir']}{', ' + m['kind'] if m.get('kind') else ''})"
        body = re.sub(r"\s+", " ", _trim_signature(m["text"])).strip()
        limit = 200 if m.get("kind") == "campaign email" else 400
        if len(body) > limit:
            body = body[:limit].rstrip() + "..."
        lines.append(f"{_fmt_time(m['time'])} - {label}: {body}")
    return title[:200], "\n".join(lines)


def replace_summary_note(parent_object: str, record_id: str, title: str, content: str) -> None:
    """Delete any earlier summary note on this record, then create the new one."""
    try:
        notes = _attio("GET", "/notes", params={"parent_object": parent_object,
                                              "parent_record_id": record_id, "limit": 50}).get("data", [])
        for n in notes:
            if str(n.get("title", "")).startswith(SUMMARY_NOTE_TITLE):
                nid = n.get("id", {}).get("note_id")
                if nid:
                    _attio("DELETE", f"/notes/{nid}")
    except Exception as e:
        print(f"[attio] Could not clear old summary notes on {parent_object}/{record_id}: {e}")
    add_attio_note(parent_object, record_id, title, content)


def _load_thread(campaign_id, lead_id) -> tuple:
    if campaign_id and lead_id and SMARTLEAD_API_KEY:
        try:
            return fetch_thread_messages(campaign_id, lead_id)
        except Exception as e:
            print(f"[attio] Thread fetch failed: {e}")
    return "", []


def refresh_conversation(lead_email: str, campaign_id, lead_id, campaign_name: str, sentiment: str,
                         person_id: str, deal_id: str = None, pending: dict = None,
                         inbox_link: str = "", thread: tuple = None) -> str:
    """
    Rebuild the living 'Conversation summary' note from the Smartlead thread and
    put it on the person (and deal). `pending` is a message we just sent that may
    not be in Smartlead's history yet. Returns a short status string.
    """
    mailbox, messages = thread if thread else _load_thread(campaign_id, lead_id)
    messages = list(messages)
    if pending:
        head = re.sub(r"\s+", " ", pending.get("text", ""))[:60]
        if not any(head and head in re.sub(r"\s+", " ", m["text"]) for m in messages):
            messages.append(pending)
            messages.sort(key=lambda x: str(x.get("time", "")))
    if not messages:
        return "no thread"
    ai = summarize_conversation(messages, campaign_name, lead_email)
    title, content = build_summary_note(lead_email, campaign_name, sentiment, mailbox, messages, ai, inbox_link)
    targets = []
    if person_id:
        replace_summary_note("people", person_id, title, content)
        targets.append("person")
    if deal_id:
        replace_summary_note("deals", deal_id, title, content)
        targets.append("deal")
    return "summary on " + "+".join(targets) if targets else "no target"


def _set_status_if_empty(obj: str, record_id: str, status: str) -> bool:
    """Set Reply status only when the record has none yet (used for auto-replies)."""
    if not record_id:
        return False
    try:
        vals = _attio("GET", f"/objects/{obj}/records/{record_id}").get("data", {}).get("values", {})
        if vals.get(REPLY_STATUS_SLUG):
            return False
        _attio("PATCH", f"/objects/{obj}/records/{record_id}",
               json={"data": {"values": {REPLY_STATUS_SLUG: [{"option": status}]}}})
        return True
    except Exception as e:
        print(f"[attio] Could not set status on {obj} {record_id}: {e}")
        return False


def _latest_sentiment(person_id: str) -> str:
    """Read the person's current Reply status so outbound updates keep it."""
    try:
        rec = _attio("GET", f"/objects/people/records/{person_id}")
        vals = rec.get("data", {}).get("values", {}).get(REPLY_STATUS_SLUG, [])
        return (vals[0].get("option", {}) or {}).get("title", "") if vals else ""
    except Exception:
        return ""


def log_outbound_to_attio(lead_email: str, author: str, text: str, when: str = "",
                          campaign_name: str = "", via: str = "",
                          campaign_id=None, lead_id=None, inbox_link: str = "") -> dict:
    """
    An email WE sent to the lead (bot reply or a teammate's manual email):
    refresh the conversation summary on the person and deal. Never raises.
    """
    if not ATTIO_API_KEY or not lead_email:
        return {"ok": False, "summary": "Attio: not configured"}
    try:
        thread = _load_thread(campaign_id, lead_id)
        first_mb = first_outreach_mailbox(thread[1], thread[0])
        person_id = _record_id(upsert_attio_person(lead_email, first_mailbox=first_mb))
        deal_id = find_attio_deal_for_person(person_id, lead_email)
        sentiment = _latest_sentiment(person_id) or "Neutral"
        pending = {"dir": "outbound", "who": author or "Hitch team", "time": when or datetime.now(timezone.utc).isoformat(),
                   "text": _trim_quoted(text), "kind": ""}
        res = refresh_conversation(lead_email, campaign_id, lead_id, campaign_name, sentiment,
                                   person_id, deal_id, pending=pending, inbox_link=inbox_link, thread=thread)
        summary = f"Attio: outbound logged ({res})"
        print(f"[attio] {summary} for {lead_email} from {author} via {via}")
        return {"ok": True, "summary": summary}
    except Exception as e:
        print(f"[attio] Outbound log FAILED for {lead_email}: {e}")
        return {"ok": False, "summary": f"Attio: outbound log failed ({e})"}


def sync_to_attio(lead_email: str, sentiment: str, campaign_name: str = "",
                  lead_response: str = "", reply_time: str = "", sender: str = "",
                  campaign_id=None, lead_id=None, inbox_link: str = "",
                  reply_from: str = "", lead_name: str = "") -> dict:
    """
    For EVERY real lead reply: upsert company + person, set Reply status on both,
    create or reuse a deal for every reply (name carries the latest reply status), then rebuild the
    (Out-of-office / auto-replies never create a deal and never overwrite an existing Reply status.)
    single living 'Conversation summary' note on the person and deal.
    Never raises -- Attio problems must not block Slack.
    """
    if not ATTIO_API_KEY:
        return {"ok": False, "summary": "Attio: not configured", "deal_id": None}

    domain = lead_email.split("@", 1)[1] if "@" in lead_email else ""
    done, step = [], "custom fields"
    # Out-of-office / auto-replies: never create a deal, and never overwrite a status set by a real reply
    auto = is_auto_reply(lead_response)
    status = "" if auto else sentiment
    try:
        ensure_reply_status_attribute()

        step = "thread"
        thread = _load_thread(campaign_id, lead_id)
        first_mb = first_outreach_mailbox(thread[1], thread[0] or sender)
        first_sent = first_sent_date(thread[1])

        step = "profile"
        profile = smartlead_lead_profile(lead_email)
        first, last = profile.get("first_name", ""), profile.get("last_name", "")
        if not (first or last) and lead_name:
            first, last = _split_name(lead_name)

        step = "company"
        company_id = _record_id(upsert_attio_company(domain, status, first_mb, first_sent)) if domain else None
        if company_id:
            if auto and _set_status_if_empty("companies", company_id, "Neutral"):
                done.append("company (Neutral, auto-reply)")
            else:
                done.append(f"company ({status or 'status kept, auto-reply'})")
            if ensure_source("companies", company_id, ATTIO_DEAL_SOURCE):
                done.append("company source")
            if ensure_company_name(company_id, profile.get("company_name", "")):
                done.append("company name")
            if ensure_company_hitch_role(company_id, "Seller"):
                done.append("role Seller")

        step = "person"
        person_id = _record_id(upsert_attio_person(lead_email, status, first_mb, first_sent))
        if auto and _set_status_if_empty("people", person_id, "Neutral"):
            done.append("person (Neutral, auto-reply)")
        else:
            done.append(f"person ({status or 'status kept, auto-reply'})")
        if ensure_source("people", person_id, ATTIO_DEAL_SOURCE):
            done.append("person source")
        if ensure_person_name(person_id, first, last):
            done.append(f"name {first} {last}".strip())
        if reply_from and _bare_email(reply_from) != lead_email.lower() and not is_own_address(reply_from):
            if ensure_person_email(person_id, reply_from):
                done.append(f"added email {_bare_email(reply_from)}")

        step = "mandate"
        mandate = mandate_for_campaign(campaign_name)
        first_reply = first_reply_date(thread[1]) or ((_iso(reply_time) or "")[:10])

        step = "campaign"
        campaign_rec = campaign_record_for(campaign_id, campaign_name, mandate)

        deal_id = None
        step = "deal"
        existing = find_attio_deal_for_person(person_id, lead_email)
        if existing:
            deal_id = existing
            try:
                link_attio_deal(deal_id, person_id, company_id, status, first_mb,
                                mandate, first_reply, campaign_name, first_sent, campaign_rec,
                                lead_email=lead_email)
            except Exception as e:
                print(f"[attio] Could not update existing deal {deal_id}: {e}")
            done.append("existing deal")
        elif auto:
            done.append("no deal (out-of-office / auto-reply)")
        else:
            deal_id = _record_id(create_attio_deal(lead_email, campaign_name, person_id, company_id,
                                                   sentiment, first_mb, mandate, first_reply,
                                                   first_sent, campaign_rec))
            done.append(f"new deal ({ATTIO_DEAL_STAGE})")
        if deal_id and mandate.get("id"):
            done.append(f"mandate {mandate.get('name') or mandate['id'][:8]}")
        elif deal_id:
            done.append("mandate UNMAPPED - ask Dylan")

        step = "summary note"
        pending = {"dir": "inbound", "who": lead_email, "time": reply_time, "text": lead_response or "", "kind": ""}
        res = refresh_conversation(lead_email, campaign_id, lead_id, campaign_name, sentiment,
                                   person_id, deal_id, pending=pending, inbox_link=inbox_link, thread=thread)
        done.append(res)
        if campaign_rec and deal_id:
            done.append(f"campaign {campaign_name}")
        if first_mb:
            done.append(f"first email from {first_mb}")
        if first_sent:
            done.append(f"first sent {first_sent}")

        summary = "Attio: synced " + " + ".join(done)
        print(f"[attio] {summary} for {lead_email} deal_id={deal_id}")
        return {"ok": True, "summary": summary, "deal_id": deal_id}
    except requests.HTTPError as e:
        detail = ""
        try:
            detail = e.response.json().get("message") or e.response.text[:120]
        except Exception:
            detail = (e.response.text[:120] if e.response is not None else str(e))
        code = e.response.status_code if e.response is not None else ""
        print(f"[attio] FAILED at {step}: {code} {detail}")
        return {"ok": False, "summary": f"Attio: failed at {step} ({code} {detail})", "deal_id": None}
    except Exception as e:
        print(f"[attio] FAILED at {step}: {e}")
        return {"ok": False, "summary": f"Attio: failed at {step} ({e})", "deal_id": None}


def send_slack_message(blocks: list) -> dict:
    resp = requests.post(
        SLACK_WEBHOOK_URL,
        json={"blocks": blocks},
    )
    resp.raise_for_status()
    return resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"ok": True}


def post_slack_chat(channel: str, thread_ts: str, text: str) -> dict:
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"channel": channel, "thread_ts": thread_ts, "text": text},
    )
    resp.raise_for_status()
    return resp.json()


def fetch_slack_thread(channel: str, ts: str) -> dict:
    resp = requests.get(
        "https://slack.com/api/conversations.replies",
        params={"channel": channel, "ts": ts, "limit": 20},
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
    )
    resp.raise_for_status()
    return resp.json()


def _strip_html(html: str) -> str:
    """Very light HTML -> text for fallbacks/logging."""
    if not html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"</p>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text).replace("\xa0", " ")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _bare_email(value) -> str:
    """'Devon Kessler <devon@hitch-advisors.com>' -> 'devon@hitch-advisors.com'"""
    m = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+", str(value or ""))
    return m.group(0).lower() if m else ""


def is_own_address(value) -> bool:
    """True if the address belongs to one of our own sending domains."""
    email = _bare_email(value)
    if "@" not in email:
        return False
    domain = email.split("@", 1)[1]
    return any(k in domain for k in OWN_DOMAIN_KEYWORDS)


def find_reply_author(body: dict, campaign_id, lead_id, message_id: str) -> str:
    """
    Work out who actually wrote the message Smartlead is calling a 'reply'.
    Smartlead also fires EMAIL_REPLY for emails OUR team sends into the thread
    from outside Smartlead, so the author must be checked.
    """
    corr = body.get("leadCorrespondence") or {}
    reply_msg = body.get("reply_message") or {}
    for candidate in (corr.get("replyReceivedFrom"), reply_msg.get("from"),
                      reply_msg.get("from_email"), body.get("reply_from_email")):
        email = _bare_email(candidate)
        if email:
            return email

    # Not in the payload -- look the message up in Smartlead's thread history
    if campaign_id and lead_id and message_id and SMARTLEAD_API_KEY:
        try:
            resp = requests.get(
                f"{SMARTLEAD_BASE}/campaigns/{campaign_id}/leads/{lead_id}/message-history",
                params={"api_key": SMARTLEAD_API_KEY}, timeout=15,
            )
            resp.raise_for_status()
            for m in (resp.json() or {}).get("history", []):
                if m.get("message_id") == message_id:
                    return _bare_email(m.get("from"))
        except Exception as e:
            print(f"[author] History lookup failed: {e}")
    return ""


def fetch_smartlead_thread(campaign_id, lead_id) -> dict:
    """
    GET /campaigns/{campaign_id}/leads/{lead_id}/message-history
    Returns the latest lead REPLY's stats_id/message_id plus the sender account
    and a combined thread HTML. Used as a fallback when Slack button metadata
    was too large to carry the thread.
    """
    resp = requests.get(
        f"{SMARTLEAD_BASE}/campaigns/{campaign_id}/leads/{lead_id}/message-history",
        params={"api_key": SMARTLEAD_API_KEY},
    )
    resp.raise_for_status()
    data = resp.json()
    history = data.get("history", []) if isinstance(data, dict) else data
    if not history:
        raise ValueError(f"[thread] No message history for campaign={campaign_id} lead={lead_id}")

    replies = [m for m in history if str(m.get("type", "")).upper() == "REPLY"]
    latest_reply = replies[-1] if replies else history[-1]
    sent = [m for m in history if str(m.get("type", "")).upper() == "SENT"]

    thread_html = "".join(f"<div>{m.get('email_body', '')}</div>" for m in history)
    print(f"[thread] history={len(history)} msgs, latest reply stats_id={latest_reply.get('stats_id')}")
    return {
        "stats_id": latest_reply.get("stats_id"),
        "message_id": latest_reply.get("message_id"),
        "reply_time": latest_reply.get("time"),
        "reply_html": latest_reply.get("email_body", ""),
        "eaccount": (sent[-1].get("from") if sent else data.get("from", "")) or "",
        "subject": latest_reply.get("subject") or (sent[-1].get("subject") if sent else ""),
        "thread_html": thread_html,
    }


def slack_text_to_html(text: str) -> str:
    """
    Convert text typed in Slack (mrkdwn) or a plain Claude draft into email HTML.

    Slack rewrites what people type:  <http://x.com|x.com>, <mailto:a@b|a@b>,
    <@U123>, &amp;, :smiley:, *bold*, _italic_, ~strike~.  This undoes all of
    that, escapes anything else, autolinks bare URLs, and turns newlines into <br>.
    """
    if not text:
        return ""

    tokens = []  # placeholders for pieces that are already HTML

    def stash(html_piece: str) -> str:
        tokens.append(html_piece)
        return f"\x00{len(tokens) - 1}\x00"

    # <mailto:addr|label> / <mailto:addr>
    text = re.sub(r"<mailto:([^|>]+)(?:\|([^>]*))?>",
                  lambda m: stash(f'<a href="mailto:{html_lib.escape(m.group(1))}">{html_lib.escape(m.group(2) or m.group(1))}</a>'),
                  text)
    # <url|label> / <url>
    text = re.sub(r"<(https?://[^|>\s]+)(?:\|([^>]*))?>",
                  lambda m: stash(f'<a href="{html_lib.escape(m.group(1))}">{html_lib.escape(m.group(2) or m.group(1))}</a>'),
                  text)
    # <#C123|channel-name> -> #channel-name ; <@U123> / <!here> -> dropped
    text = re.sub(r"<#[A-Z0-9]+\|([^>]*)>", r"#\1", text)
    text = re.sub(r"<[@!][^>]*>", "", text)

    # Slack sends &amp; &lt; &gt; -- unescape to real chars, then escape for HTML
    text = html_lib.unescape(text)
    text = html_lib.escape(text, quote=False)

    # Emoji shortcodes -> unicode
    if emoji_lib is not None:
        text = emoji_lib.emojize(text, language="alias")

    # Autolink bare URLs the user typed without Slack wrapping them
    text = re.sub(r"(?<![\"'>\x00])(https?://[^\s<>\"']+)",
                  lambda m: stash(f'<a href="{m.group(1)}">{m.group(1)}</a>'), text)

    # Basic Slack formatting
    text = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)~([^~\n]+)~(?!\w)", r"<s>\1</s>", text)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)

    text = text.replace("\r\n", "\n").replace("\n", "<br>")

    # Restore stashed HTML pieces
    text = re.sub(r"\x00(\d+)\x00", lambda m: tokens[int(m.group(1))], text)
    return text


def send_smartlead_reply(campaign_id, stats_id, reply_message_id: str, body: str,
                         lead_email: str = "", reply_email_time: str = "",
                         reply_email_body: str = "") -> dict:
    """
    Send a reply via Smartlead's master inbox:
    POST /campaigns/{campaign_id}/reply-email-thread?api_key=...
    Smartlead threads the message itself using reply_message_id, so we only
    send our own HTML body (no manual quoting of the prior thread).
    """
    html_body = "<div>" + slack_text_to_html(body) + "</div>"

    payload = {
        "email_stats_id": stats_id,
        "email_body": html_body,
        "reply_message_id": reply_message_id,
        "add_signature": False,
    }
    if lead_email:
        payload["to_email"] = lead_email
    if reply_email_time:
        payload["reply_email_time"] = reply_email_time
    if reply_email_body:
        payload["reply_email_body"] = reply_email_body

    print(f"[send_reply] campaign={campaign_id} payload={json.dumps(payload)[:500]}")
    resp = requests.post(
        f"{SMARTLEAD_BASE}/campaigns/{campaign_id}/reply-email-thread",
        params={"api_key": SMARTLEAD_API_KEY},
        headers={"Content-Type": "application/json"},
        json=payload,
    )
    print(f"[send_reply] Smartlead status={resp.status_code} body={resp.text[:300]}")
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        return {"ok": True, "raw": resp.text[:300]}


def _send_from_meta(meta: dict, body: str) -> dict:
    """Shared send path for the Slack button and the Slack thread edit flow."""
    campaign_id = meta.get("campaign_id")
    stats_id = meta.get("stats_id")
    message_id = meta.get("message_id", "")
    lead_email = clean_slack_email(meta.get("lead_email", ""))
    reply_time = meta.get("reply_time", "")
    reply_html = meta.get("reply_html", "")

    # If button metadata was trimmed, refetch the latest reply from Smartlead
    if meta.get("refetch_thread") and campaign_id and meta.get("lead_id"):
        try:
            t = fetch_smartlead_thread(campaign_id, meta["lead_id"])
            stats_id = t["stats_id"] or stats_id
            message_id = t["message_id"] or message_id
            reply_time = t["reply_time"] or reply_time
            reply_html = t["reply_html"] or reply_html
        except Exception as e:
            print(f"[send_reply] Failed to refetch thread: {e}")

    if not (campaign_id and stats_id):
        raise ValueError(f"missing campaign_id or stats_id. meta keys: {list(meta.keys())}")

    return send_smartlead_reply(
        campaign_id=campaign_id,
        stats_id=stats_id,
        reply_message_id=message_id,
        body=body,
        lead_email=lead_email,
        reply_email_time=reply_time,
        reply_email_body=reply_html,
    )


# ============================================================
# ROUTE 1: Incoming email reply webhook (from Smartlead, EMAIL_REPLY event)
# ============================================================

@app.route("/webhook/incoming", methods=["POST"])
def incoming_reply():
    data = request.json or {}
    body = data.get("body", data)

    if SMARTLEAD_WEBHOOK_SECRET and body.get("secret_key") != SMARTLEAD_WEBHOOK_SECRET:
        print("[incoming] Rejected webhook: bad secret_key")
        return jsonify({"status": "unauthorized"}), 401

    event_type = body.get("event_type", "")
    if event_type and event_type != "EMAIL_REPLY":
        print(f"[incoming] Ignoring event_type={event_type}")
        return jsonify({"status": "ignored", "event_type": event_type}), 200

    # --- Smartlead payload fields ---
    lead_email = str(body.get("sl_lead_email") or body.get("to_email") or "")
    eaccount = str(body.get("from_email", ""))          # our sending mailbox
    campaign_id = body.get("campaign_id", "")
    campaign_name = body.get("campaign_name", "")
    lead_id = body.get("sl_email_lead_id", "")
    stats_id = body.get("stats_id", "")
    message_id = body.get("message_id") or (body.get("reply_message") or {}).get("message_id", "")
    reply_time = body.get("time_replied") or body.get("event_timestamp", "")
    reply_category = body.get("reply_category", "")
    subject = body.get("subject") or "Re:"
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    reply_msg = body.get("reply_message") or {}
    sent_msg = body.get("sent_message") or {}
    reply_html = reply_msg.get("html") or body.get("reply_body", "")
    reply_snippet = reply_msg.get("text") or body.get("preview_text") or _strip_html(reply_html)
    sent_text = sent_msg.get("text") or _strip_html(sent_msg.get("html") or body.get("sent_message_body", ""))

    if not stats_id or not campaign_id:
        print(f"[incoming] Missing stats_id or campaign_id. keys={list(body.keys())}")
        return jsonify({"status": "skipped", "reason": "missing_ids"}), 200

    print(f"[incoming] campaign={campaign_id} ({campaign_name}) lead={lead_email} from={eaccount} stats_id={stats_id} category={reply_category}")

    # --- Only real lead replies get a card. Skip anything written by our own team. ---
    reply_author = find_reply_author(body, campaign_id, lead_id, message_id)
    if is_own_address(lead_email):
        print(f"[incoming] Skipping: lead address {lead_email} is one of our own domains")
        return jsonify({"status": "skipped", "reason": "lead_is_own_domain"}), 200
    outbound_reason = None
    if is_own_address(reply_author):
        outbound_reason = "sent_by_own_team"
    elif eaccount and not is_own_address(eaccount):
        # The mailbox that received a genuine reply is always ours. If it is not,
        # this event is one of our outgoing emails that Smartlead logged as a reply.
        outbound_reason = "mailbox_not_own"
    if outbound_reason:
        author = reply_author if is_own_address(reply_author) else "Hitch team"
        print(f"[incoming] Outbound message by {author} ({outbound_reason}); logging to Attio, no Slack card")
        logged = log_outbound_to_attio(
            lead_email, author, reply_snippet or _strip_html(reply_html), reply_time,
            campaign_name, via="manual email (seen by Smartlead)",
            campaign_id=campaign_id, lead_id=lead_id,
            inbox_link=body.get("ui_master_inbox_link") or body.get("app_url", ""))
        return jsonify({"status": "logged_outbound", "reason": outbound_reason,
                        "author": author, "attio": logged["summary"]}), 200

    # Step 1: Clean lead response for Slack display
    lead_response = extract_lead_response(reply_html, reply_snippet, campaign_name)

    # Step 2: Sender name from our mailbox
    sender_name = extract_sender_name(eaccount)

    # Step 3: Attio CRM sync (runs after sentiment so deals are only made for Positive replies)

    # Step 3b: Sentiment label for the Slack card
    sentiment = classify_sentiment(lead_response, campaign_name)
    attio = sync_to_attio(lead_email, sentiment, campaign_name,
                          lead_response=lead_response, reply_time=reply_time, sender=eaccount,
                          campaign_id=campaign_id, lead_id=lead_id,
                          inbox_link=body.get("ui_master_inbox_link") or body.get("app_url", ""),
                          reply_from=reply_author, lead_name=str(body.get("to_name") or ""))

    # Step 4: Claude draft
    # Calendly slot fetching disabled for now:
    # cal_info = get_calendly_info(eaccount, campaign_name)
    # available_slots = fetch_available_slots(cal_info["event_type"])
    # slack_slots_text = format_slots_for_slack(available_slots, cal_info["fallback_url"])
    thread_for_claude = (
        "ORIGINAL OUTBOUND EMAIL (sent by us):\n" + sent_text +
        "\n\n---\n\nPROSPECT'S REPLY:\n" + lead_response
    )
    draft = draft_reply(sender_name, eaccount, lead_email, campaign_name, thread_for_claude)
    draft = scrub_draft(draft)
    print(f"[draft] Generated {len(draft)} chars for {lead_email}")

    # Step 5: Slack message with action buttons
    base_meta = {
        "campaign_id": campaign_id,
        "stats_id": stats_id,
        "message_id": message_id,
        "lead_id": lead_id,
        "eaccount": eaccount,
        "lead_email": lead_email,
        "subject": subject,
        "reply_time": reply_time,
    }
    meta_send = json.dumps({**base_meta, "draft": draft, "reply_html": reply_html})
    meta_edit = json.dumps({**base_meta, "draft": draft})
    meta_dismiss = json.dumps({"lead_email": lead_email, "stats_id": stats_id})

    if len(meta_send) > 1900:
        print(f"[warn] Meta payload too large for Slack button (send={len(meta_send)}). Dropping reply_html.")
        meta_send = json.dumps({**base_meta, "draft": draft, "refetch_thread": True})
    if len(meta_send) > 1900 or len(meta_edit) > 1900:
        print(f"[warn] Meta still too large (send={len(meta_send)}, edit={len(meta_edit)}). Dropping draft from buttons.")
        meta_send = json.dumps({**base_meta, "refetch_thread": True})
        meta_edit = json.dumps({**base_meta, "refetch_thread": True})

    inbox_link = body.get("ui_master_inbox_link") or body.get("app_url", "")
    no_response = draft.strip().upper().startswith("NO RESPONSE")

    sentiment_icon = {"Positive": "\U0001f7e2", "Negative": "\U0001f534"}.get(sentiment, "\u26aa")

    # Lead's reply as a quote block (Slack section text limit is 3000 chars)
    quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in lead_response.strip().splitlines())
    if len(quoted) > 2500:
        quoted = quoted[:2500].rstrip() + "\n> _[truncated]_"

    # Received time in a friendly format
    try:
        received = datetime.fromisoformat(str(reply_time).replace("Z", "+00:00"))
        received_str = received.strftime("%b %d, %Y at %I:%M %p UTC").replace(" 0", " ")
    except Exception:
        received_str = ""

    fields = [
        {"type": "mrkdwn", "text": f"*Campaign*\n{campaign_name or '-'}"},
        {"type": "mrkdwn", "text": f"*Sentiment*\n{sentiment_icon} {sentiment}"},
        {"type": "mrkdwn", "text": f"*Lead*\n{lead_email or '-'}"},
        {"type": "mrkdwn", "text": f"*Sent from*\n{eaccount or '-'}"},
    ]
    if reply_category:
        fields.append({"type": "mrkdwn", "text": f"*Smartlead category*\n{reply_category}"})

    context_parts = []
    if inbox_link:
        context_parts.append(f"<{inbox_link}|Open in Smartlead inbox>")
    if received_str:
        context_parts.append(f"Received {received_str}")
    if subject:
        context_parts.append(f"Subject: {subject}")
    if ATTIO_API_KEY:
        context_parts.append(("\u2705 " if attio["ok"] else "\u26a0\ufe0f ") + attio["summary"])

    if no_response:
        draft_section = (
            "*Suggested reply*\n"
            "_No reply recommended. The lead declined or asked not to be contacted._\n"
            "Use *Edit & Send* if you still want to respond."
        )
    else:
        draft_section = f"*Suggested reply*\n{draft}"

    buttons = []
    if not no_response:
        buttons.append(
            {"type": "button", "text": {"type": "plain_text", "text": "Send reply", "emoji": True},
             "style": "primary", "action_id": "send_reply", "value": meta_send,
             "confirm": {
                 "title": {"type": "plain_text", "text": "Send this reply?"},
                 "text": {"type": "mrkdwn", "text": f"This will email *{lead_email}* from *{eaccount}* via Smartlead."},
                 "confirm": {"type": "plain_text", "text": "Send"},
                 "deny": {"type": "plain_text", "text": "Cancel"},
             }},
        )
    buttons.append(
        {"type": "button", "text": {"type": "plain_text", "text": "Edit & send", "emoji": True},
         "action_id": "edit_reply", "value": meta_edit},
    )
    buttons.append(
        {"type": "button", "text": {"type": "plain_text", "text": "Dismiss", "emoji": True},
         "style": "danger", "action_id": "dismiss", "value": meta_dismiss},
    )

    blocks = [
        {"type": "header", "text": {"type": "plain_text",
            "text": f"{sentiment_icon} New reply from {lead_email}"[:150], "emoji": True}},
        {"type": "section", "fields": fields},
    ]
    if context_parts:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": "  \u2022  ".join(context_parts)}]})
    blocks += [
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Lead's reply*\n{quoted}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": draft_section}},
        {"type": "actions", "elements": buttons},
    ]

    send_slack_message(blocks)

    return jsonify({"status": "posted_to_slack", "stats_id": stats_id}), 200


# ============================================================
# ROUTE 2: Slack interactive actions (button clicks)
# ============================================================

@app.route("/webhook/slack-actions", methods=["POST"])
def slack_actions():
    payload = json.loads(request.form.get("payload", "{}"))
    action = payload.get("actions", [{}])[0]
    action_id = action.get("action_id", "")
    meta = json.loads(action.get("value", "{}"))
    print(f"[slack_action] action_id={action_id} stats_id={meta.get('stats_id')} campaign={meta.get('campaign_id')} lead={meta.get('lead_email')}")

    channel_id = payload.get("container", {}).get("channel_id", "")
    message_ts = payload.get("container", {}).get("message_ts", "")
    response_url = payload.get("response_url")
    lead_email = clean_slack_email(meta.get("lead_email", ""))

    if action_id == "send_reply":
        draft = meta.get("draft", "")
        stats_id = meta.get("stats_id", "")

        dedup_key = f"{stats_id}:send"
        if dedup_key in _sent_replies:
            print(f"[send_reply] Already sent for {dedup_key}. Skipping.")
            return "", 200
        _sent_replies.add(dedup_key)

        if not draft:
            print("[send_reply] SKIPPED -- draft missing from button meta (too large). Use Edit & Send.")
            if response_url:
                requests.post(response_url, json={
                    "replace_original": "false",
                    "text": "⚠️ Draft too long for the button. Use Edit & Send instead.",
                })
            return "", 200

        try:
            result = _send_from_meta(meta, draft)
            print(f"[send_reply] Smartlead response: {result}")
            log_outbound_to_attio(lead_email, clean_slack_email(meta.get("eaccount", "")), draft,
                                  datetime.now(timezone.utc).isoformat(), via="Slack bot (Send reply)",
                                  campaign_id=meta.get("campaign_id"), lead_id=meta.get("lead_id"))
            if response_url:
                requests.post(response_url, json={
                    "replace_original": "true",
                    "text": f"✅ Reply sent to {lead_email}",
                })
        except Exception as e:
            _sent_replies.discard(dedup_key)
            print(f"[send_reply] Failed: {e}")
            if response_url:
                requests.post(response_url, json={
                    "replace_original": "false",
                    "text": f"❌ Failed to send reply to {lead_email}: {e}",
                })
        return "", 200

    elif action_id == "edit_reply":
        thread_meta = json.dumps({
            "campaign_id": meta.get("campaign_id"),
            "stats_id": meta.get("stats_id"),
            "message_id": meta.get("message_id", ""),
            "lead_id": meta.get("lead_id", ""),
            "eaccount": clean_slack_email(meta.get("eaccount", "")),
            "lead_email": lead_email,
            "subject": meta.get("subject", "Re:"),
            "reply_time": meta.get("reply_time", ""),
            "refetch_thread": True,
        })

        text = (
            f"✏️ *Edit the draft below and reply to this thread to send it.*\n\n"
            f"{meta.get('draft', '')}\n\n"
            f"META: {thread_meta}"
        )

        if not SLACK_BOT_TOKEN:
            err = "SLACK_BOT_TOKEN is not set on the server"
        elif not channel_id or not message_ts:
            err = f"Slack did not send channel/message ids (channel={channel_id!r} ts={message_ts!r})"
        else:
            try:
                res = post_slack_chat(channel_id, message_ts, text)
                err = None if res.get("ok") else res.get("error", "unknown_error")
            except Exception as e:
                err = str(e)

        if err:
            print(f"[edit_reply] FAILED to post draft to thread: {err}")
            if response_url:
                requests.post(response_url, json={
                    "replace_original": "false",
                    "response_type": "ephemeral",
                    "text": f"\u274c Edit & Send failed: {err}. "
                            f"Check SLACK_BOT_TOKEN and invite the bot to this channel.",
                })
            return "", 200

        print(f"[edit_reply] Posted draft to Slack thread for lead={lead_email}. Waiting for user reply.")
        return "", 200

    elif action_id == "dismiss":
        if response_url:
            requests.post(response_url, json={
                "replace_original": "true",
                "text": f"❌ Dismissed reply from {lead_email}",
            })
        return "", 200

    return "", 200


# ============================================================
# ROUTE 3: Slack events (thread replies for edited drafts)
# ============================================================

@app.route("/webhook/slack-events", methods=["POST"])
def slack_events():
    data = request.json or {}

    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]}), 200

    event = data.get("event", {})

    if event.get("bot_id") or not event.get("thread_ts"):
        return "", 200

    thread_ts = event["thread_ts"]
    channel = event["channel"]

    thread = fetch_slack_thread(channel, thread_ts)
    messages = thread.get("messages", [])

    human_messages = [m for m in messages if not m.get("bot_id") and m.get("subtype") != "bot_message" and "META:" not in m.get("text", "")]
    if not human_messages:
        return "", 200
    reply_text = human_messages[-1].get("text", "")

    meta_message = None
    for m in reversed(messages):
        if m.get("text") and "META:" in m["text"]:
            meta_message = m
            break

    if not meta_message:
        return "", 200

    meta_match = re.search(r"META: ({.+})", meta_message["text"])
    if not meta_match:
        return "", 200

    meta = json.loads(meta_match.group(1))
    lead_email = clean_slack_email(meta.get("lead_email", ""))
    stats_id = meta.get("stats_id", "")

    dedup_key = f"{stats_id}:{thread_ts}"
    if dedup_key in _sent_replies:
        print(f"[slack_events] Already sent reply for {dedup_key}. Skipping.")
        return "", 200

    print(f"[slack_events] Sending edited reply. stats_id={stats_id} campaign={meta.get('campaign_id')} lead={lead_email} body_preview={reply_text[:80]}")

    try:
        _sent_replies.add(dedup_key)
        result = _send_from_meta(meta, reply_text)
        print(f"[edit_send] Smartlead response: {result}")
        log_outbound_to_attio(lead_email, clean_slack_email(meta.get("eaccount", "")), reply_text,
                              datetime.now(timezone.utc).isoformat(), via="Slack bot (Edit & send)",
                              campaign_id=meta.get("campaign_id"), lead_id=meta.get("lead_id"))
        post_slack_chat(channel, thread_ts, f"✅ Reply sent to {lead_email}")
    except Exception as e:
        _sent_replies.discard(dedup_key)
        print(f"[edit_send] Failed to send reply: {e}")
        post_slack_chat(channel, thread_ts, f"❌ Failed to send reply: {str(e)}")

    return "", 200


# ============================================================
# Attio connectivity check (read-only): GET /attio/check
# ============================================================

@app.route("/attio/check", methods=["GET"])
def attio_check():
    if not ATTIO_API_KEY:
        return jsonify({"ok": False, "error": "ATTIO_API_KEY is not set"}), 200
    out = {"ok": True, "owner_id_set": bool(ATTIO_OWNER_ID), "deal_stage": ATTIO_DEAL_STAGE}
    try:
        r = requests.get("https://api.attio.com/v2/self", headers=_attio_headers(), timeout=15)
        r.raise_for_status()
        me = r.json()
        out["workspace"] = me.get("workspace_name")
        out["scopes_ok"] = True
    except Exception as e:
        return jsonify({"ok": False, "step": "auth", "error": str(e)[:300]}), 200
    try:
        r = requests.get("https://api.attio.com/v2/objects", headers=_attio_headers(), timeout=15)
        r.raise_for_status()
        slugs = [o.get("api_slug") for o in r.json().get("data", [])]
        out["objects"] = slugs
        out["has_deals_object"] = "deals" in slugs
    except Exception as e:
        out["objects_error"] = str(e)[:300]
    try:
        r = requests.get("https://api.attio.com/v2/objects/deals/attributes/stage/statuses",
                         headers=_attio_headers(), timeout=15)
        r.raise_for_status()
        stages = [s.get("title") for s in r.json().get("data", [])]
        out["deal_stages"] = stages
        out["deal_stage_valid"] = ATTIO_DEAL_STAGE in stages
    except Exception as e:
        out["deal_stages_error"] = str(e)[:300]
    try:
        r = requests.get("https://api.attio.com/v2/workspace_members", headers=_attio_headers(), timeout=15)
        r.raise_for_status()
        out["workspace_members"] = [
            {"id": m.get("id", {}).get("workspace_member_id"), "email": m.get("email_address")}
            for m in r.json().get("data", [])
        ]
    except Exception as e:
        out["members_error"] = str(e)[:300]
    try:
        out["custom_fields"] = {}
        for obj in ATTIO_OBJECTS:
            have = {a.get("api_slug") for a in _attio("GET", f"/objects/{obj}/attributes").get("data", [])}
            out["custom_fields"][obj] = {slug: (slug in have) for slug, _, _, _ in ATTIO_CUSTOM_ATTRS}
        out["reply_status_attribute_all"] = all(all(v.values()) for v in out["custom_fields"].values())
    except Exception as e:
        out["reply_status_error"] = str(e)[:300]
    out["ok"] = (out.get("scopes_ok", False) and out.get("has_deals_object", False)
                 and out.get("deal_stage_valid", False) and out.get("reply_status_attribute_all", False))
    return jsonify(out), 200


@app.route("/attio/lookup", methods=["GET"])
def attio_lookup():
    """Read-only: what Attio holds for a lead email (person, reply status, deals, notes)."""
    if not ATTIO_API_KEY:
        return jsonify({"ok": False, "error": "ATTIO_API_KEY is not set"}), 200
    email = _bare_email(request.args.get("email", ""))
    if not email:
        return jsonify({"ok": False, "error": "pass ?email="}), 200
    out = {"ok": True, "email": email}
    try:
        res = _attio("POST", "/objects/people/records/query", json={
            "filter": {"email_addresses": email}, "limit": 1})
        rows = res.get("data", [])
        if not rows:
            out["person"] = None
            return jsonify(out), 200
        p = rows[0]
        pid = p.get("id", {}).get("record_id")
        vals = p.get("values", {})
        out["person"] = {
            "record_id": pid,
            "name": [v.get("full_name") for v in vals.get("name", [])],
            "reply_status": [v.get("option", {}).get("title") for v in vals.get(REPLY_STATUS_SLUG, [])],
            "first_email_sent_from": [v.get("value") for v in vals.get(FIRST_MAILBOX_SLUG, [])],
            "emails": [v.get("email_address") for v in vals.get("email_addresses", [])],
            "created_at": p.get("created_at"),
        }
        domain = email.split("@", 1)[1]
        try:
            cq = _attio("POST", "/objects/companies/records/query", json={"filter": {"domains": domain}, "limit": 1})
            crow = (cq.get("data") or [None])[0]
            if crow:
                cv = crow.get("values", {})
                out["company"] = {
                    "record_id": crow.get("id", {}).get("record_id"),
                    "name": [v.get("value") for v in cv.get("name", [])],
                    "reply_status": [v.get("option", {}).get("title") for v in cv.get(REPLY_STATUS_SLUG, [])],
                    "first_email_sent_from": [v.get("value") for v in cv.get(FIRST_MAILBOX_SLUG, [])],
                    "hitch_role": [(v.get("option") or {}).get("title") for v in cv.get("hitch_role", [])],
                }
        except Exception as e:
            out["company_error"] = str(e)[:200]
        try:
            q = _attio("POST", "/objects/deals/records/query", json={
                "filter": {"associated_people": {"target_object": "people", "target_record_id": pid}},
                "limit": 5})
            def _dv(d):
                v = d.get("values", {})
                return {"id": d.get("id", {}).get("record_id"),
                        "name": [x.get("value") for x in v.get("name", [])],
                        "stage": [(x.get("status") or {}).get("title") for x in v.get("stage", [])],
                        "source": [(x.get("option") or {}).get("title") for x in v.get("source", [])],
                        "first_reply_date": [x.get("value") for x in v.get("first_reply_date", [])],
                        "mandate": [x.get("target_record_id") for x in v.get("mandate", [])],
                        "buyer": [x.get("target_record_id") for x in v.get("buyer", [])],
                        "campaign_old": [x.get("value") for x in v.get("campaign_old", [])],
                        "reply_status": [(x.get("option") or {}).get("title") for x in v.get(REPLY_STATUS_SLUG, [])],
                        "first_email_sent_from": [x.get("value") for x in v.get(FIRST_MAILBOX_SLUG, [])],
                        "created_at": d.get("created_at")}
            out["deals_linked"] = [_dv(d) for d in q.get("data", [])]
        except requests.HTTPError as e:
            out["deals_linked_error"] = e.response.text[:300] if e.response is not None else str(e)
        try:
            q = _attio("POST", "/objects/deals/records/query", json={
                "filter": {"name": {"$contains": email}}, "limit": 5})
            out["deals_by_name"] = [{"id": d.get("id", {}).get("record_id"),
                                     "name": [v.get("value") for v in d.get("values", {}).get("name", [])],
                                     "created_at": d.get("created_at")} for d in q.get("data", [])]
        except requests.HTTPError as e:
            out["deals_by_name_error"] = e.response.text[:300] if e.response is not None else str(e)
        notes = _attio("GET", "/notes", params={"parent_object": "people", "parent_record_id": pid, "limit": 20}).get("data", [])
        out["notes"] = [{"title": n.get("title"), "created_at": n.get("created_at")} for n in notes]
        for n in notes:
            if str(n.get("title", "")).startswith(SUMMARY_NOTE_TITLE):
                try:
                    full = _attio("GET", f"/notes/{n['id']['note_id']}").get("data", {})
                    out["summary_note"] = full.get("content_plaintext") or full.get("content") or ""
                except Exception as e:
                    out["summary_note_error"] = str(e)[:200]
                break
    except requests.HTTPError as e:
        out["ok"] = False
        out["error"] = (e.response.text[:300] if e.response is not None else str(e))
    except Exception as e:
        out["ok"] = False
        out["error"] = str(e)[:300]
    return jsonify(out), 200


@app.route("/attio/backfill", methods=["GET", "POST"])
def attio_backfill():
    """
    One-off: rebuild a lead's Attio state from their Smartlead thread.
    Sets Reply status from their latest reply, creates/links the deal if
    Positive, and writes the living Conversation summary note.
    ?email=...&confirm=yes   plus optional &cleanup=yes to delete old per-message
    bot notes ("Inbound reply ..." / "Outbound email ...").
    """
    if not ATTIO_API_KEY or not SMARTLEAD_API_KEY:
        return jsonify({"ok": False, "error": "ATTIO_API_KEY / SMARTLEAD_API_KEY not set"}), 200
    email = _bare_email(request.args.get("email", ""))
    if not email or request.args.get("confirm") != "yes":
        return jsonify({"ok": False, "error": "pass ?email=...&confirm=yes"}), 200
    try:
        lead = requests.get(f"{SMARTLEAD_BASE}/leads/", params={"api_key": SMARTLEAD_API_KEY, "email": email},
                            timeout=20).json()
        lead_id = lead.get("id")
        campaigns = lead.get("lead_campaign_data", [])
        if not lead_id or not campaigns:
            return jsonify({"ok": False, "error": "lead not found in Smartlead"}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": f"smartlead lookup: {e}"}), 200

    results = []
    for camp in campaigns:
        cid, cname = camp.get("campaign_id"), camp.get("campaign_name", "")
        try:
            mailbox, messages = fetch_thread_messages(cid, lead_id)
        except Exception as e:
            results.append({"campaign": cname, "error": str(e)}); continue
        inbound = [m for m in messages if m["dir"] == "inbound"]
        if not inbound:
            results.append({"campaign": cname, "skipped": "no reply from lead"}); continue
        latest = inbound[-1]
        lead_response = latest["text"]
        sentiment = classify_sentiment(lead_response, cname)
        r = sync_to_attio(email, sentiment, cname, lead_response=lead_response, reply_time=latest["time"],
                          sender=mailbox, campaign_id=cid, lead_id=lead_id, reply_from=latest.get("who", ""))
        results.append({"campaign": cname, "messages": len(messages), "replies": len(inbound),
                        "latest_sentiment": sentiment, "attio": r["summary"]})

    removed = 0
    if request.args.get("cleanup") == "yes":
        try:
            q = _attio("POST", "/objects/people/records/query", json={"filter": {"email_addresses": email}, "limit": 1})
            rows = q.get("data", [])
            pid = rows[0]["id"]["record_id"] if rows else None
            targets = [("people", pid)] if pid else []
            did = find_attio_deal_for_person(pid, email) if pid else None
            if did:
                targets.append(("deals", did))
            for obj, rid in targets:
                for n in _attio("GET", "/notes", params={"parent_object": obj, "parent_record_id": rid, "limit": 50}).get("data", []):
                    t = str(n.get("title", ""))
                    if t.startswith("Inbound reply (") or t.startswith("Outbound email from "):
                        _attio("DELETE", f"/notes/{n['id']['note_id']}")
                        removed += 1
        except Exception as e:
            results.append({"cleanup_error": str(e)[:200]})
    return jsonify({"ok": True, "email": email, "events": results, "old_notes_removed": removed}), 200


@app.route("/attio/inspect", methods=["GET"])
def attio_inspect():
    """Read-only: ?object=deals lists attributes (+ options/statuses); &record=<id> dumps one record."""
    if not ATTIO_API_KEY:
        return jsonify({"ok": False, "error": "ATTIO_API_KEY is not set"}), 200
    obj = request.args.get("object", "deals")
    rid = request.args.get("record")
    out = {"ok": True, "object": obj}
    try:
        if rid:
            rec = _attio("GET", f"/objects/{obj}/records/{rid}").get("data", {})
            out["record"] = {"id": rec.get("id"), "values": rec.get("values")}
            return jsonify(out), 200
        if request.args.get("search"):
            term = request.args.get("search")
            comps = _attio("POST", "/objects/companies/records/query",
                           json={"filter": {"name": {"$contains": term}}, "limit": 10}).get("data", [])
            found = []
            for cmp in comps:
                cid = cmp.get("id", {}).get("record_id")
                name = [v.get("value") for v in cmp.get("values", {}).get("name", [])]
                people = _attio("POST", "/objects/people/records/query",
                                json={"filter": {"company": {"target_object": "companies", "target_record_id": cid}},
                                      "limit": 10}).get("data", [])
                found.append({"company_id": cid, "name": name,
                              "domains": [v.get("domain") for v in cmp.get("values", {}).get("domains", [])],
                              "people": [[e.get("email_address") for e in p.get("values", {}).get("email_addresses", [])]
                                         for p in people]})
            out["companies"] = found
            return jsonify(out), 200
        if request.args.get("list"):
            recs = _attio("POST", f"/objects/{obj}/records/query", json={"limit": 50}).get("data", [])
            def _v(x):
                return (x.get("value") or x.get("option", {}).get("title") or x.get("target_record_id")
                        or x.get("domain") or x.get("email_address") or x.get("full_name"))
            out["records"] = [{"id": r.get("id", {}).get("record_id"),
                               **{k: [_v(x) for x in v] for k, v in (r.get("values") or {}).items()
                                  if v and k not in ("created_by",)}} for r in recs]
            return jsonify(out), 200
        if obj == "objects":
            out["objects"] = [{"slug": o.get("api_slug"), "singular": o.get("singular_noun")} for o in
                              _attio("GET", "/objects").get("data", [])]
            return jsonify(out), 200
        attrs = _attio("GET", f"/objects/{obj}/attributes").get("data", [])
        rows = []
        for a in attrs:
            row = {"slug": a.get("api_slug"), "title": a.get("title"), "type": a.get("type"),
                   "multi": a.get("is_multiselect"), "required": a.get("is_required"),
                   "system": a.get("is_system_attribute")}
            if a.get("type") == "record-reference":
                row["targets"] = [t.get("target_object") for t in (a.get("relationship") or {}).get("allowed_record_objects", [])] \
                    if isinstance(a.get("relationship"), dict) else (a.get("config", {}) or {}).get("record_reference", {}).get("allowed_object_ids")
            if a.get("type") == "select":
                try:
                    row["options"] = [o.get("title") for o in _attio("GET", f"/objects/{obj}/attributes/{a['api_slug']}/options").get("data", [])]
                except Exception as e:
                    row["options_error"] = str(e)[:100]
            if a.get("type") == "status":
                try:
                    row["statuses"] = [s.get("title") for s in _attio("GET", f"/objects/{obj}/attributes/{a['api_slug']}/statuses").get("data", [])]
                except Exception as e:
                    row["statuses_error"] = str(e)[:100]
            rows.append(row)
        out["attributes"] = rows
    except requests.HTTPError as e:
        out["ok"] = False; out["error"] = e.response.text[:300] if e.response is not None else str(e)
    except Exception as e:
        out["ok"] = False; out["error"] = str(e)[:300]
    return jsonify(out), 200


@app.route("/attio/setup", methods=["GET", "POST"])
def attio_setup():
    """One-off: create the People 'Reply status' select attribute if missing."""
    if not ATTIO_API_KEY:
        return jsonify({"ok": False, "error": "ATTIO_API_KEY is not set"}), 200
    try:
        res = ensure_reply_status_attribute()
        return jsonify({"ok": True, "created": res["created"], "attribute": REPLY_STATUS_SLUG,
                        "options": list(REPLY_STATUS_OPTIONS)}), 200
    except requests.HTTPError as e:
        body = e.response.text[:300] if e.response is not None else str(e)
        return jsonify({"ok": False, "error": body}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:300]}), 200


# ============================================================
# Health check
# ============================================================

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "email-reply-bot-smartlead"}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
