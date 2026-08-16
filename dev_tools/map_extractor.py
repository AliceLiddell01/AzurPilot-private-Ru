"""CLI для Event Datamine compiler; вся семантика находится в testable library API."""

from __future__ import annotations

import argparse
from pathlib import Path

from module.event_datamine.artifact import build_artifact, write_artifact
from module.event_datamine.compiler import EventCompiler
from module.event_datamine.generator import (
    allocate_map_module_names,
    generate_map_module,
    write_map_module,
)
from module.event_datamine.runtime_policy import (
    load_generated_runtime_policy,
    map_runtime_policy,
)
from module.event_datamine.source import ShareCfgLoader, SourceSnapshot


def select_maps(maps, selected_ids: set[int]):
    available_ids = {item.id for item in maps}
    missing_ids = sorted(selected_ids - available_ids)
    if missing_ids:
        raise SystemExit(f"Неизвестные map ID в EventSpec: {missing_ids}")
    return tuple(item for item in maps if not selected_ids or item.id in selected_ids)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Компиляция EventSpec из закреплённого AzurLaneLuaScripts snapshot"
    )
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument(
        "--server", choices=("CN", "EN", "JP", "TW", "KR"), required=True
    )
    parser.add_argument(
        "--revision", required=True, help="Полный Git SHA source snapshot"
    )
    parser.add_argument("--repository", default="AzurLaneTools/AzurLaneLuaScripts")
    parser.add_argument("--activity-id", type=int, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--maps-output", type=Path)
    parser.add_argument(
        "--map-id",
        type=int,
        action="append",
        help="Ограничить map generation указанными ID; можно повторять",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    snapshot = SourceSnapshot(
        args.source_root, args.server, args.repository, args.revision
    )
    spec = EventCompiler(ShareCfgLoader(snapshot)).compile(args.activity_id)
    write_artifact(args.artifact, build_artifact(spec.to_dict()))
    if args.maps_output:
        if not spec.eligible:
            raise SystemExit(
                "EventSpec не eligible для production map generation; см. findings artifact"
            )
        selected_ids = set(args.map_id or ())
        selected_maps = select_maps(spec.maps, selected_ids)
        module_names = allocate_map_module_names(selected_maps)
        package = f"{args.server.lower()}_{args.activity_id}"
        policy = load_generated_runtime_policy((package,))
        if policy is not None and policy["event_id"] != spec.id:
            raise SystemExit(
                "Runtime-policy generated package не соответствует EventSpec"
            )
        for item, module_name in zip(selected_maps, module_names, strict=True):
            path = args.maps_output / f"{module_name}.py"
            content = generate_map_module(
                item,
                runtime_policy=map_runtime_policy(
                    policy,
                    map_id=item.id,
                    chapter_name=item.chapter_name,
                ),
            )
            write_map_module(path, content, overwrite=args.overwrite)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
