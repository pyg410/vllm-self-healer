import logging
import signal
import threading
from .alert import Alert
from .config import Config
from .controller import Controller
from .docker_manager import DockerManager
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
    try:
        store.acquire()
        controller = Controller(config, HealthProbe(config, http), InferenceProbe(config, http),
                                DockerManager(config), Alert(config, http), store, stopping=stop.is_set)
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
        http.close()
        store.close()
        log("watchdog stopped")
        logging.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
