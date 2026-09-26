"""DroppedNeedle plugin API (phase 01b, EXPERIMENTAL - api_version 0).

See PLUGINS.md at the repository root for the authored contract. The host
loads third-party plugin packages from ``<root_app_dir>/plugins``; nothing is
bundled and no registry exists (a plugin is third-party by construction).
"""

from infrastructure.plugins.adapters import PluginClientAdapter, PluginIndexerAdapter
from infrastructure.plugins.host import (
    PluginHost,
    LoadedPlugin,
    PluginPublishResult,
    PluginRouteResult,
)
from infrastructure.plugins.manifest import (
    ACTIVE_CAPABILITIES,
    KNOWN_CAPABILITIES,
    PLUGIN_API_VERSION,
    PLUGIN_API_VERSION_LEGACY,
    RESERVED_CAPABILITIES,
    SUPPORTED_API_VERSIONS,
    V0_ACTIVE_CAPABILITIES,
    V0_KNOWN_CAPABILITIES,
    V0_RESERVED_CAPABILITIES,
    V1_ACTIVE_CAPABILITIES,
    V1_KNOWN_CAPABILITIES,
    V1_RESERVED_CAPABILITIES,
    ManifestError,
    PluginCapabilityConfig,
    PluginManifest,
    PluginRouteSpec,
    PluginScheduleConfig,
)
from infrastructure.plugins.protocols import (
    PluginContext,
    PluginPurchaseLink,
    ScrobbleEvent,
)

__all__ = [
    "ACTIVE_CAPABILITIES",
    "KNOWN_CAPABILITIES",
    "PLUGIN_API_VERSION",
    "PLUGIN_API_VERSION_LEGACY",
    "RESERVED_CAPABILITIES",
    "SUPPORTED_API_VERSIONS",
    "V0_ACTIVE_CAPABILITIES",
    "V0_KNOWN_CAPABILITIES",
    "V0_RESERVED_CAPABILITIES",
    "V1_ACTIVE_CAPABILITIES",
    "V1_KNOWN_CAPABILITIES",
    "V1_RESERVED_CAPABILITIES",
    "ManifestError",
    "PluginCapabilityConfig",
    "PluginClientAdapter",
    "PluginHost",
    "PluginIndexerAdapter",
    "LoadedPlugin",
    "PluginManifest",
    "PluginPublishResult",
    "PluginRouteSpec",
    "PluginRouteResult",
    "PluginScheduleConfig",
    "PluginContext",
    "PluginPurchaseLink",
    "ScrobbleEvent",
]
