import logging

from api.core.logging_config import CapabilityUrlRedactionFilter, redact_secrets


def test_temporary_file_capability_token_is_redacted_from_text_and_access_args():
    grant_id = "fgrant_" + "a" * 32
    token = "B" * 43
    path = f"/api/tmp-files/{grant_id}/{token}"
    assert redact_secrets(path) == f"/api/tmp-files/{grant_id}/***"

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", path, "1.1", 200),
        None,
    )
    assert CapabilityUrlRedactionFilter().filter(record)
    assert token not in record.getMessage()
    assert f"/api/tmp-files/{grant_id}/***" in record.getMessage()
