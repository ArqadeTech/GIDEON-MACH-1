"""
Agent orchestration: LLM + tool calling for the personal assistant.

This module defines a simple tool-calling loop. The LLM function is a
placeholder — swap ``call_llm`` for OpenAI / Anthropic / a local model
when you are ready. Tools cover Gmail, SteamOS diagnostics, and SMS.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from twilio.rest import Client as TwilioClient

from modules.gmail_handler import GmailHandler
from modules.steamos_handler import SteamOSHandler

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    """A single tool invocation requested by the model."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResponse:
    """Final text answer plus any tool results collected during the loop."""

    message: str
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    turns: int = 0


class AgentCore:
    """
    Orchestrates an LLM with named tools.

    Available tools:
      - check_emails / fetch_unread_emails
      - send_email_reply
      - steamos_* / run_steamos_command
      - send_sms
    """

    MAX_TOOL_TURNS = 5

    def __init__(
        self,
        gmail: GmailHandler | None = None,
        steamos: SteamOSHandler | None = None,
        twilio_client: TwilioClient | None = None,
        llm_fn: Callable[[str, list[dict[str, Any]]], str] | None = None,
    ) -> None:
        self.gmail = gmail or GmailHandler()
        self.steamos = steamos or SteamOSHandler()
        self._twilio = twilio_client
        self._llm_fn = llm_fn or call_llm

        self.tools: dict[str, Callable[..., Any]] = {
            "check_emails": self._tool_check_emails,
            "fetch_unread_emails": self._tool_check_emails,
            "send_email_reply": self._tool_send_email_reply,
            "get_system_status": self._tool_system_status,
            "run_steamos_command": self._tool_run_steamos_command,
            "send_sms": self._tool_send_sms,
        }
        # Expose each SteamOS diagnostic under steamos_<name>
        for name in self.steamos.list_tools():
            self.tools[f"steamos_{name}"] = self._make_steamos_tool(name)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_message(self, user_message: str, channel: str = "sms") -> AgentResponse:
        """
        Run the orchestration loop for a single user message.

        Args:
            user_message: Incoming text (SMS body, email snippet, etc.).
            channel: Origin channel label for prompt context (sms, voice, api).

        Returns:
            AgentResponse with the final assistant message.
        """
        history: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": f"[{channel}] {user_message}"},
        ]
        tool_results: list[dict[str, Any]] = []

        for turn in range(1, self.MAX_TOOL_TURNS + 1):
            raw = self._llm_fn(user_message, history)
            tool_call = self._parse_tool_call(raw)

            if tool_call is None:
                # Plain-language final answer
                return AgentResponse(message=raw.strip(), tool_results=tool_results, turns=turn)

            logger.info("Tool call turn=%d name=%s args=%s", turn, tool_call.name, tool_call.arguments)
            result = self._execute_tool(tool_call)
            tool_results.append({"tool": tool_call.name, "result": result})

            history.append({"role": "assistant", "content": raw})
            history.append(
                {
                    "role": "tool",
                    "name": tool_call.name,
                    "content": json.dumps(result, default=str),
                }
            )

        # Safety: stop after max turns and summarize
        summary = (
            "I hit the tool-call limit. Here's what I gathered:\n"
            + json.dumps(tool_results, indent=2, default=str)[:1500]
        )
        return AgentResponse(message=summary, tool_results=tool_results, turns=self.MAX_TOOL_TURNS)

    def tool_schemas(self) -> list[dict[str, Any]]:
        """Describe tools for a real function-calling LLM API."""
        return [
            {
                "name": "check_emails",
                "description": "Fetch unread Gmail inbox messages.",
                "parameters": {"max_results": "int, optional, default 5"},
            },
            {
                "name": "send_email_reply",
                "description": "Send an email reply via Gmail.",
                "parameters": {
                    "to_address": "str",
                    "subject": "str",
                    "body": "str",
                    "thread_id": "str, optional",
                },
            },
            {
                "name": "get_system_status",
                "description": "Snapshot CPU, memory, disk, and uptime on the SteamOS host.",
                "parameters": {},
            },
            {
                "name": "run_steamos_command",
                "description": "Run an allowlisted diagnostic (uname, uptime, df, free, whoami, hostname).",
                "parameters": {"command_name": "str"},
            },
            {
                "name": "send_sms",
                "description": "Send an SMS via Twilio to MY_PHONE_NUMBER or a given number.",
                "parameters": {"body": "str", "to": "str, optional"},
            },
            *[
                {
                    "name": f"steamos_{n}",
                    "description": f"SteamOS diagnostic: {n}",
                    "parameters": {},
                }
                for n in self.steamos.list_tools()
            ],
        ]

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def _tool_check_emails(self, max_results: int = 5) -> dict[str, Any]:
        try:
            emails = self.gmail.fetch_unread_emails(max_results=max_results)
            return {"ok": True, "count": len(emails), "emails": emails}
        except Exception as exc:  # noqa: BLE001 — surface errors to the LLM
            logger.exception("check_emails failed")
            return {"ok": False, "error": str(exc)}

    def _tool_send_email_reply(
        self,
        to_address: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            sent = self.gmail.send_email_reply(
                to_address=to_address,
                subject=subject,
                body=body,
                thread_id=thread_id,
            )
            return {"ok": True, "sent": sent}
        except Exception as exc:  # noqa: BLE001
            logger.exception("send_email_reply failed")
            return {"ok": False, "error": str(exc)}

    def _tool_system_status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "cpu": self.steamos.get_cpu_stats(),
            "memory": self.steamos.get_memory_stats(),
            "disk": self.steamos.get_disk_stats(),
            "uptime": self.steamos.get_uptime(),
            "system": self.steamos.get_system_info(),
        }

    def _tool_run_steamos_command(self, command_name: str) -> dict[str, Any]:
        try:
            return {"ok": True, **self.steamos.run_safe_command(command_name)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def _tool_send_sms(self, body: str, to: str | None = None) -> dict[str, Any]:
        to_number = to or os.getenv("MY_PHONE_NUMBER")
        from_number = os.getenv("TWILIO_PHONE_NUMBER")
        if not to_number or not from_number:
            return {
                "ok": False,
                "error": "MY_PHONE_NUMBER and TWILIO_PHONE_NUMBER must be set",
            }
        client = self._get_twilio()
        if client is None:
            return {"ok": False, "error": "Twilio credentials not configured"}
        try:
            message = client.messages.create(body=body, from_=from_number, to=to_number)
            return {"ok": True, "sid": message.sid, "to": to_number}
        except Exception as exc:  # noqa: BLE001
            logger.exception("send_sms failed")
            return {"ok": False, "error": str(exc)}

    def _make_steamos_tool(self, name: str) -> Callable[[], dict[str, Any]]:
        def _runner() -> dict[str, Any]:
            try:
                return {"ok": True, "data": self.steamos.run_tool(name)}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc)}

        _runner.__name__ = f"steamos_{name}"
        return _runner

    def _get_twilio(self) -> TwilioClient | None:
        if self._twilio is not None:
            return self._twilio
        sid = os.getenv("TWILIO_ACCOUNT_SID")
        token = os.getenv("TWILIO_AUTH_TOKEN")
        if not sid or not token:
            return None
        self._twilio = TwilioClient(sid, token)
        return self._twilio

    def _execute_tool(self, call: ToolCall) -> Any:
        fn = self.tools.get(call.name)
        if fn is None:
            return {"ok": False, "error": f"Unknown tool: {call.name}"}
        try:
            return fn(**call.arguments)
        except TypeError as exc:
            return {"ok": False, "error": f"Bad arguments for {call.name}: {exc}"}

    # ------------------------------------------------------------------
    # Prompt + parsing helpers
    # ------------------------------------------------------------------

    def _system_prompt(self) -> str:
        tool_lines = "\n".join(
            f"- {t['name']}: {t['description']}" for t in self.tool_schemas()
        )
        return (
            "You are GIDEON, a personal AI assistant with access to Gmail, "
            "SteamOS diagnostics, and SMS via Twilio.\n"
            "When you need a tool, reply with ONLY a JSON object:\n"
            '  {"tool": "<name>", "arguments": { ... }}\n'
            "When you can answer the user, reply with plain text (no JSON).\n"
            f"Available tools:\n{tool_lines}"
        )

    def _parse_tool_call(self, raw: str) -> ToolCall | None:
        """Extract a tool call JSON object from the model output, if present."""
        text = raw.strip()
        # Fenced JSON
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        candidates = [fenced.group(1)] if fenced else []
        # Whole message or embedded object with "tool"
        if text.startswith("{"):
            candidates.append(text)
        embedded = re.search(
            r'\{\s*"tool"\s*:\s*"[^"]+".*?\}', text, re.DOTALL
        )
        if embedded:
            candidates.append(embedded.group(0))

        for candidate in candidates:
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            name = data.get("tool") or data.get("name")
            if not name:
                continue
            args = data.get("arguments") or data.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            return ToolCall(name=str(name), arguments=args)
        return None


# ----------------------------------------------------------------------
# Placeholder LLM — replace with a real provider
# ----------------------------------------------------------------------

def call_llm(user_message: str, history: list[dict[str, Any]]) -> str:
    """
    Placeholder LLM that routes simple intents to tools without an API key.

    Replace this function (or pass ``llm_fn=`` into AgentCore) with a real
    model that supports tool / function calling.
    """
    _ = history  # reserved for conversation context with a real model
    lower = user_message.lower()

    # After a tool result, produce a short human summary
    tool_msgs = [m for m in history if m.get("role") == "tool"]
    if tool_msgs:
        last = tool_msgs[-1]
        name = last.get("name", "tool")
        try:
            payload = json.loads(last.get("content", "{}"))
        except json.JSONDecodeError:
            payload = {"raw": last.get("content")}
        return (
            f"Done — ran `{name}`.\n"
            f"Result preview:\n{json.dumps(payload, indent=2, default=str)[:1200]}"
        )

    # Intent heuristics (boilerplate only)
    if any(k in lower for k in ("email", "inbox", "unread", "gmail")):
        return json.dumps({"tool": "check_emails", "arguments": {"max_results": 5}})

    if any(k in lower for k in ("cpu", "memory", "disk", "uptime", "system", "status", "steam")):
        return json.dumps({"tool": "get_system_status", "arguments": {}})

    if "df" in lower or "disk space" in lower:
        return json.dumps(
            {"tool": "run_steamos_command", "arguments": {"command_name": "df"}}
        )

    if lower.startswith("sms:") or lower.startswith("text:"):
        body = user_message.split(":", 1)[1].strip()
        return json.dumps({"tool": "send_sms", "arguments": {"body": body}})

    provider = os.getenv("LLM_PROVIDER", "placeholder")
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    return (
        f"[placeholder LLM · provider={provider} · model={model}] "
        f"I heard: {user_message!r}. "
        "Try: 'check my email', 'system status', or 'sms: hello'."
    )
