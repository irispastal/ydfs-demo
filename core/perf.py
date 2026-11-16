"""Filesystem diagnostics. Durations are inclusive; nested stages overlap.

INFO records directory opens, HTTP pages and slow attribute queries. DEBUG also
records every attribute query. Upload stages emit start/end events, byte counts
and retry attempts. No paths, credentials, URLs or bodies are logged.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from itertools import count
from hashlib import sha256
import json
import logging
import os
from time import perf_counter

logger = logging.getLogger("ydfs.perf")
current = ContextVar("directory_trace", default=None)
sequence = count(1)


def emit(event, **fields):
    trace = current.get()
    if trace is not None:
        logger.info("[%s] %s", trace.get("log_prefix", "directory-perf"), json.dumps(
            dict(event=event, pid=os.getpid(), trace=trace["trace"], **fields), separators=(",", ":")))


def increment(name, value=1):
    trace = current.get()
    if trace is not None:
        counters = trace["counts"]
        counters[name] = counters.get(name, 0) + value


@contextmanager
def stage(name):
    trace = current.get()
    if trace is None:
        yield
        return
    start = perf_counter()
    try:
        yield
    finally:
        stages = trace["stages_ms"]
        stages[name] = stages.get(name, 0) + (perf_counter() - start) * 1000


@contextmanager
def timed_stage(name, **fields):
    """Emit both boundaries so an unfinished network request remains visible."""
    start = perf_counter()
    emit(name, phase="start", **fields)
    status, code = "ok", None
    try:
        with stage(name):
            yield
    except BaseException as error:
        status = "error"
        # CloudError codes are safe; exception messages can contain URLs/tokens.
        code = getattr(error, "code", None)
        raise
    finally:
        emit(name, phase="end", status=status, code=code,
             elapsed_ms=round((perf_counter() - start) * 1000, 3), **fields)


def traced(operation, quiet=False, prefix="directory-perf"):
    def decorate(method):
        @wraps(method)
        def invoke(*args, **kwargs):
            if current.get() is not None or not logger.isEnabledFor(logging.INFO):
                return method(*args, **kwargs)
            trace = dict(event="operation", pid=os.getpid(), trace=next(sequence), operation=operation,
                         stages_ms={}, counts={}, status="ok", log_prefix=prefix)
            if len(args) > 1 and isinstance(args[1], str):
                trace["target"] = sha256(args[1].encode("utf-8", "surrogateescape")).hexdigest()[:12]
            token = current.set(trace)
            start = perf_counter()
            try:
                return method(*args, **kwargs)
            except BaseException:
                trace["status"] = "error"
                raise
            finally:
                trace["total_ms"] = round((perf_counter() - start) * 1000, 3)
                trace["stages_ms"] = {key: round(value, 3)
                                      for key, value in trace["stages_ms"].items()}
                current.reset(token)
                log_prefix = trace.pop("log_prefix")
                level = logging.DEBUG if quiet and trace["total_ms"] < 50 else logging.INFO
                if logger.isEnabledFor(level):
                    logger.log(level, "[%s] %s", log_prefix, json.dumps(trace, separators=(",", ":")))
        return invoke
    return decorate
