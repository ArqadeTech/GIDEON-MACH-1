"""
Gmail API helpers for the personal assistant.

Uses OAuth2 desktop/installed-app credentials:
  1. Place Google Cloud OAuth client JSON at GMAIL_CREDENTIALS_PATH.
  2. On first run, a browser opens for consent; the refresh token is stored
     at GMAIL_TOKEN_PATH for subsequent runs.
"""

from __future__ import annotations

import base64
import logging
import os
from email.mime.text import MIMEText
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

DEFAULT_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


class GmailHandler:
    """Authenticate with Gmail and fetch / reply to messages."""

    def __init__(
        self,
        credentials_path: str | None = None,
        token_path: str | None = None,
        scopes: list[str] | None = None,
        user_id: str = "me",
    ) -> None:
        self.credentials_path = credentials_path or os.getenv(
            "GMAIL_CREDENTIALS_PATH", "./credentials.json"
        )
        self.token_path = token_path or os.getenv("GMAIL_TOKEN_PATH", "./token.json")
        scope_env = os.getenv("GMAIL_SCOPES", "")
        if scopes is not None:
            self.scopes = scopes
        elif scope_env.strip():
            self.scopes = [s.strip() for s in scope_env.split(",") if s.strip()]
        else:
            self.scopes = list(DEFAULT_SCOPES)
        self.user_id = user_id or os.getenv("GMAIL_USER", "me")
        self._service = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def authenticate(self) -> Any:
        """
        Load or refresh OAuth credentials and return a Gmail API service.

        Returns:
            googleapiclient discovery Resource for Gmail v1.
        """
        creds: Credentials | None = None

        if os.path.exists(self.token_path):
            creds = Credentials.from_authorized_user_file(self.token_path, self.scopes)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.info("Refreshing Gmail OAuth token")
                creds.refresh(Request())
            else:
                if not os.path.exists(self.credentials_path):
                    raise FileNotFoundError(
                        f"Gmail credentials not found at {self.credentials_path}. "
                        "Download OAuth client JSON from Google Cloud Console."
                    )
                logger.info("Starting Gmail OAuth consent flow")
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_path, self.scopes
                )
                # local_server opens a browser; use run_console() in headless setups
                creds = flow.run_local_server(port=0)

            with open(self.token_path, "w", encoding="utf-8") as token_file:
                token_file.write(creds.to_json())
            logger.info("Saved Gmail token to %s", self.token_path)

        self._service = build("gmail", "v1", credentials=creds)
        return self._service

    @property
    def service(self) -> Any:
        """Lazy-authenticated Gmail service."""
        if self._service is None:
            return self.authenticate()
        return self._service

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def fetch_unread_emails(self, max_results: int = 10) -> list[dict[str, Any]]:
        """
        Fetch unread inbox messages.

        Args:
            max_results: Maximum number of messages to return.

        Returns:
            List of dicts with id, threadId, subject, from, snippet, body_preview.
        """
        try:
            response = (
                self.service.users()
                .messages()
                .list(
                    userId=self.user_id,
                    q="is:unread in:inbox",
                    maxResults=max_results,
                )
                .execute()
            )
        except HttpError as exc:
            logger.exception("Failed to list unread emails")
            raise RuntimeError(f"Gmail list failed: {exc}") from exc

        messages = response.get("messages", [])
        results: list[dict[str, Any]] = []

        for meta in messages:
            detail = (
                self.service.users()
                .messages()
                .get(userId=self.user_id, id=meta["id"], format="full")
                .execute()
            )
            headers = {
                h["name"].lower(): h["value"]
                for h in detail.get("payload", {}).get("headers", [])
            }
            results.append(
                {
                    "id": detail["id"],
                    "threadId": detail.get("threadId"),
                    "subject": headers.get("subject", "(no subject)"),
                    "from": headers.get("from", ""),
                    "to": headers.get("to", ""),
                    "date": headers.get("date", ""),
                    "snippet": detail.get("snippet", ""),
                    "body_preview": self._extract_plain_text(detail.get("payload", {}))[
                        :500
                    ],
                }
            )

        logger.info("Fetched %d unread email(s)", len(results))
        return results

    def _extract_plain_text(self, payload: dict[str, Any]) -> str:
        """Best-effort plain-text body extraction from a Gmail message payload."""
        mime = payload.get("mimeType", "")
        body_data = payload.get("body", {}).get("data")

        if mime == "text/plain" and body_data:
            return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")

        for part in payload.get("parts", []) or []:
            text = self._extract_plain_text(part)
            if text:
                return text
        return ""

    # ------------------------------------------------------------------
    # Send / reply
    # ------------------------------------------------------------------

    def send_email_reply(
        self,
        to_address: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> dict[str, Any]:
        """
        Send an email, optionally threaded as a reply.

        Args:
            to_address: Recipient email address.
            subject: Subject line (prefix with Re: for replies if desired).
            body: Plain-text body.
            thread_id: Gmail threadId to keep the reply in the same thread.
            in_reply_to: Message-ID header of the parent message.
            references: References header chain for proper threading.

        Returns:
            Gmail API send() response (includes id, threadId, labelIds).
        """
        message = MIMEText(body)
        message["to"] = to_address
        message["subject"] = subject
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
        if references:
            message["References"] = references

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        payload: dict[str, Any] = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id

        try:
            sent = (
                self.service.users()
                .messages()
                .send(userId=self.user_id, body=payload)
                .execute()
            )
        except HttpError as exc:
            logger.exception("Failed to send email reply")
            raise RuntimeError(f"Gmail send failed: {exc}") from exc

        logger.info("Sent email id=%s threadId=%s", sent.get("id"), sent.get("threadId"))
        return sent

    def mark_as_read(self, message_id: str) -> dict[str, Any]:
        """Remove the UNREAD label from a message."""
        return (
            self.service.users()
            .messages()
            .modify(
                userId=self.user_id,
                id=message_id,
                body={"removeLabelIds": ["UNREAD"]},
            )
            .execute()
        )
