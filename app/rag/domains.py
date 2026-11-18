"""Domain-pack descriptors used to make V3 routing data-driven."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DomainPack:
    """Capabilities a domain exposes to the shared investigation controller."""

    name: str
    description: str
    document_sources: tuple[str, ...]
    supports_sql: bool = False
    supports_upload: bool = True


class DomainRegistry:
    def __init__(self) -> None:
        self._packs: dict[str, DomainPack] = {}

    def register(self, pack: DomainPack) -> None:
        if not pack.name or pack.name in self._packs:
            raise ValueError(f"Duplicate or empty domain: {pack.name!r}")
        self._packs[pack.name] = pack

    def get(self, name: str) -> DomainPack:
        try:
            return self._packs[name]
        except KeyError as exc:
            raise KeyError(f"Unknown domain: {name!r}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(self._packs)

    def describe(self) -> dict[str, dict]:
        return {
            name: {
                "description": pack.description,
                "document_sources": list(pack.document_sources),
                "supports_sql": pack.supports_sql,
                "supports_upload": pack.supports_upload,
            }
            for name, pack in self._packs.items()
        }


def default_registry() -> DomainRegistry:
    registry = DomainRegistry()
    registry.register(DomainPack(
        "policy", "计算政策口径净营收并核对政策、公告与账本引用",
        ("policy", "bulletin"), supports_sql=True,
    ))
    registry.register(DomainPack(
        "manual", "检索产品手册章节并引用指定版本文本",
        ("manual",), supports_sql=False,
    ))
    return registry
