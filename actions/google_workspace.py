"""Google Workspace integration for JARVIS.

Provides Gmail, Google Calendar, and Google Contacts access through one local
OAuth connection. Credentials stay on the user's machine under config/ and are
never intended to be committed to git.

Expected OAuth desktop client file:
    config/google_client_secret.json
Generated token:
    config/google_token.json
"""

from __future__ import annotations

import base64
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Iterable


SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/contacts.readonly",
]


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()
CONFIG_DIR = BASE_DIR / "config"
CLIENT_SECRET_PATH = CONFIG_DIR / "google_client_secret.json"
TOKEN_PATH = CONFIG_DIR / "google_token.json"


class GoogleWorkspaceSetupError(RuntimeError):
    pass


def _require_google_libs() -> None:
    try:
        import google.auth  # noqa: F401
        import googleapiclient.discovery  # noqa: F401
        import google_auth_oauthlib.flow  # noqa: F401
    except ImportError as exc:
        raise GoogleWorkspaceSetupError(
            "Google Workspace libraries are missing. Run: pip install "
            "google-api-python-client google-auth-httplib2 google-auth-oauthlib"
        ) from exc


def _get_credentials():
    _require_google_libs()
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    creds = None

    if TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        except Exception:
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        except Exception:
            creds = None

    if creds and creds.valid and creds.has_scopes(SCOPES):
        return creds

    if not CLIENT_SECRET_PATH.exists():
        raise GoogleWorkspaceSetupError(
            "Google Workspace is not connected yet. Create a Google OAuth Desktop app, "
            "download its JSON credentials, and save the file as "
            f"{CLIENT_SECRET_PATH}. Then ask me to connect Google again."
        )

    # A local OAuth browser flow is ideal for a desktop assistant. The refresh
    # token is stored locally so future starts do not require another login.
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True, prompt="consent")
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _service(api: str, version: str):
    from googleapiclient.discovery import build

    return build(api, version, credentials=_get_credentials(), cache_discovery=False)


def connect_google() -> str:
    """Starts OAuth if needed and confirms the connected Gmail account."""
    gmail = _service("gmail", "v1")
    profile = gmail.users().getProfile(userId="me").execute()
    email = profile.get("emailAddress", "Google account")
    return f"Google Workspace connected successfully as {email}."


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------


def _decode_mime_header(value: str) -> str:
    if not value:
        return ""
    chunks = []
    for part, encoding in decode_header(value):
        if isinstance(part, bytes):
            try:
                chunks.append(part.decode(encoding or "utf-8", errors="replace"))
            except LookupError:
                chunks.append(part.decode("utf-8", errors="replace"))
        else:
            chunks.append(part)
    return "".join(chunks)


def _headers(payload: dict) -> dict[str, str]:
    result: dict[str, str] = {}
    for h in payload.get("headers", []) or []:
        name = (h.get("name") or "").lower()
        if name:
            result[name] = _decode_mime_header(h.get("value", ""))
    return result


def _decode_b64url(data: str) -> str:
    if not data:
        return ""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_body(payload: dict) -> str:
    """Prefer text/plain; fall back to stripped HTML."""
    plain_parts: list[str] = []
    html_parts: list[str] = []

    def walk(part: dict) -> None:
        mime = (part.get("mimeType") or "").lower()
        body_data = (part.get("body") or {}).get("data")
        if body_data:
            text = _decode_b64url(body_data)
            if mime == "text/plain":
                plain_parts.append(text)
            elif mime == "text/html":
                html_parts.append(text)
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    text = "\n".join(x.strip() for x in plain_parts if x.strip()).strip()
    if text:
        return text

    html = "\n".join(x for x in html_parts if x.strip()).strip()
    if html:
        try:
            from bs4 import BeautifulSoup

            return BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
        except Exception:
            return re.sub(r"<[^>]+>", " ", html)
    return ""


def _message_summary(msg: dict) -> dict:
    payload = msg.get("payload", {}) or {}
    hdr = _headers(payload)
    return {
        "id": msg.get("id", ""),
        "thread_id": msg.get("threadId", ""),
        "from": hdr.get("from", ""),
        "to": hdr.get("to", ""),
        "subject": hdr.get("subject", "(no subject)"),
        "date": hdr.get("date", ""),
        "message_id": hdr.get("message-id", ""),
        "snippet": (msg.get("snippet") or "").strip(),
        "labels": msg.get("labelIds", []) or [],
    }


def gmail_list(query: str = "in:inbox", max_results: int = 10) -> str:
    gmail = _service("gmail", "v1")
    resp = gmail.users().messages().list(
        userId="me",
        q=query or "in:inbox",
        maxResults=max(1, min(int(max_results), 20)),
    ).execute()
    refs = resp.get("messages", []) or []
    if not refs:
        return f"No Gmail messages found for query: {query}"

    lines = [f"Gmail results ({len(refs)}):"]
    for idx, ref in enumerate(refs, 1):
        msg = gmail.users().messages().get(
            userId="me",
            id=ref["id"],
            format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"],
        ).execute()
        info = _message_summary(msg)
        unread = "UNREAD" if "UNREAD" in info["labels"] else "read"
        lines.extend([
            f"{idx}. [{unread}] {info['subject']}",
            f"   From: {info['from']}",
            f"   Date: {info['date']}",
            f"   ID: {info['id']}",
            f"   Preview: {info['snippet'][:220]}",
        ])
    return "\n".join(lines)


def gmail_read(message_id: str, mark_read: bool = True) -> str:
    gmail = _service("gmail", "v1")
    msg = gmail.users().messages().get(userId="me", id=message_id, format="full").execute()
    info = _message_summary(msg)
    body = _extract_body(msg.get("payload", {}) or {})

    if mark_read and "UNREAD" in info["labels"]:
        gmail.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()

    body = body.strip() or info["snippet"] or "(No readable message body.)"
    if len(body) > 12000:
        body = body[:12000] + "\n[Message truncated by JARVIS]"

    return "\n".join([
        f"From: {info['from']}",
        f"To: {info['to']}",
        f"Subject: {info['subject']}",
        f"Date: {info['date']}",
        f"Message ID: {info['id']}",
        "",
        body,
    ])


def _resolve_contact_email(receiver: str) -> str:
    receiver = receiver.strip()
    if "@" in receiver:
        return receiver
    matches = contacts_search(receiver, max_results=5, structured=True)
    if not matches:
        raise ValueError(f"No Google Contact with an email address matched '{receiver}'.")
    # Prefer exact name match when possible.
    exact = [m for m in matches if m.get("name", "").lower() == receiver.lower()]
    chosen = (exact or matches)[0]
    email = chosen.get("email", "")
    if not email:
        raise ValueError(f"Contact '{chosen.get('name', receiver)}' has no email address.")
    return email


def gmail_send(to: str, subject: str, body: str, *, thread_id: str | None = None,
               in_reply_to: str = "", references: str = "") -> str:
    gmail = _service("gmail", "v1")
    to_addr = _resolve_contact_email(to)

    message = EmailMessage()
    message["To"] = to_addr
    message["Subject"] = subject.strip() or "Message from JARVIS"
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
    message.set_content(body)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    request_body: dict = {"raw": raw}
    if thread_id:
        request_body["threadId"] = thread_id

    sent = gmail.users().messages().send(userId="me", body=request_body).execute()
    return f"Email sent to {to_addr}. Gmail message ID: {sent.get('id', '')}"


def gmail_reply(message_id: str, body: str) -> str:
    gmail = _service("gmail", "v1")
    original = gmail.users().messages().get(userId="me", id=message_id, format="full").execute()
    info = _message_summary(original)
    sender_email = parseaddr(info["from"])[1] or info["from"]
    subject = info["subject"]
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    original_mid = info.get("message_id", "")
    refs = original_mid
    return gmail_send(
        sender_email,
        subject,
        body,
        thread_id=info.get("thread_id") or None,
        in_reply_to=original_mid,
        references=refs,
    )


def gmail_from_send_message(receiver: str, message_text: str) -> str:
    """Adapter for Mark-L's existing send_message tool.

    Supports message text in either form:
        Subject: My subject\n\nBody text
    or plain body text, in which case a generic subject is used.
    """
    text = (message_text or "").strip()
    subject = "Message from JARVIS"
    body = text

    m = re.match(r"^subject\s*:\s*(.+?)(?:\r?\n){1,2}(.*)$", text, flags=re.I | re.S)
    if m:
        subject = m.group(1).strip()
        body = m.group(2).strip()

    if not body:
        raise ValueError("Email body is empty.")
    return gmail_send(receiver, subject, body)


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


def _format_event(ev: dict, idx: int | None = None) -> str:
    start = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date") or ""
    end = (ev.get("end") or {}).get("dateTime") or (ev.get("end") or {}).get("date") or ""
    prefix = f"{idx}. " if idx is not None else ""
    lines = [
        f"{prefix}{ev.get('summary', '(untitled event)')}",
        f"   Start: {start}",
        f"   End: {end}",
        f"   Event ID: {ev.get('id', '')}",
    ]
    if ev.get("location"):
        lines.append(f"   Location: {ev['location']}")
    attendees = [a.get("email", "") for a in ev.get("attendees", []) or [] if a.get("email")]
    if attendees:
        lines.append(f"   Attendees: {', '.join(attendees)}")
    return "\n".join(lines)


def calendar_list(days: int = 7, today_only: bool = False, max_results: int = 20) -> str:
    cal = _service("calendar", "v3")
    now_local = datetime.now().astimezone()
    if today_only:
        start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    else:
        start = now_local
        end = start + timedelta(days=max(1, min(int(days), 60)))

    events = cal.events().list(
        calendarId="primary",
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        maxResults=max(1, min(int(max_results), 50)),
        singleEvents=True,
        orderBy="startTime",
    ).execute().get("items", []) or []

    if not events:
        scope = "today" if today_only else f"the next {days} day(s)"
        return f"No calendar events found for {scope}."
    return "Calendar events:\n" + "\n".join(_format_event(e, i) for i, e in enumerate(events, 1))


def calendar_create(title: str, start_iso: str, end_iso: str, location: str = "",
                    description: str = "", attendees: Iterable[str] | None = None) -> str:
    cal = _service("calendar", "v3")
    if not title or not start_iso or not end_iso:
        raise ValueError("Calendar create requires title, start time, and end time.")

    resource: dict = {
        "summary": title,
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
    }
    if location:
        resource["location"] = location
    if description:
        resource["description"] = description
    attendee_list = [x.strip() for x in (attendees or []) if x and x.strip()]
    if attendee_list:
        resource["attendees"] = [{"email": _resolve_contact_email(x)} for x in attendee_list]

    event = cal.events().insert(calendarId="primary", body=resource, sendUpdates="all").execute()
    return "Calendar event created.\n" + _format_event(event)


def calendar_delete(event_id: str) -> str:
    if not event_id:
        raise ValueError("Calendar delete requires an event ID.")
    cal = _service("calendar", "v3")
    cal.events().delete(calendarId="primary", eventId=event_id, sendUpdates="all").execute()
    return f"Calendar event deleted: {event_id}"


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


def contacts_search(query: str, max_results: int = 10, structured: bool = False):
    people = _service("people", "v1")
    resp = people.people().connections().list(
        resourceName="people/me",
        pageSize=500,
        personFields="names,emailAddresses,phoneNumbers,organizations",
        sortOrder="FIRST_NAME_ASCENDING",
    ).execute()

    needle = (query or "").strip().lower()
    matches: list[dict] = []
    for p in resp.get("connections", []) or []:
        names = p.get("names", []) or []
        emails = p.get("emailAddresses", []) or []
        phones = p.get("phoneNumbers", []) or []
        orgs = p.get("organizations", []) or []
        name = (names[0].get("displayName") if names else "") or ""
        email = (emails[0].get("value") if emails else "") or ""
        phone = (phones[0].get("value") if phones else "") or ""
        org = (orgs[0].get("name") if orgs else "") or ""
        haystack = " ".join([name, email, phone, org]).lower()
        if not needle or needle in haystack:
            matches.append({"name": name, "email": email, "phone": phone, "organization": org})
        if len(matches) >= max(1, min(int(max_results), 25)):
            break

    if structured:
        return matches
    if not matches:
        return f"No Google Contacts matched: {query}"

    lines = [f"Contacts matching '{query}':"]
    for idx, c in enumerate(matches, 1):
        lines.append(f"{idx}. {c['name'] or '(unnamed contact)'}")
        if c["email"]:
            lines.append(f"   Email: {c['email']}")
        if c["phone"]:
            lines.append(f"   Phone: {c['phone']}")
        if c["organization"]:
            lines.append(f"   Organization: {c['organization']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Adapter used by the existing web_search tool
# ---------------------------------------------------------------------------


def google_workspace(mode: str, query: str, player=None) -> str:
    """Route workspace commands through Mark-L's existing web_search tool.

    This avoids a giant main.py rewrite while still exposing first-class workspace
    behavior to the Gemini Live session. The system prompt teaches the model the
    compact command syntax below.
    """
    mode = (mode or "").strip().lower()
    query = (query or "").strip()
    low = query.lower()

    if player:
        try:
            player.write_log(f"[Google:{mode}] {query}")
        except Exception:
            pass

    if low in {"connect", "status", "connect google", "google connect"}:
        return connect_google()

    if mode == "gmail":
        if low in {"unread", "unread inbox", "check unread", "check inbox"}:
            return gmail_list("is:unread in:inbox", 10)
        if low in {"inbox", "latest", "recent"}:
            return gmail_list("in:inbox", 10)
        if low.startswith("read "):
            return gmail_read(query[5:].strip(), mark_read=True)
        if low.startswith("search "):
            return gmail_list(query[7:].strip(), 10)
        if low.startswith("reply "):
            # reply <message_id> | <body>
            parts = query[6:].split("|", 1)
            if len(parts) != 2:
                raise ValueError("Gmail reply format: reply <message_id> | <body>")
            return gmail_reply(parts[0].strip(), parts[1].strip())
        if low.startswith("send "):
            # send <recipient> | <subject> | <body>
            parts = query[5:].split("|", 2)
            if len(parts) != 3:
                raise ValueError("Gmail send format: send <recipient> | <subject> | <body>")
            return gmail_send(parts[0].strip(), parts[1].strip(), parts[2].strip())
        return gmail_list(query or "in:inbox", 10)

    if mode == "calendar":
        if low in {"today", "today's events", "events today"}:
            return calendar_list(today_only=True)
        if low.startswith("upcoming"):
            m = re.search(r"(\d+)", low)
            days = int(m.group(1)) if m else 7
            return calendar_list(days=days)
        if low.startswith("create "):
            # create | title | start_iso | end_iso | location | description | attendee1,attendee2
            raw = query[7:].strip()
            if raw.startswith("|"):
                raw = raw[1:].strip()
            parts = [p.strip() for p in raw.split("|")]
            if len(parts) < 3:
                raise ValueError(
                    "Calendar create format: create | <title> | <start ISO> | <end ISO> "
                    "| <location optional> | <description optional> | <attendees optional>"
                )
            title, start_iso, end_iso = parts[:3]
            location = parts[3] if len(parts) > 3 else ""
            description = parts[4] if len(parts) > 4 else ""
            attendees = [x.strip() for x in parts[5].split(",")] if len(parts) > 5 and parts[5] else []
            return calendar_create(title, start_iso, end_iso, location, description, attendees)
        if low.startswith("delete "):
            return calendar_delete(query[7:].strip())
        return calendar_list(days=7)

    if mode == "contacts":
        return contacts_search(query, 10)

    raise ValueError(f"Unknown Google Workspace mode: {mode}")
