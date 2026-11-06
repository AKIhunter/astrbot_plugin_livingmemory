"""轻量请求级耗时；只记录指标，不采集正文、身份或改变检索范围。"""
from contextlib import contextmanager
from contextvars import ContextVar
import time

_collector = ContextVar("livingmemory_request_timing", default=None)


@contextmanager
def capture():
    metrics = {}
    token = _collector.set(metrics)
    try:
        yield metrics
    finally:
        _collector.reset(token)


@contextmanager
def measure(capability):
    metrics = _collector.get()
    if metrics is None:
        yield
        return
    started = time.perf_counter()
    status = "ok"
    try:
        yield
    except BaseException as exc:
        status = "cancelled" if type(exc).__name__ == "CancelledError" else "error"
        raise
    finally:
        metric = metrics.setdefault(capability, {"ms": 0, "calls": 0, "status": "ok"})
        metric["ms"] = round(metric["ms"] + (time.perf_counter() - started) * 1000, 2)
        metric["calls"] += 1
        # 保留已发生异常，后续成功子调用不能把它抹掉。
        if metric["status"] == "ok":
            metric["status"] = status
