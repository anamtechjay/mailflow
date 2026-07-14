"""connect() must raise a clear ConfigError (not a raw KeyError) when a required
credential is missing — the first thing an operator hits when misconfiguring a deploy."""

from __future__ import annotations

import pytest

from mailflow import connect
from mailflow.core.errors import ConfigError


def test_connect_gmail_missing_client_id_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="client_id"):
        connect(
            "gmail",
            credentials={
                "client_secret_ref": "env://X",
                "oauth_refresh_token_ref": "env://Y",
                "project_id": "p", "topic": "t", "subscription": "s",
            },
            mailbox="me",
        )


def test_connect_gmail_missing_project_id_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="project_id"):
        connect(
            "gmail",
            credentials={
                "client_id": "c", "client_secret_ref": "env://X",
                "oauth_refresh_token_ref": "env://Y",
                "topic": "t", "subscription": "s",
            },
            mailbox="me",
        )


def test_connect_graph_missing_tenant_id_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="tenant_id"):
        connect(
            "graph",
            credentials={
                "client_id": "c", "client_secret_ref": "env://X",
                "namespace": "n", "hub": "h", "tenant_domain": "d",
            },
            mailbox="me@acme.com",
        )
