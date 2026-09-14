import logging
import signal
import threading
from .alert import Alert
from .config import Config
from .controller import Controller
from .recovery import create_recovery
from .probe_server import ProbeServer, ProbeStatus
from .health import HealthProbe
from .hooks import LifecycleHooks
from .http_client import HttpClient
from .inference import InferenceProbe
from .logging_config import configure, log, log_error, configuration_loaded
from .state import StateStore
from .webhooks import EventWebhook


def main():
    configure("INFO")
    try:
        config = Config.from_env()
    except ValueError as error:
        # All Config validation summaries contain setting names, not values.
        log("invalid configuration; check environment variables", logging.ERROR,
            error_type=type(error).__name__, error_summary=str(error))
        return 2
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    store = None
    http = None
    controller = None
    server = None
    stage = "logging_setup"
    try:
        configure(config.log_level, config.log_file, config.log_max_bytes, config.log_backup_count)
        configuration_loaded(config.effective())
        stage = "http_client_setup"
        http = HttpClient()
        stage = "state_directory_and_lock"
        store = StateStore(config.state_file)
        store.acquire()
        stage = "controller_initialization"
        recovery = create_recovery(config)
        status = ProbeStatus(recovery) if config.recovery_mode == "kubernetes" else None
        controller = Controller(config, HealthProbe(config, http), InferenceProbe(config, http),
                                recovery, Alert(config, http), store, stopping=stop.is_set, status=status,
                                hooks=LifecycleHooks(config, http), events=EventWebhook(config, http))
        if status is not None:
            stage = "probe_server_bind"
            server = ProbeServer(config.watchdog_http_host, config.watchdog_http_port, status)
            server.start()
        stage = "control_loop"
        while not stop.is_set():
            controller.step()
            stop.wait(controller.delay())
        return 0
    except Exception as error:
        log_error("watchdog startup failed" if stage != "control_loop" else "watchdog execution failed",
                  error, stage)
        return 1
    finally:
        if controller:
            try:
                controller.persist()
            except Exception as error:
                log_error("state persistence failed on shutdown", error, "shutdown")
        if server:
            server.close()
        if http:
            http.close()
        if store:
            store.close()
        log("watchdog stopped")
        logging.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
