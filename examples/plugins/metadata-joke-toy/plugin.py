"""Metadata Joke toy - metadata_provider only, fixed fictional data.

Merge rule demo: the plugin fills gaps only (``None``/empty first-party
fields); anything present first-party wins and is never overwritten.
"""

from infrastructure.plugins.protocols import PluginArtistEnrichment


class MetadataJoke:
    def __init__(self, context):
        self.ctx = context

    async def enrich_artist(self, *, artist_name, mbid=None, timeout=30.0):
        if artist_name != "Test Artist":
            return None  # not mine: merge treats as gap, first-party wins
        return PluginArtistEnrichment(
            biography="Test Artist is a fictional example used by plugin tests.",
            tags=["example", "test-fixture"],
        )

    async def enrich_album(self, *, artist_name, album_title, mbid=None, timeout=30.0):
        return None
