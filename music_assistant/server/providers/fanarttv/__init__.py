"""Fanart.tv Metadata provider for Music Assistant."""
from __future__ import annotations

from json import JSONDecodeError
from typing import TYPE_CHECKING

import aiohttp.client_exceptions
from asyncio_throttle import Throttler

from music_assistant.common.models.enums import ProviderFeature
from music_assistant.common.models.media_items import ImageType, MediaItemImage, MediaItemMetadata
from music_assistant.server.controllers.cache import use_cache
from music_assistant.server.helpers.app_vars import app_var  # pylint: disable=no-name-in-module
from music_assistant.server.models.metadata_provider import MetadataProvider

if TYPE_CHECKING:
    from music_assistant.common.models.media_items import Album, Artist

# TODO: add support for personal api keys ?


IMG_MAPPING = {
    "artistthumb": ImageType.THUMB,
    "hdmusiclogo": ImageType.LOGO,
    "musicbanner": ImageType.BANNER,
    "artistbackground": ImageType.FANART,
}


class FanartTvMetadataProvider(MetadataProvider):
    """Fanart.tv Metadata provider."""

    throttler: Throttler

    async def setup(self) -> None:
        """Handle async initialization of the provider."""
        self.cache = self.mass.cache
        self.throttler = Throttler(rate_limit=2, period=1)
        self._attr_supported_features = (
            ProviderFeature.ARTIST_METADATA,
            ProviderFeature.ALBUM_METADATA,
        )

    async def get_artist_metadata(self, artist: Artist) -> MediaItemMetadata | None:
        """Retrieve metadata for artist on fanart.tv."""
        if not artist.musicbrainz_id:
            return None
        self.logger.debug("Fetching metadata for Artist %s on Fanart.tv", artist.name)
        if data := await self._get_data(f"music/{artist.musicbrainz_id}"):
            metadata = MediaItemMetadata()
            metadata.images = []
            for key, img_type in IMG_MAPPING.items():
                items = data.get(key)
                if not items:
                    continue
                for item in items:
                    metadata.images.append(MediaItemImage(img_type, item["url"]))
            return metadata
        return None

    async def get_album_metadata(self, album: Album) -> MediaItemMetadata | None:
        """Retrieve metadata for album on fanart.tv."""
        if not album.musicbrainz_id:
            return None
        self.logger.debug("Fetching metadata for Album %s on Fanart.tv", album.name)
        if data := await self._get_data(f"music/albums/{album.musicbrainz_id}"):  # noqa: SIM102
            if data and data.get("albums"):
                data = data["albums"][album.musicbrainz_id]
                metadata = MediaItemMetadata()
                metadata.images = []
                for key, img_type in IMG_MAPPING.items():
                    items = data.get(key)
                    if not items:
                        continue
                    for item in items:
                        metadata.images.append(MediaItemImage(img_type, item["url"]))
                return metadata
        return None

    @use_cache(86400 * 14)
    async def _get_data(self, endpoint, **kwargs) -> dict | None:
        """Get data from api."""
        url = f"http://webservice.fanart.tv/v3/{endpoint}"
        kwargs["api_key"] = app_var(4)
        async with self.throttler:
            async with self.mass.http_session.get(url, params=kwargs, verify_ssl=False) as response:
                try:
                    result = await response.json()
                except (
                    aiohttp.client_exceptions.ContentTypeError,
                    JSONDecodeError,
                ):
                    self.logger.error("Failed to retrieve %s", endpoint)
                    text_result = await response.text()
                    self.logger.debug(text_result)
                    return None
                except (
                    aiohttp.client_exceptions.ClientConnectorError,
                    aiohttp.client_exceptions.ServerDisconnectedError,
                ):
                    self.logger.warning("Failed to retrieve %s", endpoint)
                    return None
                if "error" in result and "limit" in result["error"]:
                    self.logger.warning(result["error"])
                    return None
                return result
