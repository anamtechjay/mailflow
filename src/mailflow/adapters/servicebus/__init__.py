"""Azure Service Bus adapter — egress Emitter + Graph ingress via Event Grid Partner Topic.

Tasks B1/B2 introduce ServiceBusConfig and ServiceBusEmitter; task C1 adds
parse_eventgrid_message; task D1 adds ServiceBusRuntime (per-message ack runtime).
"""

from mailflow.adapters.servicebus.config import ServiceBusConfig
from mailflow.adapters.servicebus.emitter import ServiceBusEmitter
from mailflow.adapters.servicebus.eventgrid import parse_eventgrid_message
from mailflow.adapters.servicebus.runtime import ServiceBusRuntime

__all__ = [
    "ServiceBusConfig",
    "ServiceBusEmitter",
    "parse_eventgrid_message",
    "ServiceBusRuntime",
]
