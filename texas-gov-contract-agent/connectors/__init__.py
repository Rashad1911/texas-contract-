"""Procurement source connectors. `all_connectors(config)` returns every enabled source."""
from core.config import Config

from . import austin, dallas, houston, san_antonio, sam, texas, vehicles


def all_connectors(config: Config):
    out = []
    for module in (sam, vehicles, dallas, houston, austin, san_antonio, texas):
        out.extend(module.build(config))
    return out
