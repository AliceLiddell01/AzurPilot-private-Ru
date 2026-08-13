"""Build current EventSpec/registry from an exact EN datamine snapshot."""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from module.event_datamine.artifact import (
    BUILTIN_ARTIFACT_ROOT,
    build_artifact,
    write_artifact,
)
from module.event_datamine.assets import write_asset_catalog
from module.event_datamine.compiler import EventCompiler
from module.event_datamine.discovery import (
    discover_major_events,
    resolve_current_candidate,
)
from module.event_datamine.generator import (
    generate_map_module,
    map_module_name,
    write_map_module,
)
from module.event_datamine.registry import write_registry
from module.event_datamine.source import ShareCfgLoader, SourceSnapshot


def verify_git_revision(root: Path, revision: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    actual = completed.stdout.strip()
    if actual != revision:
        raise ValueError(
            f"Source checkout HEAD {actual} не совпадает с pinned revision {revision}"
        )


def _map_status(spec) -> str:
    if spec.unknown_grid_types or spec.unknown_effects:
        return "unsupported"
    return "verified"


def build_current_event(
    *,
    source_root: Path,
    server: str,
    repository: str,
    revision: str,
    output_root: Path,
    asset_root: Path,
    now: datetime,
    maps_output: Path | None = None,
    overwrite: bool = False,
    verify_git: bool = True,
) -> dict:
    if verify_git:
        verify_git_revision(source_root, revision)
    snapshot = SourceSnapshot(source_root, server, repository, revision)
    loader = ShareCfgLoader(snapshot)
    candidates = discover_major_events(loader)
    current = resolve_current_candidate(candidates, server=server, now=now)
    if current is None:
        raise ValueError(f"Для {server.upper()} нет active/redemption major event")
    artifact_path = (
        output_root
        / "production"
        / f"{server.lower()}-{current.activity_id}.json"
    )
    if artifact_path.exists() and not overwrite:
        raise FileExistsError(artifact_path)
    spec = EventCompiler(loader).compile(current.activity_id)
    if {item.id for item in spec.maps} != set(current.map_ids):
        raise ValueError("Compiler map inventory не совпадает со structural discovery")

    event_maps_output = (
        maps_output / f"{server.lower()}_{current.activity_id}"
        if maps_output is not None
        else None
    )
    updated_maps = tuple(
        replace(item, source_status=_map_status(item)) for item in spec.maps
    )
    map_records = []
    map_writes: list[tuple[Path, str]] = []
    used_names: set[str] = set()
    for map_spec in updated_maps:
        status = map_spec.source_status
        base_name = map_module_name(map_spec.chapter_name)
        module_name = base_name
        if module_name in used_names:
            module_name = f"{base_name}_{map_spec.id}"
        if module_name in used_names:
            raise ValueError(f"Неуникальное имя generated map module: {module_name}")
        used_names.add(module_name)
        record = {
            "map_id": map_spec.id,
            "chapter_name": map_spec.chapter_name,
            "source_status": status,
            "module": (
                f"{server.lower()}_{current.activity_id}/{module_name}.py"
                if status == "verified"
                else ""
            ),
        }
        map_records.append(record)
        if event_maps_output is not None and status == "verified":
            target = event_maps_output / f"{module_name}.py"
            content = generate_map_module(map_spec)
            map_writes.append((target, content))

    if not overwrite:
        collisions = [target for target, _ in map_writes if target.exists()]
        if collisions:
            raise FileExistsError(collisions[0])

    if event_maps_output is not None:
        markers = (
            maps_output / "__init__.py",
            event_maps_output / "__init__.py",
        )
        for marker in markers:
            if marker.exists() and not marker.is_file():
                raise FileExistsError(marker)
        for marker in markers:
            if not marker.exists():
                write_map_module(
                    marker,
                    '"""Сгенерированные модули Event-карт; не редактировать вручную."""\n',
                )
        for target, content in map_writes:
            write_map_module(target, content, overwrite=overwrite)

    spec = replace(spec, maps=updated_maps)
    artifact = build_artifact(
        spec.to_dict(),
        compiler_version=str(EventCompiler.SCHEMA_VERSION),
        role="production",
        metadata={
            "discovery": {
                "mark": current.mark,
                "campaign_activity_ids": current.campaign_activity_ids,
                "candidate_count": len(candidates),
            },
            "generated_maps": map_records,
        },
    )
    write_artifact(artifact_path, artifact)
    write_registry(output_root)
    write_asset_catalog(output_root, asset_root=asset_root)
    return {
        "artifact": str(artifact_path),
        "digest": artifact["digest"],
        "event_id": spec.id,
        "event_name": spec.name,
        "source_status": spec.source_status,
        "revision": revision,
        "candidate_count": len(candidates),
        "map_count": len(spec.maps),
        "shop_count": len(spec.shop_items),
        "milestone_count": len(spec.milestones),
        "finding_codes": sorted({item.code for item in spec.findings}),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Structural discovery и сборка current EventSpec"
    )
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--server", choices=("CN", "EN", "JP", "TW", "KR"), required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--repository", default="AzurLaneTools/AzurLaneLuaScripts")
    parser.add_argument("--current", action="store_true", required=True)
    parser.add_argument("--now", help="Server-local ISO datetime для воспроизводимой selection")
    parser.add_argument("--output-root", type=Path, default=BUILTIN_ARTIFACT_ROOT)
    parser.add_argument(
        "--asset-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "assets",
    )
    parser.add_argument("--maps-output", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    now = datetime.fromisoformat(args.now) if args.now else datetime.now()
    result = build_current_event(
        source_root=args.source_root,
        server=args.server,
        repository=args.repository,
        revision=args.revision,
        output_root=args.output_root,
        asset_root=args.asset_root,
        now=now,
        maps_output=args.maps_output,
        overwrite=args.overwrite,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())