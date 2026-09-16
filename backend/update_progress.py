"""Request-scoped progress reporting for signed update preparation.

The observer lives in the API; downloads only report measured bytes and phases.
Context isolation keeps an unrelated download or request out of its state.
"""
from contextlib import contextmanager
from contextvars import ContextVar


_observer = ContextVar("update_preparation_observer", default=None)


@contextmanager
def observe_preparation(callback):
    token = _observer.set(callback)
    try:
        yield
    finally:
        _observer.reset(token)


def report_phase(phase):
    callback = _observer.get()
    if callback is not None:
        callback({"phase": phase})


def report_download(downloaded_bytes, total_bytes):
    callback = _observer.get()
    if callback is not None:
        callback({"phase": "downloading", "downloaded_bytes": downloaded_bytes,
                  "total_bytes": total_bytes})
