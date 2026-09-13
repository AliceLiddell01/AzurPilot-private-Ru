from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dev_tools import dock_progression_catalog as generator


@dataclass(frozen=True)
class _IdentityCatalog:
    records: tuple[object, ...] = ()
    fingerprint: str = "8" * 64


def test_lua_parser_ignores_braces_inside_single_quoted_strings() -> None:
    source = b"""
pg.ship_data_blueprint.all = { 29901 }
pg.base.ship_data_blueprint[29901] = {
 note = 'literal } { braces',
 id = 29901
}
"""

    assert generator.extract_blueprint_groups(source) == {29901}


def test_build_from_git_reads_identity_catalog_from_first_party_root(
    monkeypatch,
    tmp_path: Path,
) -> None:
    observed_paths: list[Path] = []

    def fake_read_pinned_blob(
        _repo: Path,
        *,
        commit: str,
        expected_commit: str,
        path: str,
        expected_blob_sha: str | None = None,
    ) -> tuple[bytes, str, str]:
        assert commit == expected_commit
        assert path
        if expected_blob_sha is not None:
            assert len(expected_blob_sha) == 40
        return b"{}", "1" * 40, expected_commit

    def fake_load_identity(path: Path) -> _IdentityCatalog:
        observed_paths.append(path)
        return _IdentityCatalog()

    monkeypatch.setattr(generator, "_read_pinned_blob", fake_read_pinned_blob)
    monkeypatch.setattr(generator, "load_dock_identity_catalog", fake_load_identity)
    monkeypatch.setattr(generator, "extract_supplemental_templates", lambda _data: {})
    monkeypatch.setattr(generator, "extract_blueprint_groups", lambda _data: set())
    monkeypatch.setattr(generator, "extract_maximum_level", lambda _data: 125)

    generator.build_from_git(
        tmp_path / "upstream-source",
        generator.SOURCE_COMMIT,
        tmp_path / "supplemental-source",
        generator.SUPPLEMENTAL_SOURCE_COMMIT,
    )

    assert observed_paths == [generator.IDENTITY_CATALOG_PATH]
    assert generator.IDENTITY_CATALOG_PATH == (
        generator.REPOSITORY_ROOT / "assets" / "ship" / "dock_identity_catalog.json"
    )
