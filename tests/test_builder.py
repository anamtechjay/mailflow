from mailflow.builder import build_from_config
from mailflow.config.schema import MailflowConfig
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail


def test_build_from_config_wires_a_runnable_pipeline():
    cfg = MailflowConfig.model_validate({
        "tenant": "acme",
        "provider": {"kind": "memory"},
        "filters": [{"kind": "blacklist", "params": {"domains": ["spam.com"]}}],
        "emitter": {"kind": "memory"},
    })
    stream = StreamRef(mailbox="ops@acme.com", folder="Inbox")
    seed = {stream: [
        SeedEmail("m1", b"Message-ID: <m1@x>\r\nFrom: a@spam.com\r\nSubject: x\r\n\r\nhi"),
        SeedEmail("m2", b"Message-ID: <m2@x>\r\nFrom: a@partner.com\r\nSubject: y\r\n\r\nhi"),
    ]}
    pipe = build_from_config(cfg, seed=seed)
    report = pipe.run_once()
    assert report.emitted == 1
    assert report.dropped == 1
