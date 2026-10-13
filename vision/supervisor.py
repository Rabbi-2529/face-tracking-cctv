"""
Vision supervisor: forks one worker process per camera and keeps them alive.

One process per camera rather than one thread per camera, because a hung RTSP
read or a CUDA out-of-memory inside a decoder takes the whole interpreter with
it. Isolated per camera, one bad stream costs you one stream.

Phase 1 ships this supervisor plus ``MOCK_VISION=1``, so the realtime path is
exercisable now. Phase 2 fills in :mod:`vision.runner` with the real
detect / embed / track / match pipeline; the supervisor does not change.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import signal
import sys
import time

from vision.config import settings

logger = logging.getLogger("vision.supervisor")

# How long to wait before restarting a worker that died, and how many times
# to tolerate it before backing off hard.
RESTART_DELAY_SECONDS = 5
RESTART_BACKOFF_MAX = 120
CAMERA_RELOAD_SECONDS = 60


class Supervisor:
    def __init__(self) -> None:
        self.procs: dict[str, mp.Process] = {}
        self.failures: dict[str, int] = {}
        self.next_restart: dict[str, float] = {}
        self._stop = False

    def install_signal_handlers(self) -> None:
        def _handle(signum, _frame):
            logger.info("signal %s received, stopping workers", signum)
            self._stop = True

        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)

    # ------------------------------------------------------------- workers

    def _spawn(self, spec) -> None:
        from vision.runner import run_camera

        proc = mp.Process(
            target=run_camera,
            args=(spec,),
            name=f"cam-{spec.code}",
            daemon=False,
        )
        proc.start()
        self.procs[spec.code] = proc
        logger.info("started worker for %s (pid=%s)", spec.code, proc.pid)

    def _reap(self, specs_by_code: dict) -> None:
        """Restart dead workers with exponential backoff."""
        now = time.monotonic()
        for code, proc in list(self.procs.items()):
            if proc.is_alive():
                continue

            exitcode = proc.exitcode
            del self.procs[code]

            if code not in specs_by_code:
                logger.info("worker %s exited and the camera is gone; not restarting", code)
                continue

            self.failures[code] = self.failures.get(code, 0) + 1
            delay = min(
                RESTART_DELAY_SECONDS * (2 ** (self.failures[code] - 1)),
                RESTART_BACKOFF_MAX,
            )
            self.next_restart[code] = now + delay
            logger.warning(
                "worker %s exited (code=%s, failure #%s); restarting in %ss",
                code,
                exitcode,
                self.failures[code],
                delay,
            )

        for code, when in list(self.next_restart.items()):
            if now >= when and code in specs_by_code and code not in self.procs:
                del self.next_restart[code]
                self._spawn(specs_by_code[code])

    def _stop_all(self) -> None:
        for code, proc in self.procs.items():
            if proc.is_alive():
                logger.info("terminating worker %s", code)
                proc.terminate()
        deadline = time.monotonic() + 15
        for proc in self.procs.values():
            remaining = max(0.0, deadline - time.monotonic())
            proc.join(timeout=remaining)
            if proc.is_alive():
                logger.warning("worker %s did not stop; killing", proc.name)
                proc.kill()

    # ----------------------------------------------------------------- run

    def run(self) -> None:
        self.install_signal_handlers()
        from vision.gallery import load_cameras

        last_reload = 0.0
        specs_by_code: dict = {}

        while not self._stop:
            now = time.monotonic()

            if now - last_reload > CAMERA_RELOAD_SECONDS:
                try:
                    specs = load_cameras()
                    specs_by_code = {s.code: s for s in specs}
                    last_reload = now
                    logger.info(
                        "camera list: %s camera(s) for shard %s/%s",
                        len(specs_by_code),
                        settings.shard_index,
                        settings.shard_count,
                    )
                except Exception:
                    logger.exception("could not load the camera list; retrying")
                    time.sleep(10)
                    continue

                # Stop workers whose camera was disabled, deleted, or whose
                # zone has since been marked tracking-prohibited.
                for code in list(self.procs):
                    if code not in specs_by_code:
                        logger.info("camera %s no longer eligible; stopping worker", code)
                        proc = self.procs.pop(code)
                        proc.terminate()
                        proc.join(timeout=10)

                # Start workers for new cameras.
                for code, spec in specs_by_code.items():
                    if code not in self.procs and code not in self.next_restart:
                        self._spawn(spec)

                if not specs_by_code:
                    logger.warning(
                        "no eligible cameras. Add cameras in the admin, or run "
                        "`manage.py seed_demo`, then this will pick them up."
                    )

            self._reap(specs_by_code)
            time.sleep(1)

        self._stop_all()
        logger.info("supervisor stopped")


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(levelname)s %(asctime)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    if settings.mock:
        # Mock mode replaces the whole camera tier with one synthetic walker.
        from vision.mock import MockWalker

        logger.warning("MOCK_VISION=1 - running the synthetic movement generator")
        MockWalker().run()
        return

    Supervisor().run()


if __name__ == "__main__":
    # 'spawn' keeps worker processes free of inherited CUDA/driver state,
    # which otherwise breaks GPU inference after a fork.
    mp.set_start_method("spawn", force=True)
    main()
