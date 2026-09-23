import os
import json
import re
import time
from datetime import datetime, timedelta, timezone
import requests
import anthropic
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# --- Config ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ATTIO_API_KEY = os.getenv("ATTIO_API_KEY")
ATTIO_OWNER_ID = os.getenv("ATTIO_OWNER_ID")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
INSTANTLY_API_KEY = os.getenv("INSTANTLY_API_KEY")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL")
CALENDLY_API_KEY = os.getenv("CALENDLY_API_KEY")
# Event type URIs from Calendly (found via GET /event_types)
CALENDLY_O2E_EVENT_TYPE = os.getenv(
    "CALENDLY_O2E_EVENT_TYPE",
    "https://api.calendly.com/event_types/fcf75643-7fb6-4072-b1e0-5dba5ce49c1d",
)
CALENDLY_STATE17_EVENT_TYPE = os.getenv("CALENDLY_STATE17_EVENT_TYPE", "")
# Fallback booking page URLs (used when API fails or event type not configured)
CALENDLY_O2E_URL = os.getenv("CALENDLY_O2E_URL", "https://calendly.com/gdavidson-options2exit/introcall")
CALENDLY_STATE17_URL = os.getenv("CALENDLY_STATE17_URL", "https://calendly.com/team-state17/30min")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Track sent replies to prevent Slack event retries from sending duplicates
_sent_replies = set()  # set of reply_to_uuid values that have already been sent


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

This thread is from an outbound sales campaign. Our brand is either "State17" or "Options2Exit" (also abbreviated O2E). Our sending domains include: state17.com, findstate17.com, options2exit.com, and any email address associated with those brands.

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


def draft_reply(sender_name: str, eaccount: str, lead_email: str,
                campaign_name: str, reply_text: str) -> str:
    """Use Claude to draft an email reply."""
    prompt = """
# SYSTEM PROMPT: Email Reply Engine for State17 & Options2Exit

---

## YOUR ROLE

You are a human sales associate responding to inbound email replies on behalf of two separate brands: **State17** and **Options2Exit (O2E)**. You are NOT an AI assistant. You are a real person having a real email conversation. Every reply you write must sound like it came from a busy, friendly, competent person who works in M&A and home services -- not from a language model.

You will receive:
1. The **campaign name** (tells you which brand this is)
2. The **original outbound email** that was sent (with the sender's signature)
3. The **prospect's reply**
4. Available **prospect metadata** (name, company, title, etc.)

Your job: Read the reply, classify it, and either draft a response, flag it for human review, or take no action.

---

## BRAND IDENTITIES (KEEP THESE SEPARATE -- NEVER MIX)

### State17
- Private family investment office focused on acquiring home service businesses
- Operator-to-operator approach. The founding member scaled and sold his own home service business in 2019. He knows what it's like to sit in the owner's chair
- Family capital, NOT private equity. This is a critical distinction. If a prospect asks "are you PE?" the answer is no. State17 is a family office backed by private capital. There are no fund timelines, no forced exits, no LP pressure
- National footprint. All home service verticals (roofing, HVAC, plumbing, electrical, landscaping, pest control, garage doors, windows & doors, siding, fencing). Primary focus is roofing given the founder's background
- Target: businesses doing $10M+ in revenue, or businesses with strong year-over-year growth approaching $10M
- EBITDA threshold referenced in emails is $1M+
- Privacy is paramount. Never volunteer details about the family, the portfolio, or specific companies they have acquired. If pressed, say something like "happy to walk you through our structure on a call" and move on
- Buy box: $1M+ profitability, relevant home service vertical focus above 70%, no new construction
- Positioning: "We keep your team intact and grow what you have built. We are operators, not financial engineers."

### Options2Exit (O2E)
- Sell-side M&A advisory firm helping home service business owners navigate exits
- Website: www.options2exit.com
- Led by an owner-operator who scaled his construction/roofing company from nothing to $30M in revenue over 7 years before exiting in 2019. Now he advises others through the same process
- Since inception, O2E has closed over $100M in total transactions and has an additional $84M under LOI
- O2E does NOT represent buyers. They work exclusively on behalf of the seller to maximize value
- They focus on process design, buyer selection, and transaction structure. They are not brokers
- Fee structure: they do not charge upfront. They only get paid when a deal closes and value is created for the owner
- For the truly differentiated businesses, they are seeing 6-8x+ EBITDA multiples for outliers. They helped close a business at 9x by creating competitive tension
- Positioning: "I have been where you are. I built and sold my own company. Let me help you get the best outcome."

### KEY RULE: Brand separation
- Never reference State17 in an O2E reply or vice versa
- Never hint that the same person is behind both entities
- Treat them as completely independent organizations in all communications

---

## SENDER IDENTITY

You sign every email as the person whose name appears in the signature of the original outbound email. Pull the name directly from the outbound email signature block. Match their sign-off style.

Examples from the data:
- If the outbound was signed "Best, Stephanie Miller / State17" then you ARE Stephanie Miller
- If it was signed "Griffin Davidson / Options2Exit" then you ARE Griffin Davidson
- If it was signed just "Griffin" then sign as "Griffin"

Match the formality of the original signature. If they used just a first name, use just a first name. If they used full name and title, do the same.

---

## CALENDAR LINKS

Use the correct link based on the campaign:

- **State17 replies:** """ + CALENDLY_STATE17_URL + """
- **Options2Exit replies:** """ + CALENDLY_O2E_URL + """

When dropping a calendar link, keep it casual. Examples:
- "Here is my calendar if you want to grab a time: [link]"
- "Feel free to pick whatever works: [link]"
- "Grab a slot here and we will chat: [link]"

Never say "Please use the following link to schedule." That sounds automated.

---

## REPLY CLASSIFICATION

Read every inbound reply and classify it into ONE of the following categories. Then follow the corresponding action.

### CATEGORY 1: INTERESTED / READY TO BOOK
**Signals:** "sure," "I am interested," "let's talk," "sounds good," "I am free," "yes," "I would be interested," "tell me more," "what is the best way to connect," "when works," "let's schedule a call," "tomorrow works," "call me at [number]," prospect proposes a meeting time, prospect shares their phone number

**Action:** Respond. Keep it short. Get them to the calendar link or confirm a time. Do not re-pitch. They already said yes.

**Response examples:**
- If they say "Sure": "Great, here is my calendar. Grab whatever works best for you: [link]"
- If they propose a time: "That works. I will give you a call then. Looking forward to it."
- If they share a phone number: "Got it. I will call you at [number]. Does [tomorrow/today] work or is there a better day?"
- If they say "tell me more": Give 2-3 sentences of context (not a pitch), then steer to the call. "Happy to walk you through it. Easiest thing would be a quick 15 minute call. Here is my calendar: [link]"

## OUTPUT FORMAT

Output ONLY the email reply text -- nothing else. No headers, no labels, no metadata. The output should be ready to send as-is. Include the greeting, body, sign-off, and signature.

Example of correct output:

Hi Joe,

Happy to send over more info. Easiest thing would be a quick 15 minute call. Here is my calendar: [link]

Best,
John Tanner
State17 Capital Partners

Do NOT include lines like "CAMPAIGN:", "CLASSIFICATION:", "SIGN AS:", "RESPONSE:", or any other labels. Just the email copy.

If the reply should NOT receive a response, output only: NO RESPONSE

---

## FINAL REMINDERS

1. You are a person, not an AI. Write like one.
2. Short is almost always better. When in doubt, write less.
3. The goal is to book a call or route to nurture. That is it.
4. Never argue with a prospect who says no.
5. Never share confidential details about State17's family or portfolio.
6. Keep State17 and O2E completely separate.
7. Match the prospect's energy. Mirror their tone.
8. If something feels off or you are unsure, flag it for human review rather than guessing.
9. When someone is ready to book, get out of their way. Send the link and stop talking.
10. Read the original outbound email carefully. Do not contradict anything that was said in it.
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


def get_calendly_info(email_account: str, campaign_name: str = "") -> dict:
    """Return the correct Calendly event type URI and fallback URL based on domain."""
    combined = (email_account + " " + campaign_name).lower()
    if any(kw in combined for kw in ("options2exit", "o2e")):
        return {"event_type": CALENDLY_O2E_EVENT_TYPE, "fallback_url": CALENDLY_O2E_URL}
    if any(kw in combined for kw in ("state17", "findstate17", "state 17")):
        if CALENDLY_STATE17_EVENT_TYPE:
            return {"event_type": CALENDLY_STATE17_EVENT_TYPE, "fallback_url": CALENDLY_STATE17_URL}
        return {"event_type": CALENDLY_O2E_EVENT_TYPE, "fallback_url": CALENDLY_O2E_URL}
    return {"event_type": CALENDLY_O2E_EVENT_TYPE, "fallback_url": CALENDLY_O2E_URL}


def fetch_available_slots(event_type_uri: str, num_days: int = 3) -> dict:
    """Fetch available time slots from Calendly, grouped by day."""
    if not event_type_uri or not CALENDLY_API_KEY:
        return {}

    now = datetime.now(timezone.utc)
    start = (now + timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (now + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        resp = requests.get(
            "https://api.calendly.com/event_type_available_times",
            headers={"Authorization": f"Bearer {CALENDLY_API_KEY}"},
            params={
                "event_type": event_type_uri,
                "start_time": start,
                "end_time": end,
            },
        )
        resp.raise_for_status()
        all_slots = resp.json().get("collection", [])
    except Exception as e:
        print(f"[calendly] Failed to fetch available times: {e}")
        return {}

    if not all_slots:
        return {}

    days = {}
    for slot in all_slots:
        if slot.get("status") != "available":
            continue
        dt = datetime.fromisoformat(slot["start_time"].replace("Z", "+00:00"))
        et_dt = dt - timedelta(hours=4)
        day_label = f"{et_dt.strftime('%A')}, {et_dt.strftime('%B')} {et_dt.day}"
        time_label = et_dt.strftime("%I:%M %p").lstrip("0").lower()
        if day_label not in days:
            days[day_label] = []
        days[day_label].append({
            "time": time_label,
            "url": slot["scheduling_url"],
        })

    sorted_days = dict(list(days.items())[:num_days])
    total = sum(len(v) for v in sorted_days.values())
    print(f"[calendly] Fetched {total} slots across {len(sorted_days)} days from {len(all_slots)} total available")
    return sorted_days


def format_slots_for_email(slots_by_day: dict, fallback_url: str) -> str:
    """Format grouped slots as a text block for email drafts."""
    if not slots_by_day:
        return f"Book a time that works for you here: {fallback_url}"

    lines = []
    for day_label, times in slots_by_day.items():
        lines.append(f"{day_label}")
        time_strs = [f"{t['time']} - {t['url']}" for t in times]
        lines.append("  " + "  |  ".join(time_strs))
        lines.append("")
    lines.append(f"Don't see a time that works? Pick any open slot here: {fallback_url}")
    return "\n".join(lines)


def format_slots_for_slack(slots_by_day: dict, fallback_url: str) -> str:
    """Format grouped slots as a Slack mrkdwn block."""
    if not slots_by_day:
        return f"<{fallback_url}|Book a time>"

    lines = []
    for day_label, times in slots_by_day.items():
        lines.append(f"*{day_label}*")
        time_strs = [f"<{t['url']}|{t['time']}>" for t in times]
        lines.append("  " + "  |  ".join(time_strs))
    lines.append(f"\n<{fallback_url}|See all available times>")
    return "\n".join(lines)


def extract_sender_name(email_account: str) -> str:
    """Extract first name from email, e.g. john.tanner@x.com -> John"""
    local = email_account.split("@")[0]
    first = local.split(".")[0]
    return first.capitalize()


def upsert_attio_company(domain: str) -> dict:
    resp = requests.put(
        "https://api.attio.com/v2/objects/companies/records",
        params={"matching_attribute": "domains"},
        headers={"Authorization": f"Bearer {ATTIO_API_KEY}", "Content-Type": "application/json"},
        json={"data": {"values": {"domains": [{"domain": domain}]}}},
    )
    resp.raise_for_status()
    return resp.json()


def upsert_attio_person(lead_email: str) -> dict:
    resp = requests.put(
        "https://api.attio.com/v2/objects/people/records",
        params={"matching_attribute": "email_addresses"},
        headers={"Authorization": f"Bearer {ATTIO_API_KEY}", "Content-Type": "application/json"},
        json={"data": {"values": {"email_addresses": [{"email_address": lead_email}]}}},
    )
    resp.raise_for_status()
    return resp.json()


def create_attio_deal(lead_email: str) -> dict:
    resp = requests.post(
        "https://api.attio.com/v2/objects/deals/records",
        headers={"Authorization": f"Bearer {ATTIO_API_KEY}", "Content-Type": "application/json"},
        json={
            "data": {
                "values": {
                    "name": [{"value": f"{lead_email} - Interested"}],
                    "stage": [{"status": "In Progress"}],
                    "owner": [{
                        "referenced_actor_type": "workspace-member",
                        "referenced_actor_id": ATTIO_OWNER_ID,
                    }],
                }
            }
        },
    )
    resp.raise_for_status()
    return resp.json()


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


def fetch_instantly_reply_uuid(campaign_id: str, lead_email: str) -> tuple:
    """
    Hit GET /v2/emails with campaign_id and lead.
    Returns (reply_to_uuid, eaccount, thread_html, wrote_line).
    """
    params = {
        "lead": lead_email,
        "limit": 10,
    }
    if campaign_id:
        params["campaign_id"] = campaign_id

    resp = requests.get(
        "https://api.instantly.ai/api/v2/emails",
        headers={"Authorization": f"Bearer {INSTANTLY_API_KEY}"},
        params=params,
    )
    resp.raise_for_status()
    data = resp.json()

    emails = data.get("items", data) if isinstance(data, dict) else data
    if not emails:
        raise ValueError(
            f"[fetch_uuid] No emails found for lead={lead_email} campaign={campaign_id}"
        )

    uuid = emails[0].get("id")
    eaccount = emails[-1].get("eaccount")
    from_address = emails[0].get("from_address_email", emails[0].get("eaccount", ""))

    body_obj = emails[0].get("body", {})
    if isinstance(body_obj, dict):
        thread_html = body_obj.get("html", "")
        body_text = body_obj.get("text", "")
    else:
        thread_html = str(body_obj) if body_obj else ""
        body_text = ""

    wrote_line = ""
    ts_match = re.search(r'(On\s+\w+,\s+\w+\s+\d+,\s+\d+\s+at\s+\d+:\d+\s*[APap][Mm])', body_text)
    if ts_match:
        wrote_line = f'{ts_match.group(1)} {from_address} wrote:'
        print(f"[fetch_uuid] Extracted wrote_line from body.text: {wrote_line}")
    else:
        print(f"[fetch_uuid] Could not extract timestamp from body.text, will skip wrote line")

    print(f"[fetch_uuid] reply_to_uuid={uuid} eaccount={eaccount} thread_html_len={len(thread_html)} for {lead_email}")
    return uuid, eaccount, thread_html, wrote_line


def send_instantly_reply(reply_to_uuid: str, eaccount: str, subject: str,
                         body: str, thread_html: str = "",
                         wrote_line: str = "") -> dict:
    """Send a reply via Instantly with proper HTML threading."""
    # Convert markdown autolinks <https://...> into real anchor tags
    # so they don't get stripped as unknown HTML tags.
    body = re.sub(
        r'<(https?://[^>\s]+)>',
        r'<a href="\1">\1</a>',
        body,
    )

    html_body = body.replace("\n", "<br>")
    full_html = f"<div>{html_body}</div>"
    if thread_html:
        if wrote_line:
            full_html += (
                f'<br><div class="gmail_quote">'
                f'<div dir="ltr" class="gmail_attr">{wrote_line}<br></div>'
                f'<blockquote class="gmail_quote" style="margin:0px 0px 0px 0.8ex;border-left:1px solid rgb(204,204,204);padding-left:1ex">'
                f'{thread_html}'
                f'</blockquote></div>'
            )
        else:
            full_html += f"<br>{thread_html}"

    payload = {
        "reply_to_uuid": reply_to_uuid,
        "eaccount": eaccount,
        "subject": subject,
        "body": {"html": full_html},
    }
    print(f"[send_reply] Payload: {json.dumps(payload)[:500]}")
    resp = requests.post(
        "https://api.instantly.ai/api/v2/emails/reply",
        headers={
            "Authorization": f"Bearer {INSTANTLY_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    print(f"[send_reply] Instantly status={resp.status_code} body={resp.text[:300]}")
    resp.raise_for_status()
    return resp.json()


# ============================================================
# ROUTE 1: Incoming email reply webhook (from Instantly)
# ============================================================

@app.route("/webhook/incoming", methods=["POST"])
def incoming_reply():
    data = request.json
    body = data.get("body", data)

    lead_email = str(body.get("lead_email", ""))

    reply_snippet = body.get("reply_text_snippet", "")
    reply_text = body.get("reply_text", "")
    campaign_name = body.get("campaign_name", "")
    campaign_id = body.get("campaign_id", "")

    subject = body.get("reply_subject") or body.get("subject") or "Re:"
    domain = lead_email.split("@")[1] if "@" in lead_email else ""

    # Extract clean lead response for Slack display
    lead_response = extract_lead_response(reply_text, reply_snippet, campaign_name)

    # Step 1: Resolve the reply_to_uuid + thread HTML from Instantly
    try:
        reply_to_uuid, eaccount, thread_html, wrote_line = fetch_instantly_reply_uuid(campaign_id, lead_email)
    except ValueError:
        print(f"[incoming] No emails found on first try for {lead_email}. Retrying in 3s...")
        time.sleep(3)
        try:
            reply_to_uuid, eaccount, thread_html, wrote_line = fetch_instantly_reply_uuid(campaign_id, lead_email)
        except ValueError as e:
            print(f"[incoming] Still no emails after retry for {lead_email}. Skipping. Error: {e}")
            return jsonify({"status": "skipped", "reason": "no_emails_found"}), 200

    # Step 2: Extract sender name
    sender_name = extract_sender_name(eaccount)

    # Step 3: Upsert Attio company + person + deal
    upsert_attio_company(domain)
    upsert_attio_person(lead_email)
    deal = create_attio_deal(lead_email)
    deal_id = deal["data"]["id"]["record_id"]

    # Step 4: Fetch available Calendly slots and draft reply with Claude
    cal_info = get_calendly_info(eaccount, campaign_name)
    available_slots = fetch_available_slots(cal_info["event_type"])
    scheduling_block = format_slots_for_email(available_slots, cal_info["fallback_url"])
    slack_slots_text = format_slots_for_slack(available_slots, cal_info["fallback_url"])
    draft = draft_reply(sender_name, eaccount, lead_email, campaign_name, reply_text)
    print(f"[draft] Generated {len(draft)} chars for {lead_email} ({len(available_slots)} days of slots fetched)")

    # Step 5: Build Slack message with action buttons
    meta_send = json.dumps({
        "reply_to_uuid": reply_to_uuid,
        "eaccount": eaccount,
        "subject": subject,
        "lead_email": lead_email,
        "deal_id": deal_id,
        "draft": draft,
        "thread_html": thread_html,
        "wrote_line": wrote_line,
    })
    meta_edit = json.dumps({
        "reply_to_uuid": reply_to_uuid,
        "eaccount": eaccount,
        "subject": subject,
        "lead_email": lead_email,
        "deal_id": deal_id,
        "draft": draft,
        "campaign_id": campaign_id,
        "thread_html": thread_html,
    })
    meta_dismiss = json.dumps({
        "deal_id": deal_id,
        "lead_email": lead_email,
    })

    if len(meta_send) > 1900 or len(meta_edit) > 1900:
        print(f"[warn] Meta payload too large for Slack buttons (send={len(meta_send)}, edit={len(meta_edit)}). Dropping thread_html from button meta.")
        meta_send = json.dumps({
            "reply_to_uuid": reply_to_uuid,
            "eaccount": eaccount,
            "subject": subject,
            "lead_email": lead_email,
            "deal_id": deal_id,
            "draft": draft,
            "campaign_id": campaign_id,
            "refetch_thread": True,
        })
        meta_edit = json.dumps({
            "reply_to_uuid": reply_to_uuid,
            "eaccount": eaccount,
            "subject": subject,
            "lead_email": lead_email,
            "deal_id": deal_id,
            "draft": draft,
            "campaign_id": campaign_id,
            "refetch_thread": True,
        })

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f"\U0001f514 *New Interested Reply*\n"
            f"*Campaign:* {campaign_name}\n"
            f"*Sender:* {eaccount}\n"
            f"*Lead:* {lead_email}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f"*Lead's Reply:*\n{lead_response}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f"*Available Time Slots:*\n{slack_slots_text}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f"*AI Draft:*\n{draft}"}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "\u2705 Send Reply"},
             "style": "primary", "action_id": "send_reply", "value": meta_send},
            {"type": "button", "text": {"type": "plain_text", "text": "\u270f\ufe0f Edit & Send"},
             "action_id": "edit_reply", "value": meta_edit},
            {"type": "button", "text": {"type": "plain_text", "text": "\u274c Dismiss"},
             "style": "danger", "action_id": "dismiss", "value": meta_dismiss},
        ]},
    ]

    send_slack_message(blocks)

    return jsonify({"status": "interested", "deal_id": deal_id}), 200


# ============================================================
# ROUTE 2: Slack interactive actions (button clicks)
# ============================================================

@app.route("/webhook/slack-actions", methods=["POST"])
def slack_actions():
    payload = json.loads(request.form.get("payload", "{}"))
    action = payload.get("actions", [{}])[0]
    action_id = action.get("action_id", "")
    meta = json.loads(action.get("value", "{}"))
    print(f"[slack_action] action_id={action_id} reply_to_uuid={meta.get('reply_to_uuid')} eaccount={meta.get('eaccount')} lead={meta.get('lead_email')}")

    channel_id = payload.get("container", {}).get("channel_id", "")
    message_ts = payload.get("container", {}).get("message_ts", "")

    if action_id == "send_reply":
        draft = meta.get("draft", "")
        thread_html = meta.get("thread_html", "")
        wrote_line = meta.get("wrote_line", "")

        if meta.get("refetch_thread") and meta.get("campaign_id"):
            try:
                _, _, thread_html, wrote_line = fetch_instantly_reply_uuid(
                    meta["campaign_id"], clean_slack_email(meta.get("lead_email", ""))
                )
            except Exception as e:
                print(f"[send_reply] Failed to refetch thread_html: {e}")

        eaccount = clean_slack_email(meta.get("eaccount", ""))
        lead_email = clean_slack_email(meta.get("lead_email", ""))
        reply_uuid = meta.get("reply_to_uuid", "")

        dedup_key = f"{reply_uuid}:send"
        if dedup_key in _sent_replies:
            print(f"[send_reply] Already sent for {dedup_key}. Skipping.")
            return "", 200
        _sent_replies.add(dedup_key)

        print(f"[slack_action] send_reply triggered. reply_to_uuid={reply_uuid} eaccount={eaccount} lead={lead_email}")
        if meta.get("reply_to_uuid") and eaccount:
            result = send_instantly_reply(
                reply_to_uuid=meta["reply_to_uuid"],
                eaccount=eaccount,
                subject=meta.get("subject", "Re:"),
                body=draft,
                thread_html=thread_html,
                wrote_line=wrote_line,
            )
            print(f"[send_reply] Instantly response: {result}")
        else:
            print(f"[send_reply] SKIPPED -- missing reply_to_uuid or eaccount. meta keys: {list(meta.keys())}")

        response_url = payload.get("response_url")
        if response_url:
            requests.post(response_url, json={
                "replace_original": "true",
                "text": f"\u2705 Reply sent to {lead_email}",
            })

        return "", 200

    elif action_id == "edit_reply":
        eaccount = clean_slack_email(meta.get("eaccount", ""))
        lead_email = clean_slack_email(meta.get("lead_email", ""))

        thread_meta = json.dumps({
            "reply_to_uuid": meta.get("reply_to_uuid"),
            "eaccount": eaccount,
            "subject": meta.get("subject", "Re:"),
            "lead_email": lead_email,
            "deal_id": meta.get("deal_id"),
            "campaign_id": meta.get("campaign_id", ""),
        })

        text = (
            f"\u270f\ufe0f *Edit the draft below and reply to this thread to send it.*\n\n"
            f"{meta.get('draft', '')}\n\n"
            f"META: {thread_meta}"
        )

        post_slack_chat(channel_id, message_ts, text)

        print(f"[edit_reply] Posted draft to Slack thread for lead={lead_email}. Waiting for user reply.")
        return "", 200

    elif action_id == "dismiss":
        lead_email = clean_slack_email(meta.get("lead_email", ""))
        response_url = payload.get("response_url")
        if response_url:
            requests.post(response_url, json={
                "replace_original": "true",
                "text": f"\u274c Dismissed reply from {lead_email}",
            })
        return "", 200

    return "", 200


# ============================================================
# ROUTE 3: Slack events (thread replies for edited drafts)
# ============================================================

@app.route("/webhook/slack-events", methods=["POST"])
def slack_events():
    data = request.json

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

    eaccount = clean_slack_email(meta.get("eaccount", ""))
    lead_email = clean_slack_email(meta.get("lead_email", ""))
    reply_uuid = meta.get("reply_to_uuid", "")

    dedup_key = f"{reply_uuid}:{thread_ts}"
    if dedup_key in _sent_replies:
        print(f"[slack_events] Already sent reply for {dedup_key}. Skipping.")
        return "", 200

    print(f"[slack_events] Sending edited reply directly. reply_to_uuid={reply_uuid} eaccount={eaccount} lead={lead_email} body_preview={reply_text[:80]}")

    try:
        _sent_replies.add(dedup_key)
        thread_html = ""
        wrote_line = ""
        campaign_id = meta.get("campaign_id", "")
        if campaign_id and lead_email:
            try:
                _, _, thread_html, wrote_line = fetch_instantly_reply_uuid(campaign_id, lead_email)
            except Exception as e:
                print(f"[slack_events] Could not fetch thread_html: {e}")

        result = send_instantly_reply(
            reply_to_uuid=meta.get("reply_to_uuid", ""),
            eaccount=eaccount,
            subject=meta.get("subject", "Re:"),
            body=reply_text,
            thread_html=thread_html,
            wrote_line=wrote_line,
        )
        print(f"[edit_send] Instantly response: {result}")

        post_slack_chat(channel, thread_ts, f"\u2705 Reply sent to {lead_email}")

    except Exception as e:
        print(f"[edit_send] Failed to send reply: {e}")
        post_slack_chat(channel, thread_ts, f"\u274c Failed to send reply: {str(e)}")

    return "", 200


# ============================================================
# Health check
# ============================================================

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "email-reply-bot"}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
