"""Progress messages without logging document text, payloads, or credentials."""

import sys
import threading
import time
from contextlib import contextmanager


def report(message):
    print(
        f"[document_forge {time.strftime('%H:%M:%S')}] {message}",
        file=sys.stderr,
        flush=True,
    )


@contextmanager
def waiting(label, interval=15):
    """Heartbeat means a call is pending, not that the remote model is progressing."""
    started = time.monotonic()
    stop = threading.Event()

    def heartbeat():
        while not stop.wait(interval):
            report(
                f"{label}: still waiting for provider ({time.monotonic() - started:.0f}s elapsed)"
            )

    report(f"{label}: requesting provider")
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield
    except BaseException:
        report(
            f"{label}: failed or interrupted after {time.monotonic() - started:.1f}s"
        )
        raise
    else:
        report(f"{label}: provider returned in {time.monotonic() - started:.1f}s")
    finally:
        stop.set()
        thread.join()
