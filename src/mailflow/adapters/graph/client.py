"""Thin Graph REST client over an injected HttpTransport. Handles auth header,
the $select field list, the internetMessageId fallback, and 429/Retry-After
retries. No real HTTP library here — that is the transport's job."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

from mailflow.adapters.graph.transport import GraphError, HttpResponse, HttpTransport, TokenProvider

MESSAGE_SELECT = (
    "id,internetMessageId,subject,from,sender,toRecipients,ccRecipients,"
    "bccRecipients,replyTo,body,uniqueBody,bodyPreview,isDraft,hasAttachments,"
    "receivedDateTime,sentDateTime,conversationId,parentFolderId,"
    "internetMessageHeaders,categories"
)


class GraphClient:
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
                retry_after = float(resp.headers.get("Retry-After", "1"))
                time.sleep(retry_after)
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
                raise GraphError(resp.status_code, message)
            return resp

    def get_message(self, user_id: str, message_id: str) -> dict[str, Any]:
        url = f"{self.base_url}/users/{user_id}/messages/{message_id}?$select={MESSAGE_SELECT}"
        result = self._request("GET", url).json()
        assert isinstance(result, dict)
        return result

    def get_message_by_internet_id(self, user_id: str, internet_id: str) -> dict[str, Any] | None:
        flt = quote(f"internetMessageId eq '{internet_id}'")
        url = f"{self.base_url}/users/{user_id}/messages?$filter={flt}&$select={MESSAGE_SELECT}"
        data = self._request("GET", url).json()
        items = (data or {}).get("value", []) if isinstance(data, dict) else []
        return items[0] if items else None

    def list_attachments(self, user_id: str, message_id: str) -> list[dict[str, Any]]:
        url = (f"{self.base_url}/users/{user_id}/messages/{message_id}/attachments"
               f"?$select=id,name,contentType,size,isInline,contentId")
        data = self._request("GET", url).json()
        items = (data or {}).get("value", []) if isinstance(data, dict) else []
        return [a for a in items if isinstance(a, dict)]

    def delta_sweep(
        self, user_id: str, folder: str, delta_link: str | None = None
    ) -> tuple[list[dict[str, Any]], str]:
        """Run a delta query for a mailbox folder, following @odata.nextLink to the
        end. Returns (all changed messages, the @odata.deltaLink resume token). Pass
        a stored delta_link to resume from the last sweep."""
        url = delta_link or (
            f"{self.base_url}/users/{user_id}/mailFolders('{folder}')/messages/delta"
            f"?$select={MESSAGE_SELECT}"
        )
        messages: list[dict[str, Any]] = []
        while True:
            data = self._request("GET", url).json()
            if not isinstance(data, dict):
                return messages, ""
            messages.extend(m for m in data.get("value", []) if isinstance(m, dict))
            next_link = data.get("@odata.nextLink")
            if next_link:
                url = str(next_link)
                continue
            return messages, str(data.get("@odata.deltaLink", ""))

    def list_subscriptions(self) -> list[dict[str, Any]]:
        data = self._request("GET", f"{self.base_url}/subscriptions").json()
        items = (data or {}).get("value", []) if isinstance(data, dict) else []
        return [s for s in items if isinstance(s, dict)]

    def create_subscription(
        self, *, resource: str, notification_url: str,
        client_state: str, expiration_iso: str,
    ) -> dict[str, Any]:
        body = {
            "changeType": "created,updated",
            "resource": resource,
            "notificationUrl": notification_url,
            "lifecycleNotificationUrl": notification_url,
            "clientState": client_state,
            "expirationDateTime": expiration_iso,
        }
        result = self._request("POST", f"{self.base_url}/subscriptions", json=body).json()
        assert isinstance(result, dict)
        return result

    def renew_subscription(self, subscription_id: str, expiration_iso: str) -> dict[str, Any]:
        url = f"{self.base_url}/subscriptions/{subscription_id}"
        result = self._request("PATCH", url, json={"expirationDateTime": expiration_iso}).json()
        assert isinstance(result, dict)
        return result

    def delete_subscription(self, subscription_id: str) -> None:
        self._request("DELETE", f"{self.base_url}/subscriptions/{subscription_id}")
