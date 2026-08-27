"""ASGI mock of archive.org (advancedsearch + metadata) mirroring the shapes
the ArchiveRepository reads. House convention: a plain FastAPI app mounted via
httpx ASGITransport - no HTTP-mocking libraries.

Canned items exercise every evidence class the acquisition quality model
consumes: ``Flac``, ``24bit Flac`` (depth-proven hi-res), ``VBR MP3``,
``Ogg Vorbis``, and a dark item whose licence cannot be read.

Run standalone for dev:
    uvicorn backend.tests.mocks.archive_mock:app --port 5032
"""

from typing import Any

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

app = FastAPI(title="archive-mock")

_ITEMS: dict[str, dict[str, Any]] = {
    "jamendo-cd-flac": {
        "metadata": {
            "identifier": "jamendo-cd-flac",
            "title": "Guess Who's a Mess",
            "creator": "Brad Sucks",
            "licenseurl": "https://creativecommons.org/licenses/by-nc-sa/3.0/",
        },
        "files": [
            {"name": f"{i:02d} Song.flac", "format": "Flac", "size": 25_000_000,
             "track": i, "title": f"Song {i}"}
            for i in range(1, 11)
        ],
    },
    "archive-hires-24bit": {
        "metadata": {
            "identifier": "archive-hires-24bit",
            "title": "Guess Who's a Mess",
            "creator": "Brad Sucks",
            "licenseurl": "https://creativecommons.org/licenses/by/4.0/",
        },
        "files": [
            {"name": "01 Album.flac", "format": "24bit Flac", "size": 400_000_000,
             "track": 1, "title": "Album"}
        ],
    },
    "archive-vbr-mp3": {
        "metadata": {
            "identifier": "archive-vbr-mp3",
            "title": "Guess Who's a Mess",
            "creator": "Brad Sucks",
            "licenseurl": "https://creativecommons.org/licenses/by-nc/1.0/",
        },
        "files": [
            {"name": f"{i:02d} Song.mp3", "format": "VBR MP3", "size": 5_000_000,
             "track": i, "title": f"Song {i}"}
            for i in range(1, 11)
        ],
    },
    "archive-ogg-sampler": {
        "metadata": {
            "identifier": "archive-ogg-sampler",
            "title": "Guess Who's a Mess",
            "creator": "Brad Sucks",
            "licenseurl": "https://creativecommons.org/publicdomain/mark/1.0/",
        },
        "files": [
            {"name": "track.ogg", "format": "Ogg Vorbis", "size": 900_000,
             "track": 1, "title": "Sampler"}
        ],
    },
    # A "dark" item: metadata exists but licence can't be resolved.
    "dark-item-no-licence": {
        "metadata": {
            "identifier": "dark-item-no-licence",
            "title": "Guess Who's a Mess",
            "creator": "Unknown",
        },
        "files": [],
    },
}

# Streaming payloads keyed by (identifier, filename).
_STREAMS: dict[tuple[str, str], bytes] = {}


def seed_stream(identifier: str, filename: str, payload: bytes) -> None:
    _STREAMS[(identifier, filename)] = payload


@app.get("/advancedsearch.php")
async def advancedsearch(q: str = "", rows: int = 50):  # noqa: ANN001
    """Mimics the solr-style envelope ``_search`` decodes (output=json,
    response.docs with the four fl[] fields). Only licence-carrying items are
    returned, matching the repository's ``licenseurl:[* TO *]`` clause."""
    docs = [
        {
            "identifier": identifier,
            "title": meta["metadata"]["title"],
            "creator": meta["metadata"].get("creator"),
            "year": meta.get("metadata", {}).get("year"),
            "licenseurl": meta.get("metadata", {}).get("licenseurl"),
        }
        for identifier, meta in sorted(_ITEMS.items())
        if "dark" not in identifier
        and meta.get("metadata", {}).get("licenseurl")
    ]
    return JSONResponse({"response": {"numFound": len(docs), "docs": docs}})


@app.get("/metadata/{identifier}")
async def metadata(identifier: str, response: Response):
    item = _ITEMS.get(identifier)
    if item is None:
        response.status_code = 404
        return {"error": "item not found"}
    return {
        "metadata": item["metadata"],
        "files": item["files"],
    }


@app.get("/download/{identifier}/{filename:path}")
async def download(identifier: str, filename: str):
    key = (identifier, filename)
    if key in _STREAMS:
        return Response(
            content=_STREAMS[key],
            media_type="application/octet-stream",
        )
    item = _ITEMS.get(identifier)
    entry = next((f for f in item["files"] if f["name"] == filename), None) if item else None
    if entry is None:
        return Response(status_code=404)
    return Response(b"AUDIO-BYTES" * 64, media_type="application/octet-stream")


@app.get("/download/{identifier}/{filename}/{extra:path}")
async def download_extra(identifier: str, filename: str, extra: str):
    return Response(status_code=404)
