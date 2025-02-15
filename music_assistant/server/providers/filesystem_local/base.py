"""Filesystem musicprovider support for MusicAssistant."""
from __future__ import annotations

import asyncio
import contextlib
import os
from abc import abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from time import time

import xmltodict

from music_assistant.common.helpers.util import parse_title_and_version
from music_assistant.common.models.enums import ProviderFeature
from music_assistant.common.models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant.common.models.media_items import (
    Album,
    AlbumType,
    Artist,
    BrowseFolder,
    ContentType,
    ImageType,
    MediaItemImage,
    MediaItemType,
    MediaType,
    Playlist,
    ProviderMapping,
    Radio,
    StreamDetails,
    Track,
)
from music_assistant.constants import SCHEMA_VERSION, VARIOUS_ARTISTS, VARIOUS_ARTISTS_ID
from music_assistant.server.helpers.compare import compare_strings
from music_assistant.server.helpers.playlists import parse_m3u, parse_pls
from music_assistant.server.helpers.tags import parse_tags, split_items
from music_assistant.server.models.music_provider import MusicProvider

from .helpers import get_parentdir

TRACK_EXTENSIONS = ("mp3", "m4a", "mp4", "flac", "wav", "ogg", "aiff", "wma", "dsf")
PLAYLIST_EXTENSIONS = ("m3u", "pls")
SUPPORTED_EXTENSIONS = TRACK_EXTENSIONS + PLAYLIST_EXTENSIONS
IMAGE_EXTENSIONS = ("jpg", "jpeg", "JPG", "JPEG", "png", "PNG", "gif", "GIF")


@dataclass
class FileSystemItem:
    """Representation of an item (file or directory) on the filesystem.

    - name: Name (not path) of the file (or directory).
    - path: Relative path to the item on this filesystem provider.
    - absolute_path: Absolute (provider dependent) path to this item.
    - is_file: Boolean if item is file (not directory or symlink).
    - is_dir: Boolean if item is directory (not file).
    - checksum: Checksum for this path (usually last modified time).
    - file_size : File size in number of bytes or None if unknown (or not a file).
    - local_path: Optional local accessible path to this (file)item, supported by ffmpeg.
    """

    name: str
    path: str
    absolute_path: str
    is_file: bool
    is_dir: bool
    checksum: str
    file_size: int | None = None
    local_path: str | None = None

    @property
    def ext(self) -> str | None:
        """Return file extension."""
        try:
            return self.name.rsplit(".", 1)[1]
        except IndexError:
            return None


class FileSystemProviderBase(MusicProvider):
    """Base Implementation of a musicprovider for files.

    Reads ID3 tags from file and falls back to parsing filename.
    Optionally reads metadata from nfo files and images in folder structure <artist>/<album>.
    Supports m3u files only for playlists.
    Supports having URI's from streaming providers within m3u playlist.
    """

    _attr_supported_features = (
        ProviderFeature.LIBRARY_ARTISTS,
        ProviderFeature.LIBRARY_ALBUMS,
        ProviderFeature.LIBRARY_TRACKS,
        ProviderFeature.LIBRARY_PLAYLISTS,
        ProviderFeature.PLAYLIST_TRACKS_EDIT,
        ProviderFeature.PLAYLIST_CREATE,
        ProviderFeature.BROWSE,
        ProviderFeature.SEARCH,
    )

    @abstractmethod
    async def setup(self) -> None:
        """Handle async initialization of the provider."""

    @abstractmethod
    async def listdir(
        self, path: str, recursive: bool = False
    ) -> AsyncGenerator[FileSystemItem, None]:
        """List contents of a given provider directory/path.

        Parameters
        ----------
            - path: path of the directory (relative or absolute) to list contents of.
              Empty string for provider's root.
            - recursive: If True will recursively keep unwrapping subdirectories (scandir equivalent).

        Returns:
        -------
            AsyncGenerator yielding FileSystemItem objects.

        """

    @abstractmethod
    async def resolve(self, file_path: str) -> FileSystemItem:
        """Resolve (absolute or relative) path to FileSystemItem."""

    @abstractmethod
    async def exists(self, file_path: str) -> bool:
        """Return bool is this FileSystem musicprovider has given file/dir."""

    @abstractmethod
    async def read_file_content(self, file_path: str, seek: int = 0) -> AsyncGenerator[bytes, None]:
        """Yield (binary) contents of file in chunks of bytes."""

    @abstractmethod
    async def write_file_content(self, file_path: str, data: bytes) -> None:
        """Write entire file content as bytes (e.g. for playlists)."""

    ##############################################
    # DEFAULT/GENERIC IMPLEMENTATION BELOW
    # should normally not be needed to override

    async def search(
        self, search_query: str, media_types=list[MediaType] | None, limit: int = 5  # noqa: ARG002
    ) -> list[MediaItemType]:
        """Perform search on this file based musicprovider."""
        result: list[MediaItemType] = []
        # searching the filesystem is slow and unreliable,
        # instead we make some (slow) freaking queries to the db ;-)
        params = {
            "name": f"%{search_query}%",
            "provider_instance": f"%{self.instance_id}%",
        }
        # ruff: noqa: E501
        if media_types is None or MediaType.TRACK in media_types:
            query = "SELECT * FROM tracks WHERE name LIKE :name AND provider_mappings LIKE :provider_instance"
            tracks = await self.mass.music.tracks.get_db_items_by_query(query, params)
            result += tracks
        if media_types is None or MediaType.ALBUM in media_types:
            query = "SELECT * FROM albums WHERE name LIKE :name AND provider_mappings LIKE :provider_instance"
            albums = await self.mass.music.albums.get_db_items_by_query(query, params)
            result += albums
        if media_types is None or MediaType.ARTIST in media_types:
            query = "SELECT * FROM artists WHERE name LIKE :name AND provider_mappings LIKE :provider_instance"
            artists = await self.mass.music.artists.get_db_items_by_query(query, params)
            result += artists
        if media_types is None or MediaType.PLAYLIST in media_types:
            query = "SELECT * FROM playlists WHERE name LIKE :name AND provider_mappings LIKE :provider_instance"
            playlists = await self.mass.music.playlists.get_db_items_by_query(query, params)
            result += playlists
        return result

    async def browse(self, path: str) -> BrowseFolder:
        """Browse this provider's items.

        :param path: The path to browse, (e.g. provid://artists).
        """
        _, item_path = path.split("://")
        if not item_path:
            item_path = ""
        subitems = []
        async for item in self.listdir(item_path, recursive=False):
            if item.is_dir:
                subitems.append(
                    BrowseFolder(
                        item_id=item.path,
                        provider=self.domain,
                        path=f"{self.instance_id}://{item.path}",
                        name=item.name,
                    )
                )
                continue

            if "." not in item.name or not item.ext:
                # skip system files and files without extension
                continue

            if item.ext in TRACK_EXTENSIONS:
                if db_item := await self.mass.music.tracks.get_db_item_by_prov_id(
                    item.path, provider_instance=self.instance_id
                ):
                    subitems.append(db_item)
                elif track := await self.get_track(item.path):
                    # make sure that the item exists
                    # https://github.com/music-assistant/hass-music-assistant/issues/707
                    db_item = await self.mass.music.tracks.add_db_item(track)
                    subitems.append(db_item)
                continue
            if item.ext in PLAYLIST_EXTENSIONS:
                if db_item := await self.mass.music.playlists.get_db_item_by_prov_id(
                    item.path, provider_instance=self.instance_id
                ):
                    subitems.append(db_item)
                elif playlist := await self.get_playlist(item.path):
                    # make sure that the item exists
                    # https://github.com/music-assistant/hass-music-assistant/issues/707
                    db_item = await self.mass.music.playlists.add_db_item(playlist)
                    subitems.append(db_item)
                continue

        return BrowseFolder(
            item_id=item_path,
            provider=self.domain,
            path=path,
            name=item_path or self.name,
            # make sure to sort the resulting listing
            items=sorted(subitems, key=lambda x: (x.name.casefold(), x.name)),
        )

    async def sync_library(
        self, media_types: tuple[MediaType, ...] | None = None  # noqa: ARG002
    ) -> None:
        """Run library sync for this provider."""
        cache_key = f"{self.instance_id}.checksums"
        prev_checksums = await self.mass.cache.get(cache_key, SCHEMA_VERSION)
        save_checksum_interval = 0
        if prev_checksums is None:
            prev_checksums = {}

        # find all music files in the music directory and all subfolders
        # we work bottom up, as-in we derive all info from the tracks
        cur_checksums = {}
        async for item in self.listdir("", recursive=True):
            if "." not in item.name or not item.ext:
                # skip system files and files without extension
                continue

            if item.ext not in SUPPORTED_EXTENSIONS:
                # unsupported file extension
                continue

            try:
                cur_checksums[item.path] = item.checksum
                if item.checksum == prev_checksums.get(item.path):
                    continue

                if item.ext in TRACK_EXTENSIONS:
                    # add/update track to db
                    track = await self.get_track(item.path)
                    # if the track was edited on disk, always overwrite existing db details
                    overwrite_existing = item.path in prev_checksums
                    await self.mass.music.tracks.add_db_item(
                        track, overwrite_existing=overwrite_existing
                    )
                elif item.ext in PLAYLIST_EXTENSIONS:
                    playlist = await self.get_playlist(item.path)
                    # add/update] playlist to db
                    playlist.metadata.checksum = item.checksum
                    # playlist is always in-library
                    playlist.in_library = True
                    await self.mass.music.playlists.add_db_item(playlist)
            except Exception as err:  # pylint: disable=broad-except
                # we don't want the whole sync to crash on one file so we catch all exceptions here
                self.logger.exception("Error processing %s - %s", item.path, str(err))

            # save checksums every 100 processed items
            # this allows us to pickup where we leftoff when initial scan gets interrupted
            if save_checksum_interval == 100:
                await self.mass.cache.set(cache_key, cur_checksums, SCHEMA_VERSION)
                save_checksum_interval = 0
            else:
                save_checksum_interval += 1

        # store (final) checksums in cache
        await self.mass.cache.set(cache_key, cur_checksums, SCHEMA_VERSION)
        # work out deletions
        deleted_files = set(prev_checksums.keys()) - set(cur_checksums.keys())
        await self._process_deletions(deleted_files)

    async def _process_deletions(self, deleted_files: set[str]) -> None:
        """Process all deletions."""
        # process deleted tracks/playlists
        for file_path in deleted_files:
            _, ext = file_path.rsplit(".", 1)
            if ext not in SUPPORTED_EXTENSIONS:
                # unsupported file extension
                continue

            if ext in PLAYLIST_EXTENSIONS:
                controller = self.mass.music.get_controller(MediaType.PLAYLIST)
            else:
                controller = self.mass.music.get_controller(MediaType.TRACK)

            if db_item := await controller.get_db_item_by_prov_id(
                file_path, provider_instance=self.instance_id
            ):
                await controller.remove_prov_mapping(db_item.item_id, self.instance_id)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        db_artist = await self.mass.music.artists.get_db_item_by_prov_id(
            item_id=prov_artist_id, provider_instance=self.instance_id
        )
        if db_artist is None:
            raise MediaNotFoundError(f"Artist not found: {prov_artist_id}")
        if await self.exists(prov_artist_id):
            # if path exists on disk allow parsing full details to allow refresh of metadata
            return await self._parse_artist(db_artist.name, artist_path=prov_artist_id)
        return db_artist

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        db_album = await self.mass.music.albums.get_db_item_by_prov_id(
            item_id=prov_album_id, provider_instance=self.instance_id
        )
        if db_album is None:
            raise MediaNotFoundError(f"Album not found: {prov_album_id}")
        if await self.exists(prov_album_id):
            # if path exists on disk allow parsing full details to allow refresh of metadata
            return await self._parse_album(db_album.name, prov_album_id, db_album.artists)
        return db_album

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        # ruff: noqa: PLR0915, PLR0912
        if not await self.exists(prov_track_id):
            raise MediaNotFoundError(f"Track path does not exist: {prov_track_id}")

        file_item = await self.resolve(prov_track_id)

        # parse tags
        input_file = file_item.local_path or self.read_file_content(file_item.absolute_path)
        tags = await parse_tags(input_file)

        name, version = parse_title_and_version(tags.title)
        track = Track(
            item_id=file_item.path,
            provider=self.domain,
            name=name,
            version=version,
        )

        # album
        if tags.album:
            # work out if we have an album folder
            album_dir = get_parentdir(file_item.path, tags.album)

            # album artist(s)
            if tags.album_artists:
                album_artists = []
                for index, album_artist_str in enumerate(tags.album_artists):
                    # work out if we have an artist folder
                    artist_dir = get_parentdir(file_item.path, album_artist_str)
                    artist = await self._parse_artist(album_artist_str, artist_path=artist_dir)
                    if not artist.musicbrainz_id:
                        with contextlib.suppress(IndexError):
                            artist.musicbrainz_id = tags.musicbrainz_albumartistids[index]
                    album_artists.append(artist)
            else:
                # always fallback to various artists as album artist if user did not tag album artist
                # ID3 tag properly because we must have an album artist
                self.logger.warning(
                    "%s is missing ID3 tag [albumartist], using %s as fallback",
                    file_item.path,
                    VARIOUS_ARTISTS,
                )
                album_artists = [await self._parse_artist(name=VARIOUS_ARTISTS)]

            track.album = await self._parse_album(
                tags.album,
                album_dir,
                artists=album_artists,
            )
        else:
            self.logger.warning("%s is missing ID3 tag [album]", file_item.path)

        # track artist(s)
        for index, track_artist_str in enumerate(tags.artists):
            # re-use album artist details if possible
            if track.album and (
                artist := next((x for x in track.album.artists if x.name == track_artist_str), None)
            ):
                track.artists.append(artist)
                continue
            artist = await self._parse_artist(track_artist_str)
            if not artist.musicbrainz_id:
                with contextlib.suppress(IndexError):
                    artist.musicbrainz_id = tags.musicbrainz_artistids[index]
            track.artists.append(artist)

        # cover image - prefer album image, fallback to embedded
        if track.album and track.album.image:
            track.metadata.images = [track.album.image]
        elif tags.has_cover_image:
            # we do not actually embed the image in the metadata because that would consume too
            # much space and bandwidth. Instead we set the filename as value so the image can
            # be retrieved later in realtime.
            track.metadata.images = [MediaItemImage(ImageType.THUMB, file_item.path, True)]
            if track.album:
                # set embedded cover on album
                track.album.metadata.images = track.metadata.images

        # parse other info
        track.duration = tags.duration or 0
        track.metadata.genres = tags.genres
        track.disc_number = tags.disc
        track.track_number = tags.track
        track.isrc = tags.get("isrc")
        track.metadata.copyright = tags.get("copyright")
        track.metadata.lyrics = tags.get("lyrics")
        track.musicbrainz_id = tags.musicbrainz_trackid
        if track.album:
            if not track.album.musicbrainz_id:
                track.album.musicbrainz_id = tags.musicbrainz_releasegroupid
            if not track.album.year:
                track.album.year = tags.year
            if not track.album.upc:
                track.album.upc = tags.get("barcode")
        # try to parse albumtype
        if track.album and track.album.album_type == AlbumType.UNKNOWN:
            album_type = tags.album_type
            if album_type and "compilation" in album_type:
                track.album.album_type = AlbumType.COMPILATION
            elif album_type and "single" in album_type:
                track.album.album_type = AlbumType.SINGLE
            elif album_type and "album" in album_type:
                track.album.album_type = AlbumType.ALBUM
            elif track.album.sort_name in track.sort_name:
                track.album.album_type = AlbumType.SINGLE

        # set checksum to invalidate any cached listings
        checksum_timestamp = str(int(time()))
        track.metadata.checksum = checksum_timestamp
        if track.album:
            track.album.metadata.checksum = checksum_timestamp
            for artist in track.album.artists:
                artist.metadata.checksum = checksum_timestamp

        track.add_provider_mapping(
            ProviderMapping(
                item_id=file_item.path,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                content_type=ContentType.try_parse(tags.format),
                sample_rate=tags.sample_rate,
                bit_depth=tags.bits_per_sample,
                bit_rate=tags.bit_rate,
            )
        )
        return track

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        if not await self.exists(prov_playlist_id):
            raise MediaNotFoundError(f"Playlist path does not exist: {prov_playlist_id}")

        file_item = await self.resolve(prov_playlist_id)
        playlist = Playlist(file_item.path, provider=self.domain, name=file_item.name)
        playlist.is_editable = file_item.ext != "pls"  # can only edit m3u playlists

        playlist.add_provider_mapping(
            ProviderMapping(
                item_id=file_item.path,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
            )
        )
        playlist.owner = self.name
        checksum = f"{SCHEMA_VERSION}.{file_item.checksum}"
        playlist.metadata.checksum = checksum
        return playlist

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        # filesystem items are always stored in db so we can query the database
        db_album = await self.mass.music.albums.get_db_item_by_prov_id(
            prov_album_id, provider_instance=self.instance_id
        )
        if db_album is None:
            raise MediaNotFoundError(f"Album not found: {prov_album_id}")
        # TODO: adjust to json query instead of text search
        query = f"SELECT * FROM tracks WHERE albums LIKE '%\"{db_album.item_id}\"%'"
        query += f" AND provider_mappings LIKE '%\"{self.instance_id}\"%'"
        result = []
        for track in await self.mass.music.tracks.get_db_items_by_query(query):
            track.album = db_album
            if album_mapping := next(
                (x for x in track.albums if x.item_id == db_album.item_id), None
            ):
                track.disc_number = album_mapping.disc_number
                track.track_number = album_mapping.track_number
                result.append(track)
        return sorted(result, key=lambda x: (x.disc_number or 0, x.track_number or 0))

    async def get_playlist_tracks(self, prov_playlist_id: str) -> list[Track]:
        """Get playlist tracks for given playlist id."""
        result = []
        if not await self.exists(prov_playlist_id):
            raise MediaNotFoundError(f"Playlist path does not exist: {prov_playlist_id}")

        _, ext = prov_playlist_id.rsplit(".", 1)
        try:
            # get playlist file contents
            playlist_data = b""
            async for chunk in self.read_file_content(prov_playlist_id):
                playlist_data += chunk
            playlist_data = playlist_data.decode("utf-8")

            if ext in ("m3u", "m3u8"):
                playlist_lines = await parse_m3u(playlist_data)
            else:
                playlist_lines = await parse_pls(playlist_data)

            for line_no, playlist_line in enumerate(playlist_lines):
                if media_item := await self._parse_playlist_line(
                    playlist_line, os.path.dirname(prov_playlist_id)
                ):
                    # use the linenumber as position for easier deletions
                    media_item.position = line_no
                    result.append(media_item)

        except Exception as err:  # pylint: disable=broad-except
            self.logger.warning("Error while parsing playlist %s", prov_playlist_id, exc_info=err)
        return result

    async def _parse_playlist_line(self, line: str, playlist_path: str) -> Track | Radio | None:
        """Try to parse a track from a playlist line."""
        try:
            # try to treat uri as (relative) filename
            if "://" not in line:
                for filename in (line, os.path.join(playlist_path, line)):
                    if not await self.exists(filename):
                        continue
                    return await self.get_track(filename)
            # fallback to generic uri parsing
            return await self.mass.music.get_item_by_uri(line)
        except MusicAssistantError as err:
            self.logger.warning("Could not parse uri/file %s to track: %s", line, str(err))
            return None

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        if not await self.exists(prov_playlist_id):
            raise MediaNotFoundError(f"Playlist path does not exist: {prov_playlist_id}")
        playlist_data = b""
        async for chunk in self.read_file_content(prov_playlist_id):
            playlist_data += chunk
        playlist_data = playlist_data.decode("utf-8")
        for uri in prov_track_ids:
            playlist_data += f"\n{uri}"

        # write playlist file
        await self.write_file_content(prov_playlist_id, playlist_data.encode("utf-8"))

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int]
    ) -> None:
        """Remove track(s) from playlist."""
        if not await self.exists(prov_playlist_id):
            raise MediaNotFoundError(f"Playlist path does not exist: {prov_playlist_id}")
        cur_lines = []
        _, ext = prov_playlist_id.rsplit(".", 1)

        # get playlist file contents
        playlist_data = b""
        async for chunk in self.read_file_content(prov_playlist_id):
            playlist_data += chunk
        playlist_data.decode("utf-8")

        if ext in ("m3u", "m3u8"):
            playlist_lines = await parse_m3u(playlist_data)
        else:
            playlist_lines = await parse_pls(playlist_data)

        for line_no, playlist_line in enumerate(playlist_lines):
            if line_no not in positions_to_remove:
                cur_lines.append(playlist_line)

        new_playlist_data = "\n".join(cur_lines)
        # write playlist file
        await self.write_file_content(prov_playlist_id, new_playlist_data.encode("utf-8"))

    async def create_playlist(self, name: str) -> Playlist:
        """Create a new playlist on provider with given name."""
        # creating a new playlist on the filesystem is as easy
        # as creating a new (empty) file with the m3u extension...
        filename = await self.resolve(f"{name}.m3u")
        await self.write_file_content(filename, b"")
        playlist = await self.get_playlist(filename)
        db_playlist = await self.mass.music.playlists.add_db_item(playlist)
        return db_playlist

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        db_item = await self.mass.music.tracks.get_db_item_by_prov_id(
            item_id=item_id, provider_instance=self.instance_id
        )
        if db_item is None:
            raise MediaNotFoundError(f"Item not found: {item_id}")

        prov_mapping = next(x for x in db_item.provider_mappings if x.item_id == item_id)
        file_item = await self.resolve(item_id)

        return StreamDetails(
            provider=self.domain,
            item_id=item_id,
            content_type=prov_mapping.content_type,
            media_type=MediaType.TRACK,
            duration=db_item.duration,
            size=file_item.file_size,
            sample_rate=prov_mapping.sample_rate,
            bit_depth=prov_mapping.bit_depth,
            direct=file_item.local_path,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item."""
        if seek_position:
            assert streamdetails.duration, "Duration required for seek requests"
            assert streamdetails.size, "Filesize required for seek requests"
            seek_bytes = int((streamdetails.size / streamdetails.duration) * seek_position)
        else:
            seek_bytes = 0

        async for chunk in self.read_file_content(streamdetails.item_id, seek_bytes):
            yield chunk

    async def _parse_artist(
        self,
        name: str | None = None,
        artist_path: str | None = None,
    ) -> Artist | None:
        """Lookup metadata in Artist folder."""
        assert name or artist_path
        if not artist_path:
            artist_path = name

        if not name:
            name = artist_path.split(os.sep)[-1]

        artist = Artist(
            artist_path,
            self.domain,
            name,
            provider_mappings={
                ProviderMapping(artist_path, self.domain, self.instance_id, url=artist_path)
            },
            musicbrainz_id=VARIOUS_ARTISTS_ID if compare_strings(name, VARIOUS_ARTISTS) else None,
        )

        if not await self.exists(artist_path):
            # return basic object if there is no dedicated artist folder
            return artist

        nfo_file = os.path.join(artist_path, "artist.nfo")
        if await self.exists(nfo_file):
            # found NFO file with metadata
            # https://kodi.wiki/view/NFO_files/Artists
            data = b""
            async for chunk in self.read_file_content(nfo_file):
                data += chunk
            info = await asyncio.to_thread(xmltodict.parse, data)
            info = info["artist"]
            artist.name = info.get("title", info.get("name", name))
            if sort_name := info.get("sortname"):
                artist.sort_name = sort_name
            if musicbrainz_id := info.get("musicbrainzartistid"):
                artist.musicbrainz_id = musicbrainz_id
            if description := info.get("biography"):
                artist.metadata.description = description
            if genre := info.get("genre"):
                artist.metadata.genres = set(split_items(genre))
        # find local images
        artist.metadata.images = await self._get_local_images(artist_path) or None

        return artist

    async def _parse_album(
        self, name: str | None, album_path: str | None, artists: list[Artist]
    ) -> Album | None:
        """Lookup metadata in Album folder."""
        assert (name or album_path) and artists
        if not album_path:
            # create fake path
            album_path = artists[0].name + os.sep + name

        if not name:
            name = album_path.split(os.sep)[-1]

        album = Album(
            album_path,
            self.domain,
            name,
            artists=artists,
            provider_mappings={
                ProviderMapping(album_path, self.domain, self.instance_id, url=album_path)
            },
        )

        if not await self.exists(album_path):
            # return basic object if there is no dedicated album folder
            return album

        nfo_file = os.path.join(album_path, "album.nfo")
        if await self.exists(nfo_file):
            # found NFO file with metadata
            # https://kodi.wiki/view/NFO_files/Artists
            data = b""
            async for chunk in self.read_file_content(nfo_file):
                data += chunk
            info = await asyncio.to_thread(xmltodict.parse, data)
            info = info["album"]
            album.name = info.get("title", info.get("name", name))
            if sort_name := info.get("sortname"):
                album.sort_name = sort_name
            if musicbrainz_id := info.get("musicbrainzreleasegroupid"):
                album.musicbrainz_id = musicbrainz_id
            if mb_artist_id := info.get("musicbrainzalbumartistid"):  # noqa: SIM102
                if album.artist and not album.artist.musicbrainz_id:
                    album.artist.musicbrainz_id = mb_artist_id
            if description := info.get("review"):
                album.metadata.description = description
            if year := info.get("year"):
                album.year = int(year)
            if genre := info.get("genre"):
                album.metadata.genres = set(split_items(genre))
        # parse name/version
        album.name, album.version = parse_title_and_version(album.name)

        # find local images
        album.metadata.images = await self._get_local_images(album_path) or None

        return album

    async def _get_local_images(self, folder: str) -> list[MediaItemImage]:
        """Return local images found in a given folderpath."""
        images = []
        async for item in self.listdir(folder):
            if "." not in item.path or item.is_dir:
                continue
            for ext in IMAGE_EXTENSIONS:
                if item.ext != ext:
                    continue
                try:
                    images.append(MediaItemImage(ImageType(item.name), item.path, True))
                except ValueError:
                    if "folder" in item.name or "AlbumArt" in item.name or "Artist" in item.name:
                        images.append(MediaItemImage(ImageType.THUMB, item.path, True))
        return images
