"""Explicit display-only response profiles and provenance handoff."""

import hashlib
import json
from contextvars import ContextVar
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Awaitable, Generic, TypeVar


class MbCachePolicy(Enum):
    BYPASS = "bypass"
    DISPLAY_FRESH = "display_fresh"
    DISPLAY_STALE = "display_stale"


@dataclass(frozen=True)
class MbResponseMetadata:
    origin: str
    fetched_at: float
    fresh_until: float
    retention_until: float
    source_mode: str
    source_id: str
    generation: int
    clear_epoch: int
    profile: str
    decoder_version: str


response_metadata: ContextVar[MbResponseMetadata | None] = ContextVar("mb_response_metadata", default=None)


def get_mb_response_metadata() -> MbResponseMetadata | None:
    return response_metadata.get()


T = TypeVar("T")


@dataclass(frozen=True)
class MbProjection(Generic[T]):
    value: T
    metadata: MbResponseMetadata | None


async def capture_mb_projection(operation: Awaitable[T]) -> MbProjection[T]:
    response_metadata.set(None)
    value = await operation
    return MbProjection(value, get_mb_response_metadata())


def restore_mb_projection(projection: MbProjection[T]) -> T:
    response_metadata.set(projection.metadata)
    return projection.value


def merge_mb_metadata(*items: MbResponseMetadata | None) -> MbResponseMetadata | None:
    present = [item for item in items if item is not None]
    if not present:
        return None
    first = present[0]
    identity = (first.source_mode, first.source_id, first.generation, first.clear_epoch)
    compatible = all(
        (item.source_mode, item.source_id, item.generation, item.clear_epoch) == identity
        for item in present
    )
    return replace(
        first,
        fetched_at=min(item.fetched_at for item in present),
        fresh_until=min(item.fresh_until for item in present) if compatible else 0,
        retention_until=min(item.retention_until for item in present),
    )


def bound_mb_metadata(metadata: MbResponseMetadata | None, deadline: float) -> MbResponseMetadata | None:
    return replace(metadata, fresh_until=min(metadata.fresh_until, deadline)) if metadata else None


def profile_for(path: str, params: dict[str, Any] | None, decode_type: Any) -> str | None:
    params = params or {}
    includes = frozenset(str(params.get("inc", "")).split("+")) - {""}
    if path.startswith("/artist/") and path.count("/") == 2 and set(params) <= {"inc"} and decode_type is None:
        if includes == {"tags", "aliases", "url-rels"}:
            return "artist-core-v1"
        if includes == {"url-rels"}:
            return "artist-relations-v1"
    if (path == "/release-group" and set(params) == {"artist", "limit", "offset"}
            and getattr(decode_type, "__module__", None) == "repositories.musicbrainz_artist"
            and getattr(decode_type, "__name__", None) == "_ArtistReleaseGroupsPayload"):
        return "artist-rg-page-v1"
    return None


def request_key(path: str, params: dict[str, Any] | None, decode_type: Any, source: Any) -> str:
    normalized = dict(params or {})
    if "inc" in normalized:
        normalized["inc"] = "+".join(sorted(set(normalized["inc"].split("+"))))
    decoder = f"{decode_type.__module__}.{decode_type.__qualname__}" if decode_type else "json-object"
    encoded = json.dumps([source.source_mode, source.source_id, source.generation,
                          path, normalized, decoder, 1], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
