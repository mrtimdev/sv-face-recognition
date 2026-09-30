"""Keep ``QThread`` objects alive until their threads have really ended.

Destroying a ``QThread`` while its ``run()`` is still executing aborts the
whole process ("QThread: Destroyed while thread is still running").  A worker
that may outlive whatever started it - a dialog that closed, a screen torn
down, a camera probe slower than a timeout - is parked here: C++ takes
ownership (so neither a released Python reference nor interpreter shutdown can
delete it mid-run), and it is deleted with ``deleteLater()`` once finished.
"""
from PyQt6 import sip

_PARKED = set()


def park(thread):
    """Hold *thread* until it has finished; harmless for finished or None threads."""
    if thread is None or thread in _PARKED:
        return
    _PARKED.add(thread)
    sip.transferto(thread, None)

    def release():
        thread.wait()        # ``finished`` is emitted just before the thread exits
        try:
            thread.finished.disconnect(release)
        except TypeError:
            pass
        _PARKED.discard(thread)
        thread.deleteLater()

    thread.finished.connect(release)
    if thread.isFinished():
        release()


def start_detached(thread):
    """Start *thread* with nobody else keeping it; it lives until it ends."""
    park(thread)
    thread.start()


def settle(thread, timeout_ms=2000):
    """Wait briefly for *thread*; if it is still busy, park it rather than block.

    Returns True when the thread has ended.
    """
    if thread is None:
        return True
    try:
        if thread.wait(timeout_ms):
            return True
    except RuntimeError:        # the C++ object is already gone
        return True
    park(thread)
    return False


def is_parked(thread):
    return thread in _PARKED
