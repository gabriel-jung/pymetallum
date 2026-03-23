"""Public API surface for the pymetallum core library."""

from .api import AlbumAPI, ArtistAPI, BandAPI, LabelAPI, SongAPI
from .client import MetalArchivesClient, NotFoundError

__all__ = [
    "AlbumAPI",
    "ArtistAPI",
    "BandAPI",
    "LabelAPI",
    "MetalArchivesClient",
    "NotFoundError",
    "SongAPI",
]
