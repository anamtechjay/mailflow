"""Service Bus egress smoke test: send one EmailEvent JSON to the configured queue.

  pip install -e ".[servicebus]"
  # set SB_CONNECTION_STRING and SB_QUEUE, then:
  python scripts/check_servicebus_connection.py
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    from azure.servicebus import ServiceBusClient, ServiceBusMessage  # local import

    conn = os.environ.get("SB_CONNECTION_STRING")
    queue = os.environ.get("SB_QUEUE", "mailflow-graph")
    if not conn:
        print("[FAIL] set SB_CONNECTION_STRING (and optionally SB_QUEUE)")
        return 1

    with ServiceBusClient.from_connection_string(conn) as client:
        with client.get_queue_sender(queue) as sender:
            sender.send_messages(
                ServiceBusMessage(
                    b'{"schema_version":"1.3","tenant":"smoke","email":{}}',
                    message_id="smoke-1",
                    content_type="application/json",
                )
            )
    print(f"[OK] sent 1 test message to queue={queue}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
