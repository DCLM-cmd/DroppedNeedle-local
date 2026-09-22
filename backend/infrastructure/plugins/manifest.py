"""Plugin manifest: ``plugin.toml`` at a plugin package's root.

The manifest is the contract's front door: the host refuses to import any code
before the manifest parses, declares a compatible ``api_version``, and names
only known capabilities. Capability ids are deliberately generic - the API
carries no acquisition examples anywhere (D22).
"""

import re
import tomllib
from pathlib import Path

from infrastructure.msgspec_fastapi import AppStruct

PLUGIN_API_VERSION = 1
SUPPORTED_API_VERSIONS = (0, 1)
PLUGIN_API_VERSION_LEGACY = 0

# 'metadata_provider' and 'streaming_source' are accepted (reserved) but not
# activated yet - the host logs and skips them until a future api_version wires
# their consumers. Validating them now keeps early manifests forward-compatible.
# v0 sets below are frozen: api_version 0 keeps byte-identical behaviour. The
# v1 active/reserved sets live alongside them for api_version 1 manifests;
# see protocols.py for the trust model and acquisition caps.
V0_ACTIVE_CAPABILITIES = frozenset({"scrobbler", "purchase_links"})
V0_RESERVED_CAPABILITIES = frozenset({"metadata_provider", "streaming_source"})
V0_KNOWN_CAPABILITIES = V0_ACTIVE_CAPABILITIES | V0_RESERVED_CAPABILITIES
V1_ACTIVE_CAPABILITIES = frozenset(
    {
        "scrobbler",
        "purchase_links",
        "download_client",
        "indexer",
        "subscriber",
        "publisher",
        "metadata_provider",
        "scheduler",
        "streaming_source",
    }
)
V1_RESERVED_CAPABILITIES = frozenset()
V1_KNOWN_CAPABILITIES = V1_ACTIVE_CAPABILITIES | V1_RESERVED_CAPABILITIES
ACTIVE_CAPABILITIES = V1_ACTIVE_CAPABILITIES
RESERVED_CAPABILITIES = V1_RESERVED_CAPABILITIES
KNOWN_CAPABILITIES = ACTIVE_CAPABILITIES | RESERVED_CAPABILITIES

_V1_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_ROUTE_PATH_RE = re.compile(r"^[a-z0-9][a-z0-9/_-]{0,63}$")
_PLUGIN_TARGET_RE = re.compile(r"^plugin:[a-z0-9][a-z0-9-]{0,31}$")

_ROUTE_METHODS = frozenset({"GET", "POST", "DELETE"})
_ROUTE_AUTHS = frozenset({"admin", "user"})

_ALLOWED_TOP_KEYS = frozenset({"plugin", "settings", "capability", "schedule", "route", "plugin_ui"})
_ALLOWED_PLUGIN_KEYS = frozenset(
    {
        "name",
        "version",
        "api_version",
        "entrypoint",
        "capabilities",
        "display_name",
        "description",
        "author",
        "homepage",
    }
)
_ALLOWED_SETTING_KEYS = frozenset({"key", "label", "help", "secret"})
_ALLOWED_CAPABILITY_KEYS = frozenset({"id", "source", "target_source", "display_name"})
_ALLOWED_SCHEDULE_KEYS = frozenset({"interval_minutes", "run_on_load"})
_ALLOWED_ROUTE_KEYS = frozenset({"path", "method", "auth", "rate_limit_per_minute"})
_ALLOWED_UI_KEYS = frozenset({"entry", "pages", "external_url"})


class ManifestError(Exception):
    """The manifest is missing, unparsable, or declares an invalid contract."""


class PluginSettingField(AppStruct):
    """One admin-editable setting the plugin wants (rendered by the generic
    settings UI; values are stored per-plugin in config.json)."""

    key: str
    label: str
    help: str = ""
    secret: bool = False

class PluginCapabilityConfig(AppStruct):
    """Per-capability config from one ``[[capability]]`` table (v1)."""

    id: str
    source: str = ""
    target_source: str = ""
    display_name: str = ""


class PluginScheduleConfig(AppStruct):
    """Schedule for the ``scheduler`` capability, from the ``[schedule]`` table."""

    interval_minutes: int = 60
    run_on_load: bool = False


class PluginRouteSpec(AppStruct):
    """One ``[[route]]`` table: plugin HTTP served under ``/ext/`` (v1)."""

    path: str
    method: str = "GET"
    auth: str = "admin"
    rate_limit_per_minute: int = 60


class PluginManifest(AppStruct):
    name: str  # unique id, kebab-case
    version: str
    api_version: int
    entrypoint: str  # "<module>:<ClassName>" inside the plugin package
    capabilities: list[str]
    display_name: str = ""
    description: str = ""
    author: str = ""
    homepage: str = ""
    settings: list[PluginSettingField] = []
    capability_configs: list[PluginCapabilityConfig] = []
    schedule: PluginScheduleConfig | None = None
    routes: list[PluginRouteSpec] = []
    ui_entry: str = ""
    ui_pages: list[str] = []
    ui_external_url: str = ""


def load_manifest(plugin_dir: Path) -> PluginManifest:
    path = plugin_dir / "plugin.toml"
    if not path.is_file():
        raise ManifestError(f"{plugin_dir.name}: no plugin.toml")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"{plugin_dir.name}: unreadable plugin.toml ({exc})") from exc

    plugin = raw.get("plugin")
    if not isinstance(plugin, dict):
        raise ManifestError(f"{plugin_dir.name}: missing [plugin] table")

    raw_name = str(plugin.get("name") or "").strip()
    try:
        api_version = int(plugin.get("api_version"))  # noqa: PLW2901 - version gate reads the raw value first
    except (TypeError, ValueError):
        raise ManifestError(f"{raw_name or plugin_dir.name}: api_version must be an integer") from None
    if api_version not in SUPPORTED_API_VERSIONS:
        raise ManifestError(
            f"{raw_name or plugin_dir.name}: api_version {api_version} unsupported "
            f"(host speaks {SUPPORTED_API_VERSIONS})"
        )

    name = raw_name
    if api_version == PLUGIN_API_VERSION_LEGACY:
        if not name or not all(c.isalnum() or c in "-_" for c in name):
            raise ManifestError(f"{plugin_dir.name}: invalid plugin name {name!r}")
    elif not _V1_NAME_RE.match(name):
        raise ManifestError(f"{plugin_dir.name}: invalid plugin name {name!r}")

    for key in raw:
        if key not in _ALLOWED_TOP_KEYS:
            raise ManifestError(f"{name}: unknown key {key!r}")
    for key in plugin:
        if key not in _ALLOWED_PLUGIN_KEYS:
            raise ManifestError(f"{name}: unknown [plugin] key {key!r}")

    entrypoint = str(plugin.get("entrypoint") or "").strip()
    if ":" not in entrypoint:
        raise ManifestError(f"{name}: entrypoint must be '<module>:<ClassName>'")

    capabilities = plugin.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        raise ManifestError(f"{name}: at least one capability is required")
    known = V0_KNOWN_CAPABILITIES if api_version == PLUGIN_API_VERSION_LEGACY else V1_KNOWN_CAPABILITIES
    unknown = [c for c in capabilities if c not in known]
    if unknown:
        raise ManifestError(f"{name}: unknown capabilities {unknown}")
    declared = {str(c) for c in capabilities}

    raw_settings = raw.get("settings", []) or []
    if not isinstance(raw_settings, list):
        raise ManifestError(f"{name}: [[settings]] must be a list")
    fields: list[PluginSettingField] = []
    for entry in raw_settings:
        if not isinstance(entry, dict) or not entry.get("key"):
            raise ManifestError(f"{name}: each [[settings]] entry needs a key")
        for key in entry:
            if key not in _ALLOWED_SETTING_KEYS:
                raise ManifestError(f"{name}: unknown [[settings]] key {key!r}")
        fields.append(
            PluginSettingField(
                key=str(entry["key"]),
                label=str(entry.get("label") or entry["key"]),
                help=str(entry.get("help") or ""),
                secret=bool(entry.get("secret", False)),
            )
        )

    raw_caps = raw.get("capability", []) or []
    if not isinstance(raw_caps, list):
        raise ManifestError(f"{name}: [[capability]] must be a list")
    cap_configs: list[PluginCapabilityConfig] = []
    for entry in raw_caps:
        if not isinstance(entry, dict) or not entry.get("id"):
            raise ManifestError(f"{name}: each [[capability]] entry needs an id")
        for key in entry:
            if key not in _ALLOWED_CAPABILITY_KEYS:
                raise ManifestError(f"{name}: unknown [[capability]] key {key!r}")
        cap_id = str(entry["id"])
        source = str(entry.get("source") or "")
        if source and not _V1_NAME_RE.match(source):
            raise ManifestError(f"{name}: [[capability]] source {source!r} must match ^[a-z0-9][a-z0-9-]{{0,31}}$")
        target_source = str(entry.get("target_source") or "")
        if target_source and target_source != "usenet" and not _PLUGIN_TARGET_RE.match(target_source):
            raise ManifestError(
                f"{name}: [[capability]] target_source {target_source!r} must be 'usenet' or a plugin source key"
            )
        cap_configs.append(
            PluginCapabilityConfig(
                id=cap_id,
                source=source,
                target_source=target_source,
                display_name=str(entry.get("display_name") or ""),
            )
        )
    outside = [c.id for c in cap_configs if c.id not in declared]
    if outside:
        raise ManifestError(f"{name}: [[capability]] ids {outside} must also appear in capabilities")
    if "indexer" in declared and "download_client" not in declared:
        if not any(c.id == "indexer" and c.target_source for c in cap_configs):
            raise ManifestError(f"{name}: indexer capability requires target_source ('usenet' or a plugin source key)")

    raw_schedule = raw.get("schedule")
    schedule: PluginScheduleConfig | None = None
    if raw_schedule is not None:
        if api_version == PLUGIN_API_VERSION_LEGACY:
            raise ManifestError(f"{name}: [schedule] requires api_version 1")
        if not isinstance(raw_schedule, dict):
            raise ManifestError(f"{name}: [schedule] must be a table")
        for key in raw_schedule:
            if key not in _ALLOWED_SCHEDULE_KEYS:
                raise ManifestError(f"{name}: unknown [schedule] key {key!r}")
        interval = raw_schedule.get("interval_minutes")
        if isinstance(interval, bool) or not isinstance(interval, int):
            raise ManifestError(f"{name}: [schedule] interval_minutes must be an integer in [5, 1440]")
        if not 5 <= interval <= 1440:
            raise ManifestError(f"{name}: [schedule] interval_minutes {interval} must be in [5, 1440]")
        run_on_load = raw_schedule.get("run_on_load", False)
        if not isinstance(run_on_load, bool):
            raise ManifestError(f"{name}: [schedule] run_on_load must be a boolean")
        schedule = PluginScheduleConfig(interval_minutes=interval, run_on_load=run_on_load)
    if "scheduler" in declared and schedule is None:
        raise ManifestError(f"{name}: scheduler capability requires a [schedule] table with interval_minutes in [5, 1440]")

    raw_routes = raw.get("route", []) or []
    if not isinstance(raw_routes, list):
        raise ManifestError(f"{name}: [[route]] must be a list")
    routes: list[PluginRouteSpec] = []
    if raw_routes:
        if api_version == PLUGIN_API_VERSION_LEGACY:
            raise ManifestError(f"{name}: [[route]] requires api_version 1")
        if "publisher" not in declared:
            raise ManifestError(f"{name}: [[route]] requires the publisher capability")
        for entry in raw_routes:
            if not isinstance(entry, dict):
                raise ManifestError(f"{name}: each [[route]] entry must be a table")
            for key in entry:
                if key not in _ALLOWED_ROUTE_KEYS:
                    raise ManifestError(f"{name}: unknown [[route]] key {key!r}")
            route_path = str(entry.get("path") or "")
            if not _ROUTE_PATH_RE.match(route_path):
                raise ManifestError(f"{name}: [[route]] path {route_path!r} must match ^[a-z0-9][a-z0-9/_-]{{0,63}}$")
            method = str(entry.get("method") or "GET")
            if method not in _ROUTE_METHODS:
                raise ManifestError(f"{name}: [[route]] method {method!r} must be one of GET, POST, DELETE")
            auth = str(entry.get("auth") or "admin")
            if auth not in _ROUTE_AUTHS:
                raise ManifestError(f"{name}: [[route]] auth {auth!r} must be admin or user")
            rate = entry.get("rate_limit_per_minute", 60)
            if isinstance(rate, bool) or not isinstance(rate, int):
                raise ManifestError(f"{name}: [[route]] rate_limit_per_minute must be an integer")
            if not 1 <= rate <= 600:
                raise ManifestError(f"{name}: [[route]] rate_limit_per_minute {rate} must be in [1, 600]")
            routes.append(PluginRouteSpec(path=route_path, method=method, auth=auth, rate_limit_per_minute=rate))

    raw_ui = raw.get("plugin_ui")
    ui_entry = ""
    ui_pages: list[str] = []
    ui_external_url = ""
    if raw_ui is not None:
        if api_version == PLUGIN_API_VERSION_LEGACY:
            raise ManifestError(f"{name}: [plugin_ui] requires api_version 1")
        if not isinstance(raw_ui, dict):
            raise ManifestError(f"{name}: [plugin_ui] must be a table")
        for key in raw_ui:
            if key not in _ALLOWED_UI_KEYS:
                raise ManifestError(f"{name}: unknown [plugin_ui] key {key!r}")
        ui_entry = str(raw_ui.get("entry") or "")
        if ui_entry:
            if ui_entry.startswith(("http://", "https://")) or ui_entry.startswith("/"):
                raise ManifestError(f"{name}: [plugin_ui] entry must stay inside the plugin dir")
            if ".." in ui_entry.replace("\\", "/").split("/"):
                raise ManifestError(f"{name}: [plugin_ui] entry must stay inside the plugin dir")
        raw_pages = raw_ui.get("pages", []) or []
        if not isinstance(raw_pages, list) or any(not isinstance(p, str) or not p for p in raw_pages):
            raise ManifestError(f"{name}: [plugin_ui] pages must be a list of page ids")
        if len(raw_pages) > 1:
            raise ManifestError(f"{name}: [plugin_ui] pages allows a single page id in v1")
        ui_pages = [str(p) for p in raw_pages]
        ui_external_url = str(raw_ui.get("external_url") or "")
        if ui_external_url:
            if ui_external_url.startswith("https://"):
                pass
            elif ui_external_url.startswith("http://"):
                ui_host = ui_external_url[len("http://"):].split("/", 1)[0].rsplit("@", 1)[-1].split(":")[0].strip("[]")
                if ui_host != "localhost" and not ui_host.startswith("127.") and ui_host not in ("::1",):
                    raise ManifestError(f"{name}: [plugin_ui] external_url http is only for localhost/loopback")
            else:
                raise ManifestError(f"{name}: [plugin_ui] external_url must be https:// (http only for localhost/loopback)")
        if ui_external_url and (ui_entry or ui_pages):
            raise ManifestError(f"{name}: [plugin_ui] entry/pages and external_url are mutually exclusive")

    return PluginManifest(
        name=name,
        version=str(plugin.get("version") or "0.0.0"),
        api_version=api_version,
        entrypoint=entrypoint,
        capabilities=[str(c) for c in capabilities],
        display_name=str(plugin.get("display_name") or name),
        description=str(plugin.get("description") or ""),
        author=str(plugin.get("author") or ""),
        homepage=str(plugin.get("homepage") or ""),
        settings=fields,
        capability_configs=cap_configs,
        schedule=schedule,
        routes=routes,
        ui_entry=ui_entry,
        ui_pages=ui_pages,
        ui_external_url=ui_external_url,
    )
