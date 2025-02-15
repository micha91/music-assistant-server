"""Model/base for a Provider implementation within Music Assistant."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant.common.models.config_entries import ConfigEntryValue, ProviderConfig
from music_assistant.common.models.enums import ProviderFeature, ProviderType
from music_assistant.common.models.provider import ProviderInstance, ProviderManifest
from music_assistant.constants import ROOT_LOGGER_NAME

if TYPE_CHECKING:
    from music_assistant.server import MusicAssistant

# noqa: ARG001


class Provider:
    """Base representation of a Provider implementation within Music Assistant."""

    _attr_supported_features: tuple[ProviderFeature, ...] = tuple()

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        self.mass = mass
        self.manifest = manifest
        self.config = config
        self.logger = logging.getLogger(f"{ROOT_LOGGER_NAME}.providers.{self.domain}")
        self.cache = mass.cache
        self.available = False
        self.last_error = None

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this MusicProvider."""
        return self._attr_supported_features

    async def setup(self) -> None:
        """Handle async initialization of the provider.

        Called when provider is registered (or its config updated).
        """

    async def close(self) -> None:
        """Handle close/cleanup of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """

    @property
    def type(self) -> ProviderType:
        """Return type of this provider."""
        return self.manifest.type

    @property
    def domain(self) -> str:
        """Return domain for this provider."""
        return self.manifest.domain

    @property
    def instance_id(self) -> str:
        """Return instance_id for this provider(instance)."""
        return self.config.instance_id

    @property
    def name(self) -> str:
        """Return (custom) friendly name for this provider instance."""
        if self.config.name:
            return self.config.name
        inst_count = len([x for x in self.mass.music.providers if x.domain == self.domain])
        if inst_count > 1:
            postfix = self.instance_id[:-8]
            return f"{self.manifest.name}.{postfix}"
        return self.manifest.name

    @property
    def config_entries(self) -> list[ConfigEntryValue]:
        """Return list of all ConfigEntries including values for this provider(instance)."""
        return [
            ConfigEntryValue.parse(x, self.config.values.get(x.key))
            for x in self.manifest.config_entries
        ]

    def to_dict(self, *args, **kwargs) -> ProviderInstance:  # noqa: ARG002
        """Return Provider(instance) as serializable dict."""
        return {
            "type": self.type.value,
            "domain": self.domain,
            "name": self.name,
            "instance_id": self.instance_id,
            "supported_features": [x.value for x in self.supported_features],
            "available": self.available,
            "last_error": self.last_error,
        }
