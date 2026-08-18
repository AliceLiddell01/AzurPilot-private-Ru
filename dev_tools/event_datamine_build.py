"""Сборка current EventSpec/registry из точного snapshot datamine выбранного сервера."""

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
    allocate_map_module_names,
    generate_map_module,
    map_module_path,
    write_map_module,
)
from module.event_datamine.registry import write_registry
from module.event_datamine.runtime_policy import (
    GENERATED_EVENT_ROOT,
    EventRuntimePolicyError,
    load_generated_runtime_policy,
    map_runtime_policy,
    runtime_map_policies,
    validate_runtime_template_assets,
)
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

    status = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    if status.stdout.strip():
        raise ValueError(
            f"Source checkout {root} содержит незакоммиченные или неотслеживаемые "
            f"изменения; pinned revision {revision} не воспроизводим"
        )


def _has_topology_hole(spec) -> bool:
    matrices = (
        getattr(spec, "map_data", ()),
        getattr(spec, "map_data_loop", ()) or (),
    )
    return any(cell == "??" for matrix in matrices for row in matrix for cell in row)


def _map_status(spec) -> str:
    if spec.unknown_grid_types or spec.unknown_effects:
        return "unsupported"
    if _has_topology_hole(spec):
        return "partial"
    return "verified"


def _has_spawn_kind(spec, kind: str) -> bool:
    groups = (
        getattr(spec, "spawn_data", ()),
        getattr(spec, "spawn_data_loop", ()) or (),
    )
    return any(
        int(row.get(kind, 0) or 0) > 0
        for rows in groups
        for row in rows
    )


def _runtime_map_status(spec, source_status: str, policy) -> tuple[str, str]:
    if source_status != "verified":
        return "unsupported", "source_not_verified"
    if _has_spawn_kind(spec, "siren") and (
        policy is None or policy.siren_recognition is None
    ):
        return "unsupported", "siren_recognition_missing"
    if policy is None or policy.boss_clear is None:
        return "unsupported", "boss_clear_missing"
    if policy.camera_calibration is None:
        return "unsupported", "camera_calibration_missing"
    if policy.detector_calibration is None:
        return "unsupported", "detector_calibration_missing"
    if policy.battle_plan is None:
        return "unsupported", "battle_plan_missing"
    return "verified", ""


def _event_maps_output(maps_output: Path, package: str) -> Path:
    base = maps_output.resolve()
    target = (base / package).resolve()
    if target.parent != base:
        raise ValueError(
            f"Generated campaign package вышел за пределы maps output: {package!r}"
        )
    return target


def _remove_stale_generated_modules(
    event_maps_output: Path,
    expected_targets: set[Path],
) -> None:
    """Удалить только старые Python-модули внутри явно generated package."""

    if not event_maps_output.is_dir():
        return
    for existing in event_maps_output.glob("*.py"):
        if existing.name == "__init__.py":
            continue
        resolved = existing.resolve()
        if resolved.parent != event_maps_output:
            raise ValueError(
                f"Generated module вышел за пределы package: {existing}"
            )
        if resolved not in expected_targets:
            existing.unlink()


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
    runtime_policy_root: Path | str = GENERATED_EVENT_ROOT,
) -> dict:
    if verify_git:
        verify_git_revision(source_root, revision)
    snapshot = SourceSnapshot(source_root, server, repository, revision)
    loader = ShareCfgLoader(snapshot)
    candidates = discover_major_events(loader)
    current = resolve_current_candidate(candidates, server=server, now=now)
    if current is None:
        raise ValueError(
            f"Для {server.upper()} нет active/redemption major event"
        )
    artifact_path = (
        output_root
        / "production"
        / f"{server.lower()}-{current.activity_id}.json"
    )
    if artifact_path.exists() and not overwrite:
        raise FileExistsError(artifact_path)
    spec = EventCompiler(loader).compile(current.activity_id)
    if {item.id for item in spec.maps} != set(current.map_ids):
        raise ValueError(
            "Compiler map inventory не совпадает со structural discovery"
        )

    package = f"{server.lower()}_{current.activity_id}"
    runtime_policy = load_generated_runtime_policy(
        (package,),
        root=runtime_policy_root,
    )
    if runtime_policy is not None and runtime_policy["event_id"] != spec.id:
        raise EventRuntimePolicyError(
            "Runtime-policy generated package не соответствует "
            "скомпилированному EventSpec"
        )
    policy_maps = (
        runtime_map_policies(runtime_policy)
        if runtime_policy is not None
        else {}
    )
    unknown_policy_maps = set(policy_maps) - {item.id for item in spec.maps}
    if unknown_policy_maps:
        raise EventRuntimePolicyError(
            f"Runtime-policy содержит карты вне EventSpec: "
            f"{sorted(unknown_policy_maps)}"
        )

    event_maps_output = (
        _event_maps_output(maps_output, package)
        if maps_output is not None
        else None
    )
    updated_maps = tuple(
        replace(item, source_status=_map_status(item))
        for item in spec.maps
    )
    module_names = allocate_map_module_names(updated_maps)
    map_records = []
    map_writes: list[tuple[Path, str]] = []
    for map_spec, module_name in zip(
        updated_maps,
        module_names,
        strict=True,
    ):
        source_status = map_spec.source_status
        policy = map_runtime_policy(
            runtime_policy,
            map_id=map_spec.id,
            chapter_name=map_spec.chapter_name,
        )
        if policy is not None:
            validate_runtime_template_assets(
                policy,
                server=server,
                asset_root=asset_root,
            )
        runtime_status, runtime_reason = _runtime_map_status(
            map_spec,
            source_status,
            policy,
        )
        record = {
            "map_id": map_spec.id,
            "chapter_name": map_spec.chapter_name,
            "source_status": source_status,
            "runtime_status": runtime_status,
            "runtime_reason": runtime_reason,
            "module": (
                f"{package}/{module_name}.py"
                if runtime_status == "verified"
                else ""
            ),
        }
        map_records.append(record)
        if event_maps_output is not None and runtime_status == "verified":
            target = map_module_path(event_maps_output, module_name)
            content = generate_map_module(
                map_spec,
                runtime_policy=policy,
            )
            map_writes.append((target, content))

    if not overwrite:
        collisions = [
            target
            for target, _ in map_writes
            if target.exists()
        ]
        if collisions:
            raise FileExistsError(collisions[0])

    if event_maps_output is not None:
        markers = (
            maps_output.resolve() / "__init__.py",
            event_maps_output / "__init__.py",
        )
        for marker in markers:
            if marker.exists() and not marker.is_file():
                raise FileExistsError(marker)
        for marker in markers:
            if not marker.exists():
                write_map_module(
                    marker,
                    '"""Сгенерированные модули Event-карт; '
                    'не редактировать вручную."""\n',
                )
        for target, content in map_writes:
            write_map_module(
                target,
                content,
                overwrite=overwrite,
            )
        if overwrite:
            _remove_stale_generated_modules(
                event_maps_output,
                {target.resolve() for target, _ in map_writes},
            )

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
        "runtime_map_count": sum(
            1
            for item in map_records
            if item["runtime_status"] == "verified"
        ),
        "shop_count": len(spec.shop_items),
        "milestone_count": len(spec.milestones),
        "finding_codes": sorted(
            {item.code for item in spec.findings}
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Structural discovery и сборка current EventSpec"
    )
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument(
        "--server",
        choices=("CN", "EN", "JP", "TW", "KR"),
        required=True,
    )
    parser.add_argument("--revision", required=True)
    parser.add_argument(
        "--repository",
        default="AzurLaneTools/AzurLaneLuaScripts",
    )
    parser.add_argument(
        "--current",
        action="store_true",
        required=True,
    )
    parser.add_argument(
        "--now",
        required=True,
        help="Server-local ISO datetime для воспроизводимой selection",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=BUILTIN_ARTIFACT_ROOT,
    )
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
    now = datetime.fromisoformat(args.now)
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
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
