"""Thin Gmail REST client over an injected HttpTransport. Handles the bearer token,
history diffing, raw message fetch, watch/stop, and 429/Retry-After retries. No real
HTTP library here — that is the transport's job."""

from __future__ import annotations

import random
import threading
import time
from typing import Any

from mailflow.adapters.gmail.transport import (
    GmailError,
    HttpResponse,
    HttpTransport,
    RefreshableTokenProvider,
    StaleHistoryError,
    TokenProvider,
    gmail_error_for,
)
from mailflow.core.errors import TransientError


class GmailClient:
    def __init__(
        self, *, base_url: str, token_provider: TokenProvider,
        transport: HttpTransport, max_retries: int = 3,
        max_in_flight: int = 8, backoff_base: float = 1.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.tokens = token_provider
        self.transport = transport
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        # B4: bound concurrent in-flight requests so a storm can't fan out unboundedly.
        self._inflight = threading.BoundedSemaphore(max(1, max_in_flight))

    def _backoff_sleep(self, attempt: int, retry_after: str | None) -> None:
        """Exponential backoff with jitter, never shorter than a server Retry-After."""
        delay = self.backoff_base * (2 ** attempt) + random.uniform(0.0, self.backoff_base)
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        time.sleep(delay)

    def _request(self, method: str, url: str, *, json: Any | None = None) -> HttpResponse:
        # B4: shed load once saturated rather than queueing without bound.
        if not self._inflight.acquire(blocking=False):
            raise TransientError("gmail in-flight concurrency cap reached")
        try:
            return self._request_inner(method, url, json=json)
        finally:
            self._inflight.release()

    def _request_inner(self, method: str, url: str, *, json: Any | None) -> HttpResponse:
        attempt = 0
        auth_retried = False
        while True:
            headers = {
                "Authorization": f"Bearer {self.tokens.get_token()}",
                "Content-Type": "application/json",
            }
            try:
                resp = self.transport.request(method, url, headers=headers, json=json)
            except Exception as exc:  # noqa: BLE001 - network failure is transient
                if attempt < self.max_retries:
                    self._backoff_sleep(attempt, None)
                    attempt += 1
                    continue
                raise TransientError(f"gmail network error: {exc}") from exc
            status = resp.status_code
            # B4: retry 429 AND 5xx with bounded backoff; after the bound the typed
            # mapping turns the final 429/5xx into a TransientError (DLQ-or-retry upstream).
            if (status == 429 or status >= 500) and attempt < self.max_retries:
                self._backoff_sleep(attempt, resp.headers.get("Retry-After"))
                attempt += 1
                continue
            # A2: a 401 means the access token was rejected. Force ONE refresh and retry
            # the request a single time with a fresh bearer token (bounded by auth_retried,
            # independent of max_retries); a second 401 propagates as GmailAuthError.
            if (
                status == 401
                and not auth_retried
                and isinstance(self.tokens, RefreshableTokenProvider)
            ):
                self.tokens.force_refresh()
                auth_retried = True
                continue
            if status >= 400:
                message = ""
                try:
                    body = resp.json()
                    if isinstance(body, dict):
                        message = str(body.get("error", {}).get("message", ""))
                except Exception:  # noqa: BLE001 - error body may not be JSON
                    message = ""
                raise gmail_error_for(status, message)
            return resp

    def history_message_ids(
        self, user_id: str, start_history_id: str, label_id: str | None = None
    ) -> tuple[list[str], str]:
        """Diff the mailbox since start_history_id. Returns (added message ids in order,
        latest historyId to persist as the new cursor)."""
        ids: list[str] = []
        seen: set[str] = set()
        latest = start_history_id
        page_token: str | None = None
        while True:
            url = (f"{self.base_url}/users/{user_id}/history"
                   f"?startHistoryId={start_history_id}&historyTypes=messageAdded")
            if label_id:
                url += f"&labelId={label_id}"
            if page_token:
                url += f"&pageToken={page_token}"
            try:
                resp = self._request("GET", url)
            except GmailError as exc:
                # 404 = the stored historyId is too old to diff from (spec: Gmail returns
                # 404, not 410). Signal a re-seed; any other error propagates.
                if exc.status_code == 404:
                    raise StaleHistoryError(
                        f"historyId {start_history_id} too old for {user_id}"
                    ) from exc
                raise
            data = resp.json()
            if not isinstance(data, dict):
                break
            if data.get("historyId"):
                latest = str(data["historyId"])
            for h in data.get("history", []) or []:
                for added in h.get("messagesAdded", []) or []:
                    mid = str((added.get("message") or {}).get("id", ""))
                    if mid and mid not in seen:
                        seen.add(mid)
                        ids.append(mid)
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return ids, latest

    def get_profile(self, user_id: str) -> dict[str, Any]:
        """users.getProfile -> {emailAddress, historyId, messagesTotal}. Used for the
        connectivity smoke test and to seed the initial historyId cursor."""
        result = self._request("GET", f"{self.base_url}/users/{user_id}/profile").json()
        assert isinstance(result, dict)
        return result

    def get_message_raw(self, user_id: str, message_id: str) -> dict[str, Any]:
        """messages.get(format=raw) -> dict with base64url 'raw' + 'sizeEstimate'."""
        url = f"{self.base_url}/users/{user_id}/messages/{message_id}?format=raw"
        result = self._request("GET", url).json()
        assert isinstance(result, dict)
        return result

    def get_attachment(self, user_id: str, message_id: str, attachment_id: str) -> dict[str, Any]:
        url = (f"{self.base_url}/users/{user_id}/messages/{message_id}"
               f"/attachments/{attachment_id}")
        result = self._request("GET", url).json()
        assert isinstance(result, dict)
        return result

    def watch(self, user_id: str, topic_path: str, label_ids: list[str]) -> dict[str, Any]:
        body = {"topicName": topic_path, "labelIds": label_ids}
        result = self._request("POST", f"{self.base_url}/users/{user_id}/watch", json=body).json()
        assert isinstance(result, dict)
        return result

    def stop(self, user_id: str) -> None:
        self._request("POST", f"{self.base_url}/users/{user_id}/stop")
