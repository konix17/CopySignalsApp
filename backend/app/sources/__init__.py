from .base import Source
from .gmx import Gmx
from .hyperliquid import Hyperliquid

REGISTRY: dict[str, type] = {"hyperliquid": Hyperliquid, "gmx": Gmx}


def build_sources(names: tuple[str, ...]) -> list[Source]:
    unknown = set(names) - REGISTRY.keys()
    if unknown:
        raise ValueError(f"Unknown sources {sorted(unknown)}; available: {sorted(REGISTRY)}")
    return [REGISTRY[n]() for n in names]
