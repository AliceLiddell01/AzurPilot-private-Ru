from pathlib import Path
from types import SimpleNamespace

import pytest

import dev_tools.map_extractor as extractor
from module.event_datamine.generator import allocate_map_module_names


def _map(map_id: int, chapter_name: str):
    return SimpleNamespace(id=map_id, chapter_name=chapter_name)


def test_map_module_allocator_disambiguates_duplicate_chapter_names():
    names = allocate_map_module_names(
        (_map(2050051, "EXTRA"), _map(2050052, "EXTRA"))
    )

    assert names == ("extra", "extra_2050052")


def test_map_module_allocator_rejects_secondary_suffix_collision():
    maps = (_map(1, "A"), _map(99, "A_2"), _map(2, "A"))

    with pytest.raises(ValueError, match="Неуникальное имя generated map module: a_2"):
        allocate_map_module_names(maps)


def test_map_extractor_uses_unique_allocated_paths(tmp_path: Path, monkeypatch):
    maps = (_map(2050051, "EXTRA"), _map(2050052, "EXTRA"))
    spec = SimpleNamespace(
        maps=maps,
        eligible=True,
        id="en:51101",
        to_dict=lambda: {"id": "en:51101"},
    )

    class _Compiler:
        def __init__(self, _loader):
            pass

        def compile(self, _activity_id):
            return spec

    writes: list[Path] = []
    monkeypatch.setattr(extractor, "SourceSnapshot", lambda *args: object())
    monkeypatch.setattr(extractor, "ShareCfgLoader", lambda _snapshot: object())
    monkeypatch.setattr(extractor, "EventCompiler", _Compiler)
    monkeypatch.setattr(extractor, "build_artifact", lambda _spec: {})
    monkeypatch.setattr(extractor, "write_artifact", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(extractor, "generation_patches_for", lambda *_args: ())
    monkeypatch.setattr(
        extractor, "generate_map_module", lambda *_args, **_kwargs: "pass\n"
    )
    monkeypatch.setattr(
        extractor,
        "write_map_module",
        lambda path, _content, **_kwargs: writes.append(Path(path)),
    )

    result = extractor.main(
        [
            "--source-root",
            str(tmp_path),
            "--server",
            "EN",
            "--revision",
            "a" * 40,
            "--activity-id",
            "51101",
            "--artifact",
            str(tmp_path / "artifact.json"),
            "--maps-output",
            str(tmp_path / "maps"),
        ]
    )

    assert result == 0
    assert [path.name for path in writes] == ["extra.py", "extra_2050052.py"]
