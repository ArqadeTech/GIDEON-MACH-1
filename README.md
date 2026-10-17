# GIDEON-MACH-1

Live AI personal assistant boilerplate built with **Python**, **FastAPI**, **Twilio** (voice + SMS), **Gmail API**, and **SteamOS/Linux** diagnostics. Designed to be developed and iterated in **Cursor**.

## What’s included

| Path | Role |
|------|------|
| `main.py` | FastAPI app: health, Twilio voice/SMS webhooks, media-stream WebSocket, `/agent` test API |
| `modules/agent_core.py` | LLM + tool-calling orchestration loop |
| `modules/gmail_handler.py` | Gmail OAuth2, unread fetch, send reply |
| `modules/steamos_handler.py` | Allowlisted local diagnostics (`psutil` + safe shell) |
| `.env.example` | Environment variable template |
| `requirements.txt` | Python dependencies |

## Quick start (local)

### 1. Clone & virtualenv

```bash
cd GIDEON-MACH-1   # or your checkout path
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your Twilio SID/token/numbers and Gmail paths
```

### 3. Run the API

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
# or: python main.py
```

Smoke-test:

```bash
curl http://127.0.0.1:8000/
curl -X POST http://127.0.0.1:8000/agent \
  -H 'Content-Type: application/json' \
  -d '{"message":"system status"}'
```

Useful local probes:

- `GET /` — health
- `GET /tools` — registered agent tools
- `POST /agent` — chat without Twilio

## Wire Twilio with ngrok

Twilio must reach your laptop over HTTPS. Use [ngrok](https://ngrok.com/) (or Cloudflare Tunnel) while developing.

### 1. Start the app

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### 2. Expose port 8000

```bash
ngrok http 8000
```

Copy the HTTPS forwarding URL, e.g. `https://abc123.ngrok-free.app`.

### 3. Point `.env` at ngrok

```env
PUBLIC_BASE_URL=https://abc123.ngrok-free.app
```

Restart uvicorn so TwiML embeds `wss://abc123.ngrok-free.app/media-stream`.

### 4. Configure the Twilio phone number

In the [Twilio Console](https://console.twilio.com/) → Phone Numbers → your number:

| Channel | Method | URL |
|---------|--------|-----|
| A call comes in | `HTTP POST` | `https://abc123.ngrok-free.app/incoming-call` |
| A message comes in | `HTTP POST` | `https://abc123.ngrok-free.app/incoming-sms` |

Save. Call or text the Twilio number from `MY_PHONE_NUMBER`.

### 5. Verify the path

1. `curl https://abc123.ngrok-free.app/` → `{"status":"ok",...}`
2. SMS something like `system status` or `check my email`
3. Place a voice call — you should hear the greeting and see `/media-stream` logs

> ngrok free URLs change on restart. Update Twilio webhooks and `PUBLIC_BASE_URL` each time, or use a reserved domain.

## Gmail API setup

1. Create a Google Cloud project and enable the **Gmail API**.
2. Configure the OAuth consent screen; create an **OAuth client ID** (Desktop app).
3. Download the client JSON → save as `credentials.json` (or set `GMAIL_CREDENTIALS_PATH`).
4. First authenticated call opens a browser; `token.json` is written for reuse.

Scopes default to `https://www.googleapis.com/auth/gmail.modify` (read + send + label changes).

Example (after OAuth once):

```bash
curl -X POST http://127.0.0.1:8000/agent \
  -H 'Content-Type: application/json' \
  -d '{"message":"check my email"}'
```

## SteamOS / Linux tools

`SteamOSHandler` only runs **named** diagnostics and an **allowlisted** set of shell commands (`uname`, `uptime`, `df`, `free`, `whoami`, `hostname`). Free-form shell from the model is rejected.

Try:

```bash
curl -X POST http://127.0.0.1:8000/agent \
  -H 'Content-Type: application/json' \
  -d '{"message":"system status"}'
```

## Agent / LLM

`modules/agent_core.py` ships a **placeholder** `call_llm` that maps simple phrases to tools (`check my email`, `system status`, `sms: hello`). Replace it with your provider:

```python
from modules.agent_core import AgentCore

def my_llm(user_message, history):
    # Call OpenAI / Anthropic / local model; return either plain text
    # or JSON: {"tool": "check_emails", "arguments": {"max_results": 5}}
    ...

agent = AgentCore(llm_fn=my_llm)
```

Or set `LLM_PROVIDER`, `LLM_API_KEY`, and `LLM_MODEL` in `.env` once you wire a real client.

## Voice streaming notes

`/media-stream` accepts Twilio Media Streams (µ-law 8 kHz). The boilerplate:

- Logs `connected` / `start` / `media` / `stop`
- Decodes inbound audio to PCM (ready for STT)
- Sends a `gideon-ready` mark
- Leaves clear hooks for **STT → AgentCore → TTS → outbound media**

Outbound frames to Twilio must be base64 µ-law; see comments in `main.py`.

## Project layout

```
.
├── main.py
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
└── modules/
    ├── __init__.py
    ├── agent_core.py
    ├── gmail_handler.py
    └── steamos_handler.py
```

## Security notes

- Keep `.env`, `credentials.json`, and `token.json` out of git (already in `.gitignore`).
- `/incoming-sms` optionally rejects senders that are not `MY_PHONE_NUMBER`.
- SteamOS commands are allowlisted; do not widen this without careful review.
- Validate Twilio request signatures in production (`X-Twilio-Signature`) before trusting webhooks.

## License

MIT — see `LICENSE`.
