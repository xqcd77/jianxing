"""Standalone scheduler. Web workers do not each run their own periodic scanner."""

from dataclasses import replace
import argparse
import logging
import time

from .app import create_app
from .config import Settings
from .errors import BankError
from .storage import audit


def main():
    parser = argparse.ArgumentParser(description="简行独立调度器")
    parser.add_argument("--once", action="store_true", help="扫描一次后退出")
    args = parser.parse_args()
    settings = replace(Settings.from_env(), scheduler_mode="disabled", memory_embedding="lexical")
    app = create_app(settings)
    try:
        with app.state.graph.process_locks.hold("scheduler:leader", timeout=0):
            run_loop(app, settings, args.once)
    except KeyboardInterrupt:
        pass
    except BankError as error:
        if error.code == "PLAN_BUSY":
            logging.info("A scheduler is already running for this data directory")
        else:
            raise
    finally:
        app.state.graph.close()
        app.state.memory.close()
        app.state.db.engine.dispose()


def run_loop(app, settings, once):
    while True:
        try:
            app.state.events.tick()
            with app.state.db.write() as session:
                from .storage import save_document, now_iso
                save_document(session, "scheduler_heartbeat", "system_status", "system", {"last_tick_at": now_iso()})
        except Exception as error:
            logging.warning("Scheduler tick failed: %s", type(error).__name__)
            with app.state.db.write() as session:
                audit(session, "system", "SCHEDULER_ERROR", {"error_type": type(error).__name__})
        if once:
            return
        time.sleep(max(1, settings.scheduler_interval))

if __name__ == "__main__":
    main()
