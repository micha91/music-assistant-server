"""Main Music Assistant class."""
from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import os
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any

from aiohttp import ClientSession, TCPConnector, web
from zeroconf import InterfaceChoice, NonUniqueNameException, ServiceInfo, Zeroconf

from music_assistant.common.helpers.util import get_ip, get_ip_pton, select_free_port
from music_assistant.common.models.config_entries import ProviderConfig
from music_assistant.common.models.enums import EventType, ProviderType
from music_assistant.common.models.errors import (
    MusicAssistantError,
    ProviderUnavailableError,
    SetupFailedError,
)
from music_assistant.common.models.event import MassEvent
from music_assistant.common.models.provider import ProviderManifest
from music_assistant.constants import CONF_SERVER_ID, CONF_WEB_IP, ROOT_LOGGER_NAME
from music_assistant.server.controllers.cache import CacheController
from music_assistant.server.controllers.config import ConfigController
from music_assistant.server.controllers.metadata import MetaDataController
from music_assistant.server.controllers.music import MusicController
from music_assistant.server.controllers.players import PlayerController
from music_assistant.server.controllers.streams import StreamsController
from music_assistant.server.helpers.api import APICommandHandler, api_command, mount_websocket_api
from music_assistant.server.helpers.util import install_package
from music_assistant.server.models.plugin import PluginProvider

from .models.metadata_provider import MetadataProvider
from .models.music_provider import MusicProvider
from .models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from types import TracebackType

ProviderInstanceType = MetadataProvider | MusicProvider | PlayerProvider
EventCallBackType = Callable[[MassEvent], None]
EventSubscriptionType = tuple[EventCallBackType, tuple[EventType] | None, tuple[str] | None]

LOGGER = logging.getLogger(ROOT_LOGGER_NAME)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROVIDERS_PATH = os.path.join(BASE_DIR, "providers")


class MusicAssistant:
    """Main MusicAssistant (Server) object."""

    loop: asyncio.AbstractEventLoop
    http_session: ClientSession
    _web_apprunner: web.AppRunner
    _web_tcp: web.TCPSite

    def __init__(self, storage_path: str, port: int | None = None) -> None:
        """Initialize the MusicAssistant Server."""
        self.storage_path = storage_path
        self.port = port
        self.base_ip = get_ip()
        # shared zeroconf instance
        self.zeroconf = Zeroconf(interfaces=InterfaceChoice.All)
        # we dynamically register command handlers
        self.webapp = web.Application()
        self.command_handlers: dict[str, APICommandHandler] = {}
        self._subscribers: set[EventSubscriptionType] = set()
        self._available_providers: dict[str, ProviderManifest] = {}
        self._providers: dict[str, ProviderInstanceType] = {}

        # init core controllers
        self.config = ConfigController(self)
        self.cache = CacheController(self)
        self.metadata = MetaDataController(self)
        self.music = MusicController(self)
        self.players = PlayerController(self)
        self.streams = StreamsController(self)
        self._tracked_tasks: list[asyncio.Task] = []
        self.closing = False
        # register all api commands (methods with decorator)
        self._register_api_commands()

    async def start(self) -> None:
        """Start running the Music Assistant server."""
        self.loop = asyncio.get_running_loop()

        # create shared aiohttp ClientSession
        self.http_session = ClientSession(
            loop=self.loop,
            connector=TCPConnector(ssl=False),
        )
        # setup config controller first and fetch important config values
        await self.config.setup()
        if self.port is None:
            # if port is None, we need to autoselect it
            self.port = await select_free_port(8095, 9200)
        # allow overriding of the base_ip if autodetect failed
        self.base_ip = self.config.get(CONF_WEB_IP, self.base_ip)
        LOGGER.info(
            "Starting Music Assistant Server on port: %s" " - autodetected IP-address: %s",
            self.port,
            self.base_ip,
        )

        # setup other core controllers
        await self.cache.setup()
        await self.music.setup()
        await self.metadata.setup()
        await self.players.setup()
        await self.streams.setup()

        # load providers
        await self._load_providers()
        # setup web server
        mount_websocket_api(self, "/ws")
        self._web_apprunner = web.AppRunner(self.webapp, access_log=None)
        await self._web_apprunner.setup()
        # set host to None to bind to all addresses on both IPv4 and IPv6
        host = None
        self._web_tcp = web.TCPSite(self._web_apprunner, host=host, port=self.port)
        await self._web_tcp.start()
        await self._setup_discovery()

    async def stop(self) -> None:
        """Stop running the music assistant server."""
        LOGGER.info("Stop called, cleaning up...")
        self.signal_event(EventType.SHUTDOWN)
        self.closing = True
        # cancel all running tasks
        for task in self._tracked_tasks:
            task.cancel()
        # stop/clean streams controller
        await self.streams.close()
        # stop/clean webserver
        await self._web_tcp.stop()
        await self._web_apprunner.cleanup()
        await self.webapp.shutdown()
        await self.webapp.cleanup()
        # stop core controllers
        await self.metadata.close()
        await self.music.close()
        await self.players.close()
        # cleanup all providers
        for prov in self._providers.values():
            await prov.close()
        # cleanup cache and config
        await self.config.close()
        await self.cache.close()
        # close/cleanup shared http session
        if self.http_session:
            await self.http_session.close()

    @property
    def base_url(self) -> str:
        """Return the (web)server's base url."""
        return f"http://{self.base_ip}:{self.port}"

    @property
    def server_id(self) -> str:
        """Return unique ID of this server."""
        if not self.config.initialized:
            return ""
        return self.config.get(CONF_SERVER_ID)  # type: ignore[no-any-return]

    @api_command("providers/available")
    def get_available_providers(self) -> list[ProviderManifest]:
        """Return all available Providers."""
        return list(self._available_providers.values())

    @api_command("providers")
    def get_providers(
        self, provider_type: ProviderType | None = None
    ) -> list[ProviderInstanceType]:
        """Return all loaded/running Providers (instances), optionally filtered by ProviderType."""
        return [
            x for x in self._providers.values() if provider_type is None or provider_type == x.type
        ]

    @property
    def providers(self) -> list[ProviderInstanceType]:
        """Return all loaded/running Providers (instances)."""
        return list(self._providers.values())

    def get_provider(self, provider_instance_or_domain: str) -> ProviderInstanceType:
        """Return provider by instance id (or domain)."""
        if prov := self._providers.get(provider_instance_or_domain):
            return prov
        for prov in self._providers.values():
            if prov.domain == provider_instance_or_domain:
                return prov
        raise ProviderUnavailableError(f"Provider {provider_instance_or_domain} is not available")

    def signal_event(
        self,
        event: EventType,
        object_id: str | None = None,
        data: Any = None,
    ) -> None:
        """Signal event to subscribers."""
        if self.closing:
            return

        if LOGGER.isEnabledFor(logging.DEBUG) and event != EventType.QUEUE_TIME_UPDATED:
            # do not log queue time updated events because that is too chatty
            LOGGER.getChild("event").debug("%s %s", event.value, object_id or "")

        event_obj = MassEvent(event=event, object_id=object_id, data=data)
        for cb_func, event_filter, id_filter in self._subscribers:
            if not (event_filter is None or event in event_filter):
                continue
            if not (id_filter is None or object_id in id_filter):
                continue
            if asyncio.iscoroutinefunction(cb_func):
                asyncio.run_coroutine_threadsafe(cb_func(event_obj), self.loop)
            else:
                self.loop.call_soon_threadsafe(cb_func, event_obj)

    def subscribe(
        self,
        cb_func: EventCallBackType,
        event_filter: EventType | tuple[EventType] | None = None,
        id_filter: str | tuple[str] | None = None,
    ) -> Callable:
        """Add callback to event listeners.

        Returns function to remove the listener.
            :param cb_func: callback function or coroutine
            :param event_filter: Optionally only listen for these events
            :param id_filter: Optionally only listen for these id's (player_id, queue_id, uri)
        """
        if isinstance(event_filter, EventType):
            event_filter = (event_filter,)
        if isinstance(id_filter, str):
            id_filter = (id_filter,)
        listener = (cb_func, event_filter, id_filter)
        self._subscribers.add(listener)

        def remove_listener():
            self._subscribers.remove(listener)

        return remove_listener

    def create_task(
        self,
        target: Coroutine | Awaitable | Callable | asyncio.Future,
        *args: Any,
        **kwargs: Any,
    ) -> asyncio.Task | asyncio.Future:
        """Create Task on (main) event loop from Coroutine(function).

        Tasks created by this helper will be properly cancelled on stop.
        """
        if asyncio.iscoroutinefunction(target):
            task = self.loop.create_task(target(*args, **kwargs))
        elif isinstance(target, asyncio.Future):
            task = target
        elif asyncio.iscoroutine(target):
            task = self.loop.create_task(target)
        else:
            # assume normal callable (non coroutine or awaitable)
            task = self.loop.create_task(asyncio.to_thread(target, *args, **kwargs))

        def task_done_callback(*args, **kwargs):  # noqa: ARG001
            self._tracked_tasks.remove(task)
            if LOGGER.isEnabledFor(logging.DEBUG):
                # print unhandled exceptions
                task_name = getattr(task, "name", "")
                if not task.cancelled() and task.exception():
                    task_name = task.get_name() if hasattr(task, "get_name") else task
                    LOGGER.exception(
                        "Exception in task %s",
                        task_name,
                        exc_info=task.exception(),
                    )

        self._tracked_tasks.append(task)
        task.add_done_callback(task_done_callback)
        return task

    def register_api_command(
        self,
        command: str,
        handler: Callable,
    ) -> None:
        """Dynamically register a command on the API."""
        assert command not in self.command_handlers, "Command already registered"
        self.command_handlers[command] = APICommandHandler.parse(command, handler)

    async def load_provider(self, conf: ProviderConfig) -> None:  # noqa: C901
        """Load (or reload) a provider."""
        # if provider is already loaded, stop and unload it first
        await self.unload_provider(conf.instance_id)

        LOGGER.debug("Loading provider %s", conf.name or conf.domain)
        # abort if provider is disabled
        if not conf.enabled:
            LOGGER.debug(
                "Not loading provider %s because it is disabled",
                conf.name or conf.instance_id,
            )
            return

        domain = conf.domain
        prov_manifest = self._available_providers.get(domain)
        # check for other instances of this provider
        existing = next((x for x in self.providers if x.domain == domain), None)
        if existing and not prov_manifest.multi_instance:
            raise SetupFailedError(
                f"Provider {domain} already loaded and only one instance allowed."
            )

        if not prov_manifest:
            raise SetupFailedError(f"Provider {domain} manifest not found")

        # try to load the module
        try:
            prov_mod = importlib.import_module(f".{domain}", "music_assistant.server.providers")
            for name, obj in inspect.getmembers(prov_mod):
                if not inspect.isclass(obj):
                    continue
                # lookup class to initialize
                if name == prov_manifest.init_class or (
                    not prov_manifest.init_class
                    and issubclass(
                        obj, MusicProvider | PlayerProvider | MetadataProvider | PluginProvider
                    )
                    and obj != MusicProvider
                    and obj != PlayerProvider
                    and obj != MetadataProvider
                    and obj != PluginProvider
                ):
                    prov_cls = obj
                    break
            else:
                raise AttributeError("Unable to locate Provider class")
            provider: ProviderInstanceType = prov_cls(self, prov_manifest, conf)
            self._providers[provider.instance_id] = provider
            try:
                await provider.setup()
            except MusicAssistantError as err:
                provider.last_error = str(err)
                provider.available = False
                raise err

            # mark provider as available once setup succeeded
            provider.available = True
            provider.last_error = None
            # if this is a music provider, start sync
            if provider.type == ProviderType.MUSIC:
                await self.music.start_sync(providers=[provider.instance_id])
        # pylint: disable=broad-except
        except Exception as exc:
            LOGGER.exception(
                "Error loading provider(instance) %s: %s",
                conf.name or conf.domain,
                str(exc),
            )
        else:
            LOGGER.info(
                "Loaded %s provider %s",
                provider.type.value,
                conf.name or conf.domain,
            )
        # always signal event, regardless if the loading succeeded or not
        self.signal_event(EventType.PROVIDERS_UPDATED, data=self.get_providers())

    async def unload_provider(self, instance_id: str) -> None:
        """Unload a provider."""
        if provider := self._providers.get(instance_id):
            # make sure to stop any running sync tasks first
            for sync_task in self.music.in_progress_syncs:
                if sync_task.provider_instance == instance_id:
                    sync_task.task.cancel()
                    await sync_task.task
            await provider.close()
            self._providers.pop(instance_id)
            self.signal_event(EventType.PROVIDERS_UPDATED, data=self.get_providers())

    def _register_api_commands(self) -> None:
        """Register all methods decorated as api_command within a class(instance)."""
        for cls in (
            self,
            self.config,
            self.metadata,
            self.music,
            self.players,
            self.players.queues,
        ):
            for attr_name in dir(cls):
                if attr_name.startswith("__"):
                    continue
                obj = getattr(cls, attr_name)
                if hasattr(obj, "api_cmd"):
                    # method is decorated with our api decorator
                    self.register_api_command(obj.api_cmd, obj)

    async def _load_providers(self) -> None:
        """Load providers from config."""
        # load all available providers from manifest files
        await self.__load_available_providers()

        # create default config for any 'load_by_default' providers (e.g. URL provider)
        # we must do this first to resolve any dependencies
        # NOTE: this will auto load any not yet existing providers
        provider_configs = self.config.get_provider_configs()
        for prov_manifest in self._available_providers.values():
            if not prov_manifest.load_by_default:
                continue
            existing = any(x for x in provider_configs if x.domain == prov_manifest.domain)
            if existing:
                continue
            default_conf = self.config.create_provider_config(prov_manifest.domain)
            # skip_reload to prevent race condition
            self.config.set_provider_config(default_conf, skip_reload=True)

        # load all configured (and enabled) providers
        for allow_depends_on in (False, True):
            for prov_conf in self.config.get_provider_configs():
                prov_manifest = self._available_providers[prov_conf.domain]
                if prov_manifest.depends_on and not allow_depends_on:
                    continue
                if prov_conf.instance_id in self._providers:
                    continue
                await self.load_provider(prov_conf)

    async def __load_available_providers(self) -> None:
        """Preload all available provider manifest files."""
        for dir_str in os.listdir(PROVIDERS_PATH):
            dir_path = os.path.join(PROVIDERS_PATH, dir_str)
            if not os.path.isdir(dir_path):
                continue
            # get files in subdirectory
            for file_str in os.listdir(dir_path):
                file_path = os.path.join(dir_path, file_str)
                if not os.path.isfile(file_path):
                    continue
                if file_str != "manifest.json":
                    continue
                try:
                    provider_manifest = await ProviderManifest.parse(file_path)
                    self._available_providers[provider_manifest.domain] = provider_manifest
                    # install requirement/dependencies
                    for requirement in provider_manifest.requirements:
                        await install_package(requirement)
                    LOGGER.debug("Loaded manifest for provider %s", dir_str)
                except Exception as exc:  # pylint: disable=broad-except
                    LOGGER.exception(
                        "Error while loading manifest for provider %s",
                        dir_str,
                        exc_info=exc,
                    )

    async def _setup_discovery(self) -> None:
        """Make this Music Assistant instance discoverable on the network."""

        def setup_discovery():
            zeroconf_type = "_music-assistant._tcp.local."
            server_id = "mass"  # TODO ?

            info = ServiceInfo(
                zeroconf_type,
                name=f"{server_id}.{zeroconf_type}",
                addresses=[get_ip_pton()],
                port=self.port,
                properties={},
                server=f"mass_{server_id}.local.",
            )
            LOGGER.debug("Starting Zeroconf broadcast...")
            try:
                existing = getattr(self, "mass_zc_service_set", None)
                if existing:
                    self.zeroconf.update_service(info)
                else:
                    self.zeroconf.register_service(info)
                setattr(self, "mass_zc_service_set", True)
            except NonUniqueNameException:
                LOGGER.error(
                    "Music Assistant instance with identical name present in the local network!"
                )

        await asyncio.to_thread(setup_discovery)

    async def __aenter__(self) -> MusicAssistant:
        """Return Context manager."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException],
        exc_val: BaseException,
        exc_tb: TracebackType,
    ) -> bool | None:
        """Exit context manager."""
        await self.stop()
        if exc_val:
            raise exc_val
        return exc_type
