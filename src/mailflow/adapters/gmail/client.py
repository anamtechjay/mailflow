"""Thin Gmail REST client over an injected HttpTransport. Handles the bearer token,
history diffing, raw message fetch, watch/stop, and 429/Retry-After retries. No real
HTTP library here — that is the transport's job."""

from __future__ import annotations

import time
from typing import Any

from mailflow.adapters.gmail.transport import (
    GmailError,
    HttpResponse,
    HttpTransport,
    StaleHistoryError,
    TokenProvider,
)


class GmailClient:
    def __init__(
        self, *, base_url: str, token_provider: TokenProvider,
        transport: HttpTransport, max_retries: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.tokens = token_provider
        self.transport = transport
        self.max_retries = max_retries

    def _request(self, method: str, url: str, *, json: Any | None = None) -> HttpResponse:
        attempt = 0
        while True:
            headers = {
                "Authorization": f"Bearer {self.tokens.get_token()}",
                "Content-Type": "application/json",
            }
            resp = self.transport.request(method, url, headers=headers, json=json)
            if resp.status_code == 429 and attempt < self.max_retries:
                time.sleep(float(resp.headers.get("Retry-After", "1")))
                attempt += 1
                continue
            if resp.status_code >= 400:
                message = ""
                try:
                    body = resp.json()
                    if isinstance(body, dict):
                        message = str(body.get("error", {}).get("message", ""))
                except Exception:  # noqa: BLE001 - error body may not be JSON
                    message = ""
                raise GmailError(resp.status_code, message)
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
