import json
import logging
import sys

import httpx
import pytest

from src.core.logging import JsonFormatter, RequestIdFilter, request_id_ctx, setup_logging


def test_json_formatter_outputs_single_line_json_with_request_id_and_exception() -> None:
    """RequestIdFilter + JsonFormatter tek satır JSON, request_id ve exception üretmelidir."""
    request_filter = RequestIdFilter()
    formatter = JsonFormatter()
    token = request_id_ctx.set("req-json-42")
    try:
        try:
            raise ValueError("simulated failure")
        except ValueError:
            record = logging.LogRecord(
                name="corerag.test",
                level=logging.ERROR,
                pathname=__file__,
                lineno=1,
                msg="Operation failed for %s",
                args=("doc-1",),
                exc_info=sys.exc_info(),
            )
        assert request_filter.filter(record) is True
        formatted = formatter.format(record)
    finally:
        request_id_ctx.reset(token)

    assert "\n" not in formatted
    payload = json.loads(formatted)
    assert payload["level"] == "ERROR"
    assert payload["logger"] == "corerag.test"
    assert payload["request_id"] == "req-json-42"
    assert payload["message"] == "Operation failed for doc-1"
    assert "ValueError: simulated failure" in payload["exception"]
    assert "T" in payload["timestamp"]


def test_setup_logging_json_mode_and_preserves_pytest_caplog(
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """setup_logging JSON formatını kurmalı ve pytest caplog yakalamasını bozmamalıdır."""
    setup_logging(log_level="DEBUG", log_format="json")
    token = request_id_ctx.set("ctx-json-99")
    try:
        root_logger = logging.getLogger()
        assert any(isinstance(h.formatter, JsonFormatter) for h in root_logger.handlers)
        assert logging.getLogger("uvicorn.access").disabled is True

        test_logger = logging.getLogger("corerag.json_check")
        with caplog.at_level(logging.INFO):
            test_logger.info("hello json logger")

        assert any(record.message == "hello json logger" for record in caplog.records)
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip().splitlines()[-1])
        assert parsed["message"] == "hello json logger"
        assert parsed["request_id"] == "ctx-json-99"
    finally:
        request_id_ctx.reset(token)
        setup_logging(log_level="INFO", log_format="console")


async def test_caplog_still_captures_logs_when_app_fixture_is_active(
    client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """create_app() sonrası RequestIdMiddleware erişim logları caplog tarafından yakalanmalıdır."""
    with caplog.at_level(logging.INFO):
        response = await client.get("/api/v1/health", headers={"X-Request-ID": "caplog-req-1"})

    assert response.status_code == 200
    assert any("GET /api/v1/health -> 200" in record.message for record in caplog.records)
