import json
import logging
from logging.handlers import RotatingFileHandler
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
            **getattr(record, "details", {}),
        }, ensure_ascii=False)


def configure(level, log_file="", max_bytes=10485760, backup_count=5):
    handlers = [logging.StreamHandler(sys.stdout)]
    handlers[0].setFormatter(JsonFormatter())
    logger = logging.getLogger("watchdog")
    for old in logger.handlers[:]:
        logger.removeHandler(old)
        old.close()
    logger.handlers[:] = handlers
    logger.setLevel(level)
    logger.propagate = False
    logging.raiseExceptions = False
    # Install stdout first: a file-open failure can still be reported safely.
    if log_file:
        handler = RotatingFileHandler(log_file, maxBytes=max_bytes,
                                      backupCount=backup_count, encoding="utf-8")
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)


def log(event, level=logging.INFO, **details):
    logging.getLogger("watchdog").log(level, event, extra={"details": details})


def configuration_loaded(details):
    logger = logging.getLogger("watchdog")
    record = logger.makeRecord("watchdog", logging.INFO, "", 0, "configuration loaded", (), None,
                               extra={"details": details})
    logger.handle(record)


def log_error(event, error, stage):
    # Exception messages and paths may contain admin-configured credentials.
    # Report fixed context and the exception class, never the exception repr.
    log(event, logging.ERROR, error_type=type(error).__name__, stage=stage,
        error_code=error.errno if isinstance(error, OSError) else None)
