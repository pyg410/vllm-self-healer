import logging
import signal
import threading
from .alert import Alert
from .config import Config
from .controller import Controller
from .recovery import create_recovery
from .probe_server import ProbeServer, ProbeStatus
from .health import HealthProbe
from .http_client import HttpClient
from .inference import InferenceProbe
from .logging_config import configure, log
from .state import StateStore


def main():
    configure("INFO")
    try:
        config = Config.from_env()
    except ValueError:
        log("invalid configuration; check environment variables", logging.ERROR)
        return 2
    configure(config.log_level)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    store = StateStore(config.state_file)
    http = HttpClient()
    controller = None
    server = None
    try:
        store.acquire()
        recovery = create_recovery(config)
        status = ProbeStatus(recovery) if config.recovery_mode == "kubernetes" else None
        controller = Controller(config, HealthProbe(config, http), InferenceProbe(config, http),
                                recovery, Alert(config, http), store, stopping=stop.is_set, status=status)
        if status is not None:
            server = ProbeServer(config.watchdog_http_host, config.watchdog_http_port, status)
            server.start()
        while not stop.is_set():
            controller.step()
            stop.wait(controller.delay())
        return 0
    except Exception:
        log("watchdog startup failed", logging.ERROR)
        return 1
    finally:
        if controller:
            try:
                controller.persist()
            except Exception:
                log("state persistence failed on shutdown", logging.ERROR)
        if server:
            server.close()
        http.close()
        store.close()
        log("watchdog stopped")
        logging.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
