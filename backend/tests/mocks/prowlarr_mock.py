"""Realistic Prowlarr JSON mocks, shaped from the inspected devopsarr/prowlarr-py
generated-client docs (``ReleaseResource``/``IndexerResource`` field names).
Served via ``httpx.MockTransport`` so the real ``ProwlarrClient`` JSON path is
exercised end to end.

Hosts (selected by request host):
- ``prowlarr``: healthy instance; ``/search`` returns a mixed feed (one usenet
  FLAC hit, one torrent hit, one minimal usenet hit with no download URL).
- ``prowlarr-torrents``: search returns torrent-only results (all skipped in v1).
- ``prowlarr-nostatus``: ``/system/status`` 404s (A0-unconfirmed endpoint); the
  indexer list still answers, so health degrades instead of failing.
- ``prowlarr-auth``: every endpoint 401 (wrong API key).
- ``prowlarr-ratelimit``: search 429 with ``Retry-After: 5``.
- ``prowlarr-error``: search 500.
- ``prowlarr-html``: search 200 with an HTML body (proxy/login page).

Live-verified against Prowlarr **2.3.5.5327**: ``/system/status`` carries
``version``; ``/search`` honors ``query`` + repeated ``categories``/``indexerIds``
+ ``limit``; ``downloadUrl`` embeds ``?apikey=`` (self-contained for SABnzbd);
``publishDate`` is ISO-8601 with ``Z``; bad key yields 401.
"""

import httpx

_STATUS = '{"version": "1.32.2.4987"}'

_INDEXERS = """[
  {"id": 7, "name": "NZBGeek", "protocol": "usenet", "enable": true},
  {"id": 11, "name": "DrunkenSlug", "protocol": "usenet", "enable": true},
  {"id": 12, "name": "DisabledTracker", "protocol": "torrent", "enable": false}
]"""

_SEARCH_MIXED = """[
  {
    "guid": "usenet-guid-flac-1",
    "title": "Radiohead - In Rainbows (2007) [FLAC]",
    "size": 2315726631,
    "files": 113,
    "grabs": 205,
    "indexerId": 7,
    "indexer": "NZBGeek",
    "categories": [{"id": 3040, "name": "Audio/Lossless"}],
    "downloadUrl": "https://prowlarr.test/9/download?apikey=MOCKKEY&link=ezQxYw",
    "magnetUrl": "",
    "protocol": "usenet",
    "publishDate": "2025-10-23T19:17:23Z"
  },
  {
    "guid": "torrent-guid-1",
    "title": "Radiohead - In Rainbows (2007) [FLAC] [torrent]",
    "size": 400000000,
    "indexerId": 12,
    "indexer": "SomeTracker",
    "categories": [{"id": 3040, "name": "Audio/Lossless"}],
    "downloadUrl": "",
    "magnetUrl": "magnet:?xt=urn:btih:cafe",
    "protocol": "torrent",
    "publishDate": "2025-10-20T10:00:00Z",
    "seeders": 42,
    "leechers": 3
  },
  {
    "guid": "usenet-guid-nourl",
    "title": "Radiohead - In Rainbows (2007) [MP3]",
    "size": 120000000,
    "indexerId": 11,
    "indexer": "DrunkenSlug",
    "categories": [{"id": 3010, "name": "Audio/MP3"}],
    "downloadUrl": "",
    "protocol": "usenet",
    "publishDate": ""
  }
]"""

_SEARCH_TORRENTS = """[
  {
    "guid": "torrent-only-1",
    "title": "Radiohead - In Rainbows (2007) [FLAC] [torrent]",
    "size": 400000000,
    "indexerId": 12,
    "indexer": "SomeTracker",
    "categories": [{"id": 3040, "name": "Audio/Lossless"}],
    "magnetUrl": "magnet:?xt=urn:btih:cafe",
    "protocol": "torrent"
  }
]"""


def _json_response(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, headers={"Content-Type": "application/json"}, content=body.encode()
    )


def prowlarr_handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    path = request.url.path
    if host == "prowlarr-auth":
        return httpx.Response(401, content=b"Unauthorized")
    if path == "/api/v1/system/status":
        if host == "prowlarr-nostatus":
            return httpx.Response(404, content=b"Not Found")
        return _json_response(_STATUS)
    if path == "/api/v1/indexer":
        return _json_response(_INDEXERS)
    if path == "/api/v1/search":
        if host == "prowlarr-ratelimit":
            return httpx.Response(
                429, headers={"Retry-After": "5"}, content=b"Rate limited"
            )
        if host == "prowlarr-error":
            return httpx.Response(500, content=b"Server error")
        if host == "prowlarr-html":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                content=b"<html><body>login</body></html>",
            )
        if host == "prowlarr-torrents":
            return _json_response(_SEARCH_TORRENTS)
        return _json_response(_SEARCH_MIXED)
    return httpx.Response(404, content=b"Not Found")


def client_for(handler=prowlarr_handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))
