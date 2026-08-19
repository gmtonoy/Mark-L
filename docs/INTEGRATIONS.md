# JARVIS Integrations Setup

This branch adds real article reading, Google Workspace access, and outbound AI phone calls.

## 1. Install updated dependencies

From the Mark-L folder:

```powershell
pip install -r requirements.txt
```

## 2. Google Workspace — Gmail, Calendar, Contacts

JARVIS uses one local OAuth connection for all three Google services.

### Google Cloud setup

1. Create or select a project in Google Cloud Console.
2. Enable these APIs:
   - Gmail API
   - Google Calendar API
   - People API
3. Configure the OAuth consent screen.
4. While the app is in testing, add your own Google account as a test user.
5. Create an **OAuth Client ID** with application type **Desktop app**.
6. Download the OAuth client JSON.
7. Save it locally as:

```text
config/google_client_secret.json
```

Do **not** commit that file. It is ignored by git.

### Connect from JARVIS

Start Mark-L and say something like:

> JARVIS, connect my Google account.

A Google OAuth browser window opens. After approval, JARVIS stores the refresh token locally at:

```text
config/google_token.json
```

That token is also ignored by git.

### Example commands

- `Check my unread emails.`
- `Any emails from Stockton this week?`
- `Read the first email.`
- `Reply saying I'll be there at two.`
- `Email John. Subject: Meeting. Tell him I'll call tomorrow.`
- `What's on my calendar today?`
- `What do I have over the next seven days?`
- `Add lunch tomorrow from 1 PM to 2 PM.`
- `Find Sarah in my contacts.`

New email sends, replies, calendar creation, and calendar deletion only happen when explicitly requested.

## 3. AI phone calls — Twilio ConversationRelay

The phone agent uses:

- Twilio Programmable Voice for the real outbound phone call.
- Twilio ConversationRelay for speech recognition and speech synthesis.
- Gemini for the live conversational reasoning.
- A local FastAPI WebSocket server on port `8765` by default.

The person being called is told that JARVIS is an AI assistant calling on your behalf.

### Twilio setup

1. Create a Twilio account.
2. Get a Twilio phone number with Voice capability.
3. In Twilio Voice settings, accept the Predictive and Generative AI/ML Features Addendum required for ConversationRelay.
4. Make the JARVIS phone server publicly reachable over HTTPS/WSS. For local development, use a secure tunnel such as Cloudflare Tunnel or ngrok that forwards to local port `8765`.
5. Copy:

```text
config/phone_config.example.json
```

to:

```text
config/phone_config.json
```

6. Fill in the local file:

```json
{
  "twilio_account_sid": "AC...",
  "twilio_auth_token": "...",
  "twilio_phone_number": "+1...",
  "public_base_url": "https://your-public-tunnel.example",
  "local_port": 8765,
  "caller_name": "Tonoy",
  "default_country_code": "+1",
  "validate_twilio_signature": true
}
```

`phone_config.json` is ignored by git. You can also provide Twilio secrets with these environment variables instead:

```text
TWILIO_ACCOUNT_SID
TWILIO_AUTH_TOKEN
TWILIO_PHONE_NUMBER
JARVIS_PHONE_PUBLIC_URL
```

Keep `validate_twilio_signature` enabled. The WebSocket rejects connections that do not validate as Twilio requests.

### Example commands

- `JARVIS, check phone setup.`
- `Call the hotel and ask if late checkout is available tomorrow.`
- `Call John and tell him the meeting moved to three.`
- `What's the status of the call?`
- `Show my recent JARVIS calls.`

JARVIS stores local call status, transcripts, and outcomes in:

```text
memory/call_history.json
```

That file is ignored by git.

### Phone-agent boundaries

The phone agent does not call emergency/crisis-service numbers. It does not authorize purchases, payments, contracts, account changes, legal agreements, medical decisions, or other commitments unless the user's explicit call task supplied the exact authority and limits. If the human asks for a decision the agent does not have authority to make, it gathers the information and reports back.

## 4. News/article reading

News mode now attempts to open the actual article pages and extract the article text before creating a multi-source briefing. Direct article URLs can also be read.

Examples:

- `What's the latest OpenAI news? Tell me what actually happened.`
- `Read this article: https://...`
- `Compare what the sources are saying about this story.`

## 5. Automated checks

The repository includes `.github/workflows/python-checks.yml`. GitHub compiles the Python source on pushes and pull requests so syntax errors are caught before merge.
