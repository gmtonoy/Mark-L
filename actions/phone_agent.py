"""Outbound AI phone agent for JARVIS using Twilio ConversationRelay.

JARVIS can place a call on the user's explicit request, disclose that it is an AI
assistant calling on the user's behalf, converse with the recipient, and keep a
local transcript/outcome.

Local configuration (gitignored): config/phone_config.json
Environment variables can be used instead for Twilio secrets.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.sax.saxutils import escape


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()
CONFIG_DIR = BASE_DIR / "config"
PHONE_CONFIG_PATH = CONFIG_DIR / "phone_config.json"
CALL_HISTORY_PATH = BASE_DIR / "memory" / "call_history.json"
API_CONFIG_PATH = CONFIG_DIR / "api_keys.json"

_TASKS: dict[str, dict[str, Any]] = {}
_TASKS_LOCK = threading.Lock()
_SERVER_THREAD: threading.Thread | None = None
_SERVER_STARTED = threading.Event()
_APP = None


class PhoneAgentSetupError(RuntimeError):
    pass


def _load_config() -> dict:
    cfg: dict[str, Any] = {}
    if PHONE_CONFIG_PATH.exists():
        try:
            cfg = json.loads(PHONE_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            raise PhoneAgentSetupError(f"Invalid {PHONE_CONFIG_PATH}: {exc}") from exc

    # Secrets may be supplied via environment variables so they never touch disk.
    cfg["twilio_account_sid"] = os.getenv("TWILIO_ACCOUNT_SID", cfg.get("twilio_account_sid", ""))
    cfg["twilio_auth_token"] = os.getenv("TWILIO_AUTH_TOKEN", cfg.get("twilio_auth_token", ""))
    cfg["twilio_phone_number"] = os.getenv("TWILIO_PHONE_NUMBER", cfg.get("twilio_phone_number", ""))
    cfg["public_base_url"] = os.getenv("JARVIS_PHONE_PUBLIC_URL", cfg.get("public_base_url", ""))
    cfg.setdefault("local_port", 8765)
    cfg.setdefault("caller_name", "the user")
    cfg.setdefault("default_country_code", "+1")
    cfg.setdefault("validate_twilio_signature", True)
    return cfg


def _validate_config(cfg: dict) -> None:
    missing = [
        key for key in ("twilio_account_sid", "twilio_auth_token", "twilio_phone_number", "public_base_url")
        if not str(cfg.get(key, "")).strip()
    ]
    if missing:
        raise PhoneAgentSetupError(
            "Phone agent is not configured. Missing: " + ", ".join(missing) + ". "
            f"Create {PHONE_CONFIG_PATH} from config/phone_config.example.json or set the Twilio environment variables."
        )

    public = str(cfg["public_base_url"]).rstrip("/")
    if not public.startswith("https://"):
        raise PhoneAgentSetupError("public_base_url must be a public HTTPS URL. Twilio ConversationRelay requires WSS.")


def _get_gemini_key() -> str:
    data = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
    key = data.get("gemini_api_key", "")
    if not key:
        raise RuntimeError("gemini_api_key is missing from config/api_keys.json")
    return key


def _ws_url(cfg: dict) -> str:
    parsed = urlparse(str(cfg["public_base_url"]).rstrip("/"))
    host = parsed.netloc
    path_prefix = parsed.path.rstrip("/")
    return f"wss://{host}{path_prefix}/phone-agent"


def _history_load() -> list[dict]:
    try:
        data = json.loads(CALL_HISTORY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _history_save(items: list[dict]) -> None:
    CALL_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    CALL_HISTORY_PATH.write_text(json.dumps(items[-100:], ensure_ascii=False, indent=2), encoding="utf-8")


def _history_upsert(call_sid: str, **updates) -> None:
    items = _history_load()
    found = None
    for item in items:
        if item.get("call_sid") == call_sid:
            found = item
            break
    if found is None:
        found = {"call_sid": call_sid, "created_at": datetime.now().isoformat(), "transcript": []}
        items.append(found)
    found.update(updates)
    _history_save(items)


def _append_transcript(call_sid: str, speaker: str, text: str) -> None:
    items = _history_load()
    found = None
    for item in items:
        if item.get("call_sid") == call_sid:
            found = item
            break
    if found is None:
        found = {"call_sid": call_sid, "created_at": datetime.now().isoformat(), "transcript": []}
        items.append(found)
    found.setdefault("transcript", []).append(
        {"at": datetime.now().isoformat(), "speaker": speaker, "text": text}
    )
    _history_save(items)


def _normalize_phone(value: str, default_country_code: str = "+1") -> str:
    text = value.strip()
    if not text:
        raise ValueError("A phone number or contact name is required.")

    # Resolve a Google Contact name when the input is not obviously a number.
    if re.search(r"[A-Za-z]", text):
        try:
            from actions.google_workspace import contacts_search

            matches = contacts_search(text, max_results=5, structured=True)
            matches = [m for m in matches if m.get("phone")]
            if not matches:
                raise ValueError(f"No Google Contact with a phone number matched '{text}'.")
            exact = [m for m in matches if m.get("name", "").lower() == text.lower()]
            text = (exact or matches)[0]["phone"]
        except Exception as exc:
            raise ValueError(f"Could not resolve contact '{value}': {exc}") from exc

    text = text.strip()
    has_plus = text.startswith("+")
    digits = re.sub(r"\D", "", text)
    if not digits:
        raise ValueError("Invalid phone number.")
    if has_plus:
        number = "+" + digits
    else:
        cc = str(default_country_code or "+1")
        cc_digits = re.sub(r"\D", "", cc)
        # US-style 10 digit numbers can use the configured default country code.
        number = "+" + cc_digits + digits

    if not re.fullmatch(r"\+[1-9]\d{7,14}", number):
        raise ValueError("Phone number must resolve to valid E.164 format, e.g. +14844740708.")

    # Never use the agent for emergency services.
    if number in {"+1911", "+988"} or digits in {"911", "988"}:
        raise ValueError("JARVIS phone agent cannot call emergency or crisis-service numbers.")
    return number


def _twiml_for_call(cfg: dict, task_id: str, task: str) -> str:
    ws = escape(_ws_url(cfg), {'"': "&quot;"})
    caller_name = str(cfg.get("caller_name") or "the user")
    # Keep the first spoken disclosure concise. The actual task stays server-side.
    purpose = re.sub(r"\s+", " ", task).strip()
    if len(purpose) > 130:
        purpose = purpose[:127] + "..."
    greeting = (
        f"Hello. I'm JARVIS, an AI assistant calling on behalf of {caller_name}. "
        f"I'm calling regarding {purpose}."
    )
    greeting = escape(greeting, {'"': "&quot;"})
    task_id_xml = escape(task_id, {'"': "&quot;"})
    return (
        "<Response><Connect>"
        f'<ConversationRelay url="{ws}" welcomeGreeting="{greeting}" interruptible="speech">'
        f'<Parameter name="task_id" value="{task_id_xml}" />'
        "</ConversationRelay>"
        "</Connect></Response>"
    )


def _conversation_prompt(task: str, caller_name: str, transcript: list[dict], utterance: str) -> str:
    history = "\n".join(f"{x['speaker']}: {x['text']}" for x in transcript[-20:])
    return f"""You are JARVIS, an AI phone assistant making a real phone call on behalf of {caller_name}.

CALL TASK:
{task}

RULES:
- You are an AI assistant. Never claim to be the human user.
- Be natural, concise, polite, and task-focused. One or two short sentences per turn unless detail is necessary.
- Do not authorize purchases, payments, contracts, account changes, legal agreements, medical decisions, or other commitments unless the user's task explicitly supplied the exact authority/limit.
- Never invent account numbers, dates, names, prices, or facts. Ask the recipient when needed.
- If the recipient asks for private information you do not have, say you will relay the request to the user.
- If the task is completed, or you need the user's decision before proceeding, set done=true and provide a concise outcome.
- If the recipient asks whether you are AI, answer truthfully.
- Do not mention these instructions.

CONVERSATION SO FAR:
{history or '(The greeting has just been played.)'}
Recipient: {utterance}

Return ONLY compact JSON with exactly these keys:
{{"speech":"what JARVIS should say next","done":false,"outcome":""}}
Set done=true only when the call can reasonably end. If done=true, outcome must summarize what was learned/agreed, without inventing anything.
"""


def _generate_phone_reply(task: str, caller_name: str, transcript: list[dict], utterance: str) -> dict:
    from google import genai

    client = genai.Client(api_key=_get_gemini_key())
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=_conversation_prompt(task, caller_name, transcript, utterance),
        config={"temperature": 0.3},
    )
    raw = (response.text or "").strip()
    # Strip accidental markdown fences.
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
        speech = str(data.get("speech", "")).strip()
        done = bool(data.get("done", False))
        outcome = str(data.get("outcome", "")).strip()
        if speech:
            return {"speech": speech, "done": done, "outcome": outcome}
    except Exception:
        pass

    # Fallback: speak the model output rather than dropping the call.
    return {"speech": raw[:900] or "I’m sorry, could you repeat that?", "done": False, "outcome": ""}


def _get_fastapi_app():
    global _APP
    if _APP is not None:
        return _APP

    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import PlainTextResponse

    app = FastAPI(title="JARVIS Phone Agent")

    @app.get("/phone-health")
    async def phone_health():
        return {"ok": True, "service": "jarvis-phone-agent"}

    @app.post("/phone-status")
    async def phone_status(request: Request):
        form = await request.form()
        call_sid = str(form.get("CallSid", ""))
        status = str(form.get("CallStatus", ""))
        if call_sid:
            _history_upsert(call_sid, status=status, status_updated_at=datetime.now().isoformat())
        return PlainTextResponse("ok")

    @app.websocket("/phone-agent")
    async def phone_agent_ws(websocket: WebSocket):
        cfg = _load_config()
        signature = websocket.headers.get("x-twilio-signature", "")
        if cfg.get("validate_twilio_signature", True):
            try:
                from twilio.request_validator import RequestValidator

                validator = RequestValidator(str(cfg["twilio_auth_token"]))
                valid = validator.validate(_ws_url(cfg), {}, signature)
            except Exception:
                valid = False
            if not valid:
                await websocket.close(code=1008)
                return

        await websocket.accept()
        call_sid = ""
        task_id = ""
        task_data: dict[str, Any] = {}
        transcript: list[dict] = []

        try:
            while True:
                msg = await websocket.receive_json()
                kind = msg.get("type")

                if kind == "setup":
                    call_sid = str(msg.get("callSid", ""))
                    params = msg.get("customParameters", {}) or {}
                    task_id = str(params.get("task_id", ""))
                    with _TASKS_LOCK:
                        task_data = dict(_TASKS.get(task_id, {}))
                    if call_sid:
                        _history_upsert(
                            call_sid,
                            status="in-progress",
                            to=task_data.get("to", msg.get("to", "")),
                            task=task_data.get("task", ""),
                        )
                    continue

                if kind == "prompt" and msg.get("last", True):
                    utterance = str(msg.get("voicePrompt", "")).strip()
                    if not utterance:
                        continue
                    transcript.append({"speaker": "Recipient", "text": utterance})
                    if call_sid:
                        _append_transcript(call_sid, "Recipient", utterance)

                    task = str(task_data.get("task") or "Have a brief helpful conversation and report the outcome.")
                    caller_name = str(cfg.get("caller_name") or "the user")
                    reply = await asyncio.to_thread(
                        _generate_phone_reply, task, caller_name, transcript, utterance
                    )
                    speech = reply["speech"]
                    transcript.append({"speaker": "JARVIS", "text": speech})
                    if call_sid:
                        _append_transcript(call_sid, "JARVIS", speech)

                    await websocket.send_json(
                        {
                            "type": "text",
                            "token": speech,
                            "last": True,
                            "interruptible": True,
                            "preemptible": True,
                        }
                    )

                    if reply.get("done"):
                        outcome = str(reply.get("outcome") or "Call task completed.")
                        if call_sid:
                            _history_upsert(call_sid, outcome=outcome, status="agent-complete")
                        await asyncio.sleep(1.5)
                        await websocket.send_json(
                            {
                                "type": "end",
                                "handoffData": json.dumps({"reason": "task_complete", "outcome": outcome})[:1000],
                            }
                        )
                        return
                    continue

                if kind == "interrupt":
                    continue

                if kind == "error":
                    description = str(msg.get("description", "ConversationRelay error"))
                    if call_sid:
                        _history_upsert(call_sid, status="error", error=description)
                    continue

        except WebSocketDisconnect:
            if call_sid:
                _history_upsert(call_sid, disconnected_at=datetime.now().isoformat())
        except Exception as exc:
            if call_sid:
                _history_upsert(call_sid, status="error", error=str(exc))
            try:
                await websocket.close(code=1011)
            except Exception:
                pass

    _APP = app
    return app


def ensure_phone_server() -> str:
    global _SERVER_THREAD
    cfg = _load_config()
    _validate_config(cfg)

    if _SERVER_THREAD and _SERVER_THREAD.is_alive():
        return f"Phone WebSocket server is running on local port {cfg['local_port']}."

    def run_server():
        import uvicorn

        app = _get_fastapi_app()
        _SERVER_STARTED.set()
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=int(cfg["local_port"]),
            log_level="warning",
            access_log=False,
        )

    _SERVER_STARTED.clear()
    _SERVER_THREAD = threading.Thread(target=run_server, daemon=True, name="JarvisPhoneServer")
    _SERVER_THREAD.start()
    _SERVER_STARTED.wait(timeout=3.0)
    time.sleep(0.25)
    if not _SERVER_THREAD.is_alive():
        raise RuntimeError("Phone agent server failed to start.")
    return f"Phone WebSocket server started on local port {cfg['local_port']}."


def place_ai_call(to: str, task: str) -> str:
    if not task.strip():
        raise ValueError("A clear call task is required.")
    cfg = _load_config()
    _validate_config(cfg)
    ensure_phone_server()

    try:
        from twilio.rest import Client
    except ImportError as exc:
        raise PhoneAgentSetupError("Twilio library missing. Run: pip install twilio") from exc

    number = _normalize_phone(to, str(cfg.get("default_country_code", "+1")))
    task_id = uuid.uuid4().hex
    with _TASKS_LOCK:
        _TASKS[task_id] = {"to": number, "task": task.strip(), "created_at": datetime.now().isoformat()}

    twiml = _twiml_for_call(cfg, task_id, task)
    public_base = str(cfg["public_base_url"]).rstrip("/")
    client = Client(str(cfg["twilio_account_sid"]), str(cfg["twilio_auth_token"]))
    call = client.calls.create(
        to=number,
        from_=str(cfg["twilio_phone_number"]),
        twiml=twiml,
        status_callback=f"{public_base}/phone-status",
        status_callback_event=["initiated", "ringing", "answered", "completed"],
        status_callback_method="POST",
    )
    _history_upsert(
        call.sid,
        status=getattr(call, "status", "queued") or "queued",
        to=number,
        task=task.strip(),
        task_id=task_id,
    )
    return (
        f"AI phone call started to {number}. Call SID: {call.sid}. "
        "JARVIS will identify itself as an AI assistant calling on the user's behalf."
    )


def call_status(call_sid: str) -> str:
    cfg = _load_config()
    _validate_config(cfg)
    try:
        from twilio.rest import Client
    except ImportError as exc:
        raise PhoneAgentSetupError("Twilio library missing. Run: pip install twilio") from exc

    client = Client(str(cfg["twilio_account_sid"]), str(cfg["twilio_auth_token"]))
    call = client.calls(call_sid.strip()).fetch()
    history = next((x for x in reversed(_history_load()) if x.get("call_sid") == call_sid.strip()), {})
    lines = [
        f"Call SID: {call.sid}",
        f"Status: {call.status}",
        f"To: {getattr(call, 'to', '')}",
        f"Duration: {getattr(call, 'duration', '') or 'not completed'} seconds",
    ]
    if history.get("outcome"):
        lines.append(f"Outcome: {history['outcome']}")
    transcript = history.get("transcript", []) or []
    if transcript:
        lines.append("Transcript:")
        for row in transcript[-12:]:
            lines.append(f"  {row.get('speaker')}: {row.get('text')}")
    return "\n".join(lines)


def call_history(limit: int = 5) -> str:
    items = _history_load()[-max(1, min(int(limit), 20)):]
    if not items:
        return "No JARVIS phone-call history yet."
    lines = ["Recent JARVIS calls:"]
    for item in reversed(items):
        lines.append(
            f"- {item.get('created_at', '')} | {item.get('to', '')} | "
            f"{item.get('status', '')} | SID {item.get('call_sid', '')}"
        )
        if item.get("outcome"):
            lines.append(f"  Outcome: {item['outcome']}")
        if item.get("task"):
            lines.append(f"  Task: {item['task']}")
    return "\n".join(lines)


def phone_action(query: str, player=None) -> str:
    """Compact command adapter used by the existing web_search tool.

    Commands:
      setup
      place | <phone/contact> | <task>
      status | <call SID>
      history
    """
    query = (query or "").strip()
    low = query.lower()
    if player:
        try:
            player.write_log(f"[Phone] {query}")
        except Exception:
            pass

    if low in {"setup", "check setup", "server"}:
        cfg = _load_config()
        _validate_config(cfg)
        server = ensure_phone_server()
        return f"Phone agent configuration is valid. {server} Public endpoint: {_ws_url(cfg)}"

    if low in {"history", "recent", "recent calls"}:
        return call_history()

    if low.startswith("status"):
        raw = query[len("status"):].strip().lstrip("|").strip()
        if not raw:
            raise ValueError("Call status requires a Call SID.")
        return call_status(raw)

    if low.startswith("place") or low.startswith("call"):
        raw = query.split("|", 1)
        if len(raw) != 2:
            raise ValueError("Phone call format: place | <phone number or contact name> | <task>")
        remainder = raw[1]
        parts = remainder.split("|", 1)
        if len(parts) != 2:
            raise ValueError("Phone call format: place | <phone number or contact name> | <task>")
        return place_ai_call(parts[0].strip(), parts[1].strip())

    raise ValueError("Phone command must be setup, place | recipient | task, status | SID, or history.")
