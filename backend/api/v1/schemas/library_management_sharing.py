"""Wire contracts for portable Library Management profiles."""

from __future__ import annotations

from typing import Literal

import msgspec

from api.v1.schemas.library_management import (
    LibraryManagementProfile,
    NamingScriptSettings,
    TaggingScriptSettings,
)
from infrastructure.msgspec_fastapi import AppStruct


class LibraryManagementProfileExportRequest(AppStruct):
    expected_settings_revision: str


class LibraryManagementProfileExportResponse(AppStruct):
    filename: str
    mime_type: str
    document: str
    share_code: str
    bundle_hash: str
    settings_revision: str


class LibraryManagementProfileImportPreviewRequest(AppStruct):
    content: str
    expected_settings_revision: str


class LibraryManagementProfileImportRequest(AppStruct):
    content: str
    reviewed_bundle_hash: str
    name: str
    expected_settings_revision: str


class LibraryManagementProfileImportWarning(AppStruct):
    code: str
    severity: Literal["warning", "danger"]
    title: str
    message: str


class LibraryManagementProfileImportPreviewResponse(AppStruct):
    profile: LibraryManagementProfile
    bundle_hash: str
    settings_revision: str
    naming_scripts: list[NamingScriptSettings] = msgspec.field(default_factory=list)
    tagging_scripts: list[TaggingScriptSettings] = msgspec.field(default_factory=list)
    aspects: list[str] = msgspec.field(default_factory=list)
    warnings: list[LibraryManagementProfileImportWarning] = msgspec.field(
        default_factory=list
    )


class LibraryManagementProfileImportResponse(AppStruct):
    profile: LibraryManagementProfile
    settings_revision: str
    naming_scripts: list[NamingScriptSettings] = msgspec.field(default_factory=list)
    tagging_scripts: list[TaggingScriptSettings] = msgspec.field(default_factory=list)
