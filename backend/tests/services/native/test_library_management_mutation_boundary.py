"""Regression guards for the staged Library Management mutation boundary."""


from core.dependencies.service_providers import (
    get_download_orchestrator,
    get_download_service,
    get_drop_import_service,
    get_file_processor,
    get_target_drop_import_service,
    get_target_file_processor,
)
from infrastructure.audio.tagger import AudioTagger
from infrastructure.persistence.native_library_store import NativeLibraryStore
from services.native.drop_import_service import DropImportService
from services.native.file_processor import FileProcessor
from services.native.target_catalog_writer_service import TargetCatalogWriterService


def test_obsolete_scalar_audio_mutators_are_not_public() -> None:
    for name in ("write_mb_tags", "write_album_identity", "write_cover_art"):
        assert not hasattr(AudioTagger, name)
    assert not hasattr(TargetCatalogWriterService, "update_tags")
    assert not hasattr(NativeLibraryStore, "update_target_track_tags")


def test_import_services_have_no_legacy_publication_implementation() -> None:
    for name in (
        "_import_into_library",
        "_replace_same_path",
        "_retire_replaced_file",
    ):
        assert not hasattr(FileProcessor, name)
    for name in ("_import_mapped", "_import_bonus", "_move_into_library"):
        assert not hasattr(DropImportService, name)


def test_download_and_drop_providers_converge_on_target_publication() -> None:
    """Functional replacement for the retired source-substring checks: every
    download/drop provider must hand back the TARGET service wired to the
    shared staged publisher (``publish_import_bundle``). A lane that loses the
    publisher hook raises at import time instead of writing files directly, so
    a None hook here means imports stopped going through E10's chain."""
    try:
        processor = get_file_processor()
        assert isinstance(processor, FileProcessor)
        # get_file_processor is a pure alias of the target builder.
        assert processor is get_target_file_processor()
        assert processor._publish_import_bundle is not None
        assert processor._policy_revision_getter is not None

        drop = get_drop_import_service()
        assert isinstance(drop, DropImportService)
        assert drop is get_target_drop_import_service()
        assert drop._publish_import_bundle is not None

        # The download lanes import through the SAME processor singleton.
        orchestrator = get_download_orchestrator()
        assert any(
            getattr(strategy, "_file_processor", None) is processor
            for strategy in orchestrator._strategies.values()
        )
        assert get_download_service()._file_processor is processor
    finally:
        from core.dependencies._registry import clear_all_singletons

        clear_all_singletons()


