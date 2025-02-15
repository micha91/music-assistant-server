"""Helper module for parsing the Youtube Music API.

This helpers file is an async wrapper around the excellent ytmusicapi package.
While the ytmusicapi package does an excellent job at parsing the Youtube Music results,
it is unfortunately not async, which is required for Music Assistant to run smoothly.
This also nicely separates the parsing logic from the Youtube Music provider logic.
"""

import asyncio
import json
from time import time

import ytmusicapi


async def get_artist(prov_artist_id: str) -> dict[str, str]:
    """Async wrapper around the ytmusicapi get_artist function."""

    def _get_artist():
        ytm = ytmusicapi.YTMusic()
        try:
            artist = ytm.get_artist(channelId=prov_artist_id)
            # ChannelId can sometimes be different and original ID is not part of the response
            artist["channelId"] = prov_artist_id
        except KeyError:
            user = ytm.get_user(channelId=prov_artist_id)
            artist = {"channelId": prov_artist_id, "name": user["name"]}
        return artist

    return await asyncio.to_thread(_get_artist)


async def get_album(prov_album_id: str) -> dict[str, str]:
    """Async wrapper around the ytmusicapi get_album function."""

    def _get_album():
        ytm = ytmusicapi.YTMusic()
        return ytm.get_album(browseId=prov_album_id)

    return await asyncio.to_thread(_get_album)


async def get_playlist(
    prov_playlist_id: str, headers: dict[str, str], username: str
) -> dict[str, str]:
    """Async wrapper around the ytmusicapi get_playlist function."""

    def _get_playlist():
        user = username if is_brand_account(username) else None
        ytm = ytmusicapi.YTMusic(auth=json.dumps(headers), user=user)
        playlist = ytm.get_playlist(playlistId=prov_playlist_id)
        playlist["checksum"] = get_playlist_checksum(playlist)
        return playlist

    return await asyncio.to_thread(_get_playlist)


async def get_track(prov_track_id: str) -> dict[str, str]:
    """Async wrapper around the ytmusicapi get_playlist function."""

    def _get_song():
        ytm = ytmusicapi.YTMusic()
        track_obj = ytm.get_song(videoId=prov_track_id)
        track = {}
        track["videoId"] = track_obj["videoDetails"]["videoId"]
        track["title"] = track_obj["videoDetails"]["title"]
        track["artists"] = [
            {
                "channelId": track_obj["videoDetails"]["channelId"],
                "name": track_obj["videoDetails"]["author"],
            }
        ]
        track["duration"] = track_obj["videoDetails"]["lengthSeconds"]
        track["thumbnails"] = track_obj["microformat"]["microformatDataRenderer"]["thumbnail"][
            "thumbnails"
        ]
        track["isAvailable"] = track_obj["playabilityStatus"]["status"] == "OK"
        return track

    return await asyncio.to_thread(_get_song)


async def get_library_artists(headers: dict[str, str], username: str) -> dict[str, str]:
    """Async wrapper around the ytmusicapi get_library_artists function."""

    def _get_library_artists():
        user = username if is_brand_account(username) else None
        ytm = ytmusicapi.YTMusic(auth=json.dumps(headers), user=user)
        artists = ytm.get_library_subscriptions(limit=9999)
        # Sync properties with uniformal artist object
        for artist in artists:
            artist["id"] = artist["browseId"]
            artist["name"] = artist["artist"]
            del artist["browseId"]
            del artist["artist"]
        return artists

    return await asyncio.to_thread(_get_library_artists)


async def get_library_albums(headers: dict[str, str], username: str) -> dict[str, str]:
    """Async wrapper around the ytmusicapi get_library_albums function."""

    def _get_library_albums():
        user = username if is_brand_account(username) else None
        ytm = ytmusicapi.YTMusic(auth=json.dumps(headers), user=user)
        return ytm.get_library_albums(limit=9999)

    return await asyncio.to_thread(_get_library_albums)


async def get_library_playlists(headers: dict[str, str], username: str) -> dict[str, str]:
    """Async wrapper around the ytmusicapi get_library_playlists function."""

    def _get_library_playlists():
        user = username if is_brand_account(username) else None
        ytm = ytmusicapi.YTMusic(auth=json.dumps(headers), user=user)
        playlists = ytm.get_library_playlists(limit=9999)
        # Sync properties with uniformal playlist object
        for playlist in playlists:
            playlist["id"] = playlist["playlistId"]
            del playlist["playlistId"]
            playlist["checksum"] = get_playlist_checksum(playlist)
        return playlists

    return await asyncio.to_thread(_get_library_playlists)


async def get_library_tracks(headers: dict[str, str], username: str) -> dict[str, str]:
    """Async wrapper around the ytmusicapi get_library_tracks function."""

    def _get_library_tracks():
        user = username if is_brand_account(username) else None
        ytm = ytmusicapi.YTMusic(auth=json.dumps(headers), user=user)
        tracks = ytm.get_library_songs(limit=9999)
        return tracks

    return await asyncio.to_thread(_get_library_tracks)


async def library_add_remove_artist(
    headers: dict[str, str], prov_artist_id: str, add: bool = True, username: str = None
) -> bool:
    """Add or remove an artist to the user's library."""

    def _library_add_remove_artist():
        user = username if is_brand_account(username) else None
        ytm = ytmusicapi.YTMusic(auth=json.dumps(headers), user=user)
        if add:
            return "actions" in ytm.subscribe_artists(channelIds=[prov_artist_id])
        if not add:
            return "actions" in ytm.unsubscribe_artists(channelIds=[prov_artist_id])
        return None

    return await asyncio.to_thread(_library_add_remove_artist)


async def library_add_remove_album(
    headers: dict[str, str], prov_item_id: str, add: bool = True, username: str = None
) -> bool:
    """Add or remove an album or playlist to the user's library."""
    album = await get_album(prov_album_id=prov_item_id)

    def _library_add_remove_album():
        user = username if is_brand_account(username) else None
        ytm = ytmusicapi.YTMusic(auth=json.dumps(headers), user=user)
        playlist_id = album["audioPlaylistId"]
        if add:
            return ytm.rate_playlist(playlist_id, "LIKE")
        if not add:
            return ytm.rate_playlist(playlist_id, "INDIFFERENT")
        return None

    return await asyncio.to_thread(_library_add_remove_album)


async def library_add_remove_playlist(
    headers: dict[str, str], prov_item_id: str, add: bool = True, username: str = None
) -> bool:
    """Add or remove an album or playlist to the user's library."""

    def _library_add_remove_playlist():
        user = username if is_brand_account(username) else None
        ytm = ytmusicapi.YTMusic(auth=json.dumps(headers), user=user)
        if add:
            return "actions" in ytm.rate_playlist(prov_item_id, "LIKE")
        if not add:
            return "actions" in ytm.rate_playlist(prov_item_id, "INDIFFERENT")
        return None

    return await asyncio.to_thread(_library_add_remove_playlist)


async def add_remove_playlist_tracks(
    headers: dict[str, str],
    prov_playlist_id: str,
    prov_track_ids: list[str],
    add: bool,
    username: str = None,
) -> bool:
    """Async wrapper around adding/removing tracks to a playlist."""

    def _add_playlist_tracks():
        user = username if is_brand_account(username) else None
        ytm = ytmusicapi.YTMusic(auth=json.dumps(headers), user=user)
        if add:
            return ytm.add_playlist_items(playlistId=prov_playlist_id, videoIds=prov_track_ids)
        if not add:
            return ytm.remove_playlist_items(playlistId=prov_playlist_id, videos=prov_track_ids)
        return None

    return await asyncio.to_thread(_add_playlist_tracks)


async def get_song_radio_tracks(
    headers: dict[str, str], username: str, prov_item_id: str, limit=25
) -> dict[str, str]:
    """Async wrapper around the ytmusicapi radio function."""
    user = username if is_brand_account(username) else None

    def _get_song_radio_tracks():
        ytm = ytmusicapi.YTMusic(auth=json.dumps(headers), user=user)
        playlist_id = f"RDAMVM{prov_item_id}"
        result = ytm.get_watch_playlist(videoId=prov_item_id, playlistId=playlist_id, limit=limit)
        # Replace inconsistensies for easier parsing
        for track in result["tracks"]:
            if track.get("thumbnail"):
                track["thumbnails"] = track["thumbnail"]
                del track["thumbnail"]
            if track.get("length"):
                track["duration"] = get_sec(track["length"])
        return result

    return await asyncio.to_thread(_get_song_radio_tracks)


async def search(query: str, ytm_filter: str = None, limit: int = 20) -> list[dict]:
    """Async wrapper around the ytmusicapi search function."""

    def _search():
        ytm = ytmusicapi.YTMusic()
        results = ytm.search(query=query, filter=ytm_filter, limit=limit)
        # Sync result properties with uniformal objects
        for result in results:
            if result["resultType"] == "artist":
                result["id"] = result["browseId"]
                result["name"] = result["artist"]
                del result["browseId"]
                del result["artist"]
            elif result["resultType"] == "playlist":
                if "playlistId" in result:
                    result["id"] = result["playlistId"]
                    del result["playlistId"]
                elif "browseId" in result:
                    result["id"] = result["browseId"]
                    del result["browseId"]
        return results

    return await asyncio.to_thread(_search)


def get_playlist_checksum(playlist_obj: dict) -> str:
    """Try to calculate a checksum so we can detect changes in a playlist."""
    for key in ("duration_seconds", "trackCount"):
        if key in playlist_obj:
            return playlist_obj[key]
    return str(int(time()))


def is_brand_account(username: str) -> bool:
    """Check if the provided username is a brand-account."""
    return len(username) == 21 and username.isdigit()


def get_sec(time_str):
    """Get seconds from time."""
    parts = time_str.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    return 0
