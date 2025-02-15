"""Models for providers and plugins in the MA ecosystem."""

import asyncio
from dataclasses import dataclass, field
from typing import TypedDict

from mashumaro import DataClassDictMixin

from music_assistant.common.helpers.json import load_json_file

from .config_entries import ConfigEntry
from .enums import MediaType, ProviderFeature, ProviderType


@dataclass
class ProviderManifest(DataClassDictMixin):
    """ProviderManifest, details of a provider."""

    type: ProviderType
    domain: str
    name: str
    description: str
    codeowners: list[str]

    # optional params
    # config_entries: list of config entries required to configure/setup this provider
    config_entries: list[ConfigEntry] = field(default_factory=list)
    # requirements: list of (pip style) python packages required for this provider
    requirements: list[str] = field(default_factory=list)
    # documentation: link/url to documentation.
    documentation: str | None = None
    # init_class: class to initialize, within provider's package
    # e.g. `SpotifyProvider`. (autodetect if None)
    init_class: str | None = None
    # multi_instance: whether multiple instances of the same provider are allowed/possible
    multi_instance: bool = False
    # builtin: whether this provider is a system/builtin and can not disabled/removed
    builtin: bool = False
    # load_by_default: load this provider by default (mostly used together with `builtin`)
    load_by_default: bool = False
    # depends_on: depends on another provider to function
    depends_on: str | None = None

    @classmethod
    async def parse(cls: "ProviderManifest", manifest_file: str) -> "ProviderManifest":
        """Parse ProviderManifest from file."""
        manifest_dict = await load_json_file(manifest_file)
        return cls.from_dict(manifest_dict)


class ProviderInstance(TypedDict):
    """Provider instance detailed dict when a provider is serialized over the api."""

    type: ProviderType
    domain: str
    name: str
    instance_id: str
    supported_features: list[ProviderFeature]
    available: bool
    last_error: str | None


@dataclass
class SyncTask:
    """Description of a Sync task/job of a musicprovider."""

    provider_domain: str
    provider_instance: str
    media_types: tuple[MediaType]
    task: asyncio.Task

    def __post_init__(self):
        """Execute action after initialization."""
        # make sure that the task does not get serialized.
        setattr(self.task, "do_not_serialize", True)
