import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Literal

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    """Her log kaydına aktif asenkron bağlamdan request_id değerini ekler."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        return True


class JsonFormatter(logging.Formatter):
    """Üretim ortamı için tek satırlık yapılandırılmış JSON log formatlayıcısı."""

    def format(self, record: logging.LogRecord) -> str:
        log_payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_payload, ensure_ascii=False)


def setup_logging(
    log_level: str = "INFO",
    log_format: Literal["console", "json"] = "console",
) -> None:
    """Uygulama genelindeki kök logger ve kütüphane log seviyelerini yapılandırır."""
    normalized_level = log_level.upper()
    root_logger = logging.getLogger()
    root_logger.setLevel(normalized_level)
    root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestIdFilter())

    if log_format == "json":
        formatter: logging.Formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(request_id)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    handler.setFormatter(formatter)
    root_logger.addHandler(handler)

    # Dış kütüphanelerin aşırı gürültülü loglarını dengede tut
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # Uvicorn loglarını da tek formata (request_id + JSON) bağla; access log'u middleware basar.
    for uvicorn_logger_name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(uvicorn_logger_name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
        uvicorn_logger.setLevel(normalized_level)

    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.propagate = False
    access_logger.disabled = True
