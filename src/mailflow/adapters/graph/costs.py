"""CostTracker — query Azure Cost Management for accrued spend and estimate the
remaining free credit (the $200 trial grant by default).

Note on the $200 trial credit: Azure does NOT expose the raw remaining credit
balance via a clean API for trial/MOSP accounts (portal only), and the trial
credit expires 30 days after signup regardless of usage. What IS queryable is the
*accrued cost*, so we report `remaining ~= grant - accrued_cost`. Pair this with a
native Azure Budget alert for hard guardrails.

Testable seam: uses the adapter's injected HttpTransport + TokenProvider (the token
must be ARM-scoped: `https://management.azure.com/.default`). No azure SDK import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mailflow.adapters.graph.transport import GraphError, HttpTransport, TokenProvider

COST_API_VERSION = "2023-11-01"
DEFAULT_CREDIT_GRANT = 200.0  # USD — Azure free-trial credit


@dataclass(frozen=True)
class CostSummary:
    amount: float
    currency: str = ""


class CostTracker:
    def __init__(
        self,
        *,
        subscription_id: str,
        transport: HttpTransport,
        token_provider: TokenProvider,
        credit_grant: float = DEFAULT_CREDIT_GRANT,
        base_url: str = "https://management.azure.com",
        timeframe: str = "MonthToDate",
    ) -> None:
        self.subscription_id = subscription_id
        self.transport = transport
        self.tokens = token_provider
        self.credit_grant = credit_grant
        self.base_url = base_url.rstrip("/")
        self.timeframe = timeframe

    def accrued_cost(self) -> CostSummary:
        url = (
            f"{self.base_url}/subscriptions/{self.subscription_id}"
            f"/providers/Microsoft.CostManagement/query?api-version={COST_API_VERSION}"
        )
        body: dict[str, Any] = {
            "type": "ActualCost",
            "timeframe": self.timeframe,
            "dataset": {
                "granularity": "None",
                "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            },
        }
        headers = {
            "Authorization": f"Bearer {self.tokens.get_token()}",
            "Content-Type": "application/json",
        }
        resp = self.transport.request("POST", url, headers=headers, json=body)
        if resp.status_code >= 400:
            raise GraphError(resp.status_code, "cost management query failed")
        return self._parse(resp.json())

    def remaining_credit(self) -> float:
        return max(0.0, self.credit_grant - self.accrued_cost().amount)

    @staticmethod
    def _parse(data: Any) -> CostSummary:
        props = data.get("properties", {}) if isinstance(data, dict) else {}
        columns = props.get("columns", []) or []
        rows = props.get("rows", []) or []
        names = [str(c.get("name", "")).lower() for c in columns]
        cost_idx = next(
            (i for i, n in enumerate(names) if n in ("cost", "pretaxcost", "costusd")), 0
        )
        cur_idx = next((i for i, n in enumerate(names) if n == "currency"), None)
        total = 0.0
        for row in rows:
            try:
                total += float(row[cost_idx])
            except (IndexError, TypeError, ValueError):
                continue
        currency = ""
        if rows and cur_idx is not None:
            try:
                currency = str(rows[0][cur_idx])
            except IndexError:
                currency = ""
        return CostSummary(amount=total, currency=currency)
