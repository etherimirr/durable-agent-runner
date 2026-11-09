from .store import Claim, LeaseLost, Store

__all__ = ["Claim", "LeaseLost", "Store", "Worker"]


def __getattr__(name):
    # Imported lazily so `python -m dar.worker` does not load the module twice.
    if name == "Worker":
        from .worker import Worker
        return Worker
    raise AttributeError(name)
