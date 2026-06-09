from mailflow.core import ports


def test_all_eleven_ports_are_exported():
    expected = {
        "MailboxProvider",
        "SubscriptionManager",
        "EnvelopeParser",
        "Filter",
        "Classifier",
        "ContentExtractor",
        "Emitter",
        "CursorStore",
        "DedupeStore",
        "SecretProvider",
        "BlobStore",
    }
    assert expected <= set(dir(ports))


def test_runtime_checkable_smoke_for_emitter():
    # @runtime_checkable verifies method NAMES only (spec §5.3 / R-D5).
    class FakeEmitter:
        def emit(self, event):  # noqa: ANN001, ANN201
            return None

    assert isinstance(FakeEmitter(), ports.Emitter)
