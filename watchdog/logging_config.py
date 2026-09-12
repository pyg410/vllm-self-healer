import json
import logging
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


def configure(level):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("watchdog")
    logger.handlers[:] = [handler]
    logger.setLevel(level)
    logger.propagate = False


def log(event, level=logging.INFO, **details):
    logging.getLogger("watchdog").log(level, event, extra={"details": details})
