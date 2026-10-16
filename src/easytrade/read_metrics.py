"""Local read timing only; no account data, screenshots or UI actions."""
from functools import wraps
from pathlib import Path
from time import perf_counter


def read_operation(name):
    def decorate(method):
        @wraps(method)
        def run(self, *args, **kwargs):
            previous = getattr(self, "_read_metrics", None)
            if previous is not None or not self.profile.get("console_diagnostics"):
                return method(self, *args, **kwargs)
            metrics = {"operation": name, "steps": {}}
            self._read_metrics = metrics
            started, success = perf_counter(), False
            try:
                result = method(self, *args, **kwargs)
                success = True
                return result
            finally:
                self._read_metrics = previous
                from .diagnostics import record_action
                try:
                    record_action(Path(self.profile["console_diagnostics"]).parent.parent,
                                  "native_read_timing", operation=name, success=success,
                                  elapsed_ms=round((perf_counter()-started)*1000, 1), steps=metrics["steps"])
                except OSError:
                    pass  # Optional timing must not change a read result/error.
        return run
    return decorate


def read_step(name):
    def decorate(method):
        @wraps(method)
        def run(self, *args, **kwargs):
            return measure(self, name, lambda: method(self, *args, **kwargs))
        return run
    return decorate


def measure(adapter, name, call):
    metrics = getattr(adapter, "_read_metrics", None)
    if metrics is None:
        return call()
    started = perf_counter()
    try:
        return call()
    finally:
        step = metrics["steps"].setdefault(name, {"count": 0, "elapsed_ms": 0})
        step["count"] += 1
        step["elapsed_ms"] = round(step["elapsed_ms"] + (perf_counter()-started)*1000, 2)
