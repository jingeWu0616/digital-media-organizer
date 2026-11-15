"""Thread-local observations; media services remain usable without a scheduler."""
from contextvars import ContextVar
from contextlib import contextmanager

_observer = ContextVar('media_task_observer', default=None)


@contextmanager
def observing(callback):
    token = _observer.set(callback)
    try:
        yield
    finally:
        _observer.reset(token)


def phase(name):
    callback = _observer.get()
    if callback:
        callback(dict(phase=name, percent=None))


def progress(completed, total):
    callback = _observer.get()
    if callback and total:
        callback(dict(percent=min(100., 100. * completed / total), completed_bytes=completed, total_bytes=total))


def detail(**values):
    callback = _observer.get()
    if callback: callback(values)


def journal(kind, identifier):
    callback = _observer.get()
    if callback:
        callback(dict(journal_type=kind, journal_id=identifier))
