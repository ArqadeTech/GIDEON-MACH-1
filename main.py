"""
GIDEON-MACH-1 — FastAPI backend for a live AI personal assistant.

Endpoints:
  GET  /                 Health check
  POST /incoming-call    Twilio voice webhook → TwiML + media stream
  WS   /media-stream     Real-time Twilio Media Streams audio
  POST /incoming-sms     Twilio SMS webhook → agent reply
  POST /agent            Optional JSON API to talk to the agent directly
"""

from __future__ import annotations

import audioop
import base64
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import Connect, VoiceResponse

from modules.agent_core import AgentCore
from modules.gmail_handler import GmailHandler
from modules.steamos_handler import SteamOSHandler

load_dotenv()

logging.basicConfig(
    level=logging.DEBUG if os.getenv("DEBUG", "false").lower() == "true" else logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("gideon")

# Shared agent instance (initialized in lifespan)
agent: AgentCore | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Create handlers once at startup."""
    global agent
    agent = AgentCore(
        gmail=GmailHandler(),
        steamos=SteamOSHandler(),
    )
    logger.info("GIDEON agent ready · tools=%s", list(agent.tools.keys()))
    yield
    logger.info("Shutting down GIDEON")


app = FastAPI(
    title="GIDEON-MACH-1",
    description="Live AI personal assistant (Twilio + Gmail + SteamOS)",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _public_base_url(request: Request) -> str:
    """
    Resolve the public HTTPS base URL for TwiML callbacks.

    Prefer PUBLIC_BASE_URL (ngrok) so Twilio can reach the media stream;
    fall back to the request's host for local-only testing.
    """
    configured = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if configured:
        return configured

    # Honor reverse-proxy headers when present (ngrok / cloudflare)
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host"))
    return f"{proto}://{host}"


def _ws_url_for_media(request: Request) -> str:
    """Build wss://…/media-stream from the public HTTP base URL."""
    base = _public_base_url(request)
    parsed = urlparse(base)
    scheme = "wss" if parsed.scheme in ("https", "wss") else "ws"
    return f"{scheme}://{parsed.netloc}/media-stream"


def _get_agent() -> AgentCore:
    if agent is None:
        raise RuntimeError("Agent not initialized")
    return agent


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/")
async def health_check() -> dict[str, Any]:
    """Root health check — useful for ngrok / uptime probes."""
    return {
        "status": "ok",
        "service": "GIDEON-MACH-1",
        "version": "0.1.0",
        "endpoints": [
            "/",
            "/incoming-call",
            "/media-stream",
            "/incoming-sms",
            "/agent",
        ],
    }


# ---------------------------------------------------------------------------
# Twilio Voice
# ---------------------------------------------------------------------------

@app.post("/incoming-call")
async def incoming_call(request: Request) -> Response:
    """
    Twilio voice webhook.

    Returns TwiML that greets the caller and connects the call to the
    ``/media-stream`` WebSocket for bidirectional audio streaming.
    """
    form = await request.form()
    from_number = form.get("From", "unknown")
    call_sid = form.get("CallSid", "")
    logger.info("Incoming call from=%s sid=%s", from_number, call_sid)

    stream_url = _ws_url_for_media(request)
    response = VoiceResponse()
    response.say(
        "Hello. This is Gideon. Connecting you to the live assistant.",
        voice="Polly.Joanna",
    )
    # Connect → Stream redirects media to our WebSocket
    connect = Connect()
    connect.stream(url=stream_url)
    response.append(connect)

    # Fallback if the stream ends without further verbs
    response.say("The media stream has ended. Goodbye.")
    response.hangup()

    twiml = str(response)
    logger.debug("TwiML for call %s:\n%s", call_sid, twiml)
    return Response(content=twiml, media_type="application/xml")


# ---------------------------------------------------------------------------
# Twilio Media Streams (WebSocket)
# ---------------------------------------------------------------------------

@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket) -> None:
    """
    Handle real-time audio from Twilio Media Streams.

    Twilio sends JSON messages:
      - connected / start  — stream metadata (streamSid, callSid, mediaFormat)
      - media              — base64 µ-law (PCMU) audio payload
      - stop / mark        — stream lifecycle

    This boilerplate:
      1. Logs stream lifecycle events.
      2. Decodes inbound µ-law frames (ready to pipe into STT).
      3. Echoes a simple mark back so you can verify the bidirectional path.
      4. Leaves hooks where you would plug in STT → AgentCore → TTS → outbound media.

    Outbound audio to Twilio must be base64-encoded mulaw @ 8kHz mono, e.g.:
        await websocket.send_json({
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": base64.b64encode(mulaw_bytes).decode("ascii")},
        })
    """
    await websocket.accept()
    stream_sid: str | None = None
    call_sid: str | None = None
    inbound_frames = 0
    # Accumulate a short buffer if you later batch to an STT engine
    pcm_buffer = bytearray()

    logger.info("Media stream WebSocket accepted")

    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            event = data.get("event")

            if event == "connected":
                logger.info("Twilio media stream connected: %s", data.get("protocol"))

            elif event == "start":
                start = data.get("start", {})
                stream_sid = start.get("streamSid") or data.get("streamSid")
                call_sid = start.get("callSid")
                media_format = start.get("mediaFormat", {})
                logger.info(
                    "Stream started streamSid=%s callSid=%s format=%s",
                    stream_sid,
                    call_sid,
                    media_format,
                )
                # Acknowledge with a mark (optional but useful for debugging)
                if stream_sid:
                    await websocket.send_json(
                        {
                            "event": "mark",
                            "streamSid": stream_sid,
                            "mark": {"name": "gideon-ready"},
                        }
                    )

            elif event == "media":
                payload_b64 = data.get("media", {}).get("payload", "")
                if not payload_b64:
                    continue
                mulaw = base64.b64decode(payload_b64)
                # Convert µ-law → 16-bit linear PCM for STT pipelines
                try:
                    pcm = audioop.ulaw2lin(mulaw, 2)
                    pcm_buffer.extend(pcm)
                except Exception:  # noqa: BLE001
                    logger.debug("Skipping undecodable media frame")
                    continue

                inbound_frames += 1
                if inbound_frames % 50 == 0:
                    logger.debug(
                        "Received %d media frames (~%d PCM bytes buffered)",
                        inbound_frames,
                        len(pcm_buffer),
                    )
                    # --- Hook: flush buffer to STT, then agent, then TTS ---
                    # transcript = await stt_transcribe(bytes(pcm_buffer))
                    # reply = _get_agent().process_message(transcript, channel="voice")
                    # audio_out = await tts_synthesize(reply.message)
                    # await websocket.send_json({... media ...})
                    pcm_buffer.clear()

            elif event == "mark":
                logger.debug("Mark received: %s", data.get("mark"))

            elif event == "stop":
                logger.info(
                    "Stream stopped streamSid=%s callSid=%s frames=%d",
                    stream_sid,
                    call_sid,
                    inbound_frames,
                )
                break

            else:
                logger.debug("Unhandled media event: %s", event)

    except WebSocketDisconnect:
        logger.info(
            "Media stream disconnected streamSid=%s frames=%d",
            stream_sid,
            inbound_frames,
        )
    except Exception:
        logger.exception("Media stream error")
        await websocket.close()


# ---------------------------------------------------------------------------
# Twilio SMS
# ---------------------------------------------------------------------------

@app.post("/incoming-sms")
async def incoming_sms(
    From: str = Form(...),
    Body: str = Form(""),
    MessageSid: str = Form(""),
) -> Response:
    """
    Twilio SMS webhook.

    Reads the inbound message, runs it through the agent (placeholder LLM +
    tools), and replies with TwiML <Message>.
    """
    logger.info("Incoming SMS from=%s sid=%s body=%r", From, MessageSid, Body)

    # Optional: only accept SMS from your own number
    allowed = os.getenv("MY_PHONE_NUMBER", "").strip()
    if allowed and From != allowed:
        logger.warning("Rejected SMS from unauthorized number %s", From)
        twiml = MessagingResponse()
        twiml.message("Unauthorized sender.")
        return Response(content=str(twiml), media_type="application/xml")

    try:
        result = _get_agent().process_message(Body, channel="sms")
        reply_text = result.message
    except Exception as exc:  # noqa: BLE001
        logger.exception("Agent failed on SMS")
        reply_text = f"Sorry — I hit an error: {exc}"

    # Twilio SMS has a practical length limit; truncate politely
    if len(reply_text) > 1500:
        reply_text = reply_text[:1490] + "…"

    twiml = MessagingResponse()
    twiml.message(reply_text)
    return Response(content=str(twiml), media_type="application/xml")


# ---------------------------------------------------------------------------
# Direct agent API (local testing without Twilio)
# ---------------------------------------------------------------------------

class AgentRequest(BaseModel):
    message: str = Field(..., min_length=1, description="User message for the agent")
    channel: str = Field(default="api", description="Channel label for prompt context")


@app.post("/agent")
async def agent_endpoint(payload: AgentRequest) -> JSONResponse:
    """JSON chat endpoint for local testing of the orchestration loop."""
    result = _get_agent().process_message(payload.message, channel=payload.channel)
    return JSONResponse(
        {
            "message": result.message,
            "turns": result.turns,
            "tool_results": result.tool_results,
        }
    )


@app.get("/tools")
async def list_tools() -> dict[str, Any]:
    """List registered agent tools (handy while wiring the LLM)."""
    return {"tools": _get_agent().tool_schemas()}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host=host, port=port, reload=True)
