from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import black_myth_save_manager as app  # noqa: E402


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def check_black_myth_rules(base: Path) -> None:
    source = base / "archive" / "main" / "save"
    destination = base / "game" / "76561190000000000"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    write_bytes(source / "ArchiveSaveFile.2.sav", b"new")
    write_bytes(source / "UserSettingSaveGame.sav", b"source-settings")
    write_bytes(destination / "ArchiveSaveFile.1.sav", b"old")
    write_bytes(destination / "ArchiveSaveFile.2.sav", b"old")
    write_bytes(destination / "UserSettingSaveGame.sav", b"current-settings")

    warnings = app.apply_save_folder(source, destination)
    assert warnings == []
    assert (destination / "ArchiveSaveFile.2.sav").read_bytes() == b"new"
    assert not (destination / "ArchiveSaveFile.1.sav").exists()
    assert (destination / "UserSettingSaveGame.sav").read_bytes() == b"current-settings"

    locked_destination = base / "game" / "locked"
    locked_destination.mkdir()
    write_bytes(locked_destination / "UserSettingSaveGame.sav", b"locked-settings")
    warnings = app.apply_save_folder(source, locked_destination, allow_locked=True)
    assert warnings == []
    assert (locked_destination / "UserSettingSaveGame.sav").read_bytes() == b"locked-settings"

    fresh_destination = base / "game" / "fresh"
    fresh_destination.mkdir()
    warnings = app.apply_save_folder(source, fresh_destination)
    assert warnings == []
    assert (fresh_destination / "UserSettingSaveGame.sav").read_bytes() == b"source-settings"

    empty = base / "archive" / "empty"
    empty.mkdir()
    root, nodes = app.scan_save_tree(base / "archive")
    assert root.total_save_dirs == 1
    assert str(empty) in nodes


def check_black_myth_detection(base: Path) -> None:
    savegames = base / "bmw-game" / "SaveGames"
    only_settings = savegames / "76561190000000001"
    write_bytes(only_settings / "UserSettingSaveGame.sav", b"settings")
    assert app.detect_actual_save_folder(savegames, game=app.BMW_GAME) is None
    write_bytes(only_settings / "ArchiveSaveFile.2.txt", b"not-save")
    assert app.detect_actual_save_folder(savegames, game=app.BMW_GAME) is None

    write_bytes(only_settings / "ArchiveSaveFile.2.sav", b"save")
    assert app.detect_actual_save_folder(savegames, game=app.BMW_GAME) == only_settings


def check_pbz_rules(base: Path) -> None:
    game = app.PBZ_GAME
    archive_root = base / "pbz-archive"
    save_folder = archive_root / "main"
    realtime = save_folder / "1RealTime"
    empty = archive_root / "empty"
    write_bytes(save_folder / "PBZSaveGame.sav", b"pbz-new")
    write_bytes(save_folder / "other.sav", b"ignored")
    write_bytes(save_folder / "steam_autocloud.vdf", b"cloud")
    write_bytes(realtime / "20261030-220000-000.sav", b"backup")
    empty.mkdir(parents=True)

    root, nodes = app.scan_save_tree(archive_root, game)
    save_node = nodes[str(save_folder)]
    assert root.total_save_dirs == 1
    assert str(empty) in nodes
    assert save_node.is_save_dir
    assert [item.name for item in save_node.display_files] == ["PBZSaveGame.sav"]
    assert {item.name for item in save_node.direct_files} == {
        "PBZSaveGame.sav",
        "other.sav",
        "steam_autocloud.vdf",
    }
    assert app.node_has_slim_source(save_node)
    assert app.node_has_slim_target(save_node)

    slim_targets = {path.relative_to(save_folder) for path in app.collect_slim_targets(save_node)}
    assert slim_targets == {
        Path("other.sav"),
        Path("steam_autocloud.vdf"),
        Path("1RealTime") / "20261030-220000-000.sav",
    }
    assert Path("PBZSaveGame.sav") not in slim_targets

    steam_root = base / "pbz-game" / "SaveGames" / "Steam"
    other_steam_id = steam_root / "76561190000000001"
    target_steam_id = steam_root / "76561190000000002"
    write_bytes(other_steam_id / "20261030-230000-000.sav", b"not-a-main-save")
    write_bytes(target_steam_id / "PBZSaveGame.sav", b"main-save")
    assert app.detect_actual_save_folder(steam_root, game=game) == target_steam_id
    assert app.resolve_current_save_folder(steam_root, game) == target_steam_id

    destination = base / "pbz-game" / "current" / "76561190000000002"
    write_bytes(destination / "PBZSaveGame.sav", b"pbz-old")
    write_bytes(destination / "steam_autocloud.vdf", b"keep-cloud")
    write_bytes(destination / "other.sav", b"keep-other")
    warnings = app.apply_save_folder(save_folder, destination, game=game)
    assert warnings == []
    assert (destination / "PBZSaveGame.sav").read_bytes() == b"pbz-new"
    assert (destination / "steam_autocloud.vdf").read_bytes() == b"keep-cloud"
    assert (destination / "other.sav").read_bytes() == b"keep-other"

    copied = base / "pbz-archive" / "copied"
    app.copy_save_folder(save_folder, copied, game)
    assert [item.name for item in copied.iterdir()] == ["PBZSaveGame.sav"]
    assert (copied / "PBZSaveGame.sav").read_bytes() == b"pbz-new"


def check_pbz_injection_helpers() -> None:
    bridge, oodle = app.pbz_bridge_source_paths()
    assert bridge.is_file()
    assert oodle.is_file()
    ok = app._parse_pbz_result("ok slots=2 injected=2 failed=0 error=", Path("bridge.log"))
    assert ok.ok and ok.slots == 2 and ok.injected == 2 and ok.failed == 0
    failed = app._parse_pbz_result("fail slots=2 injected=1 failed=1 error=bad", Path("bridge.log"))
    assert not failed.ok and failed.failed == 1 and failed.message == "bad"


def check_settings_isolation() -> None:
    legacy = {
        "version": 1,
        "source_directory": r"D:\old-archive",
        "current_save_directory": r"E:\old-current",
    }
    games = app.normalize_game_settings(legacy)
    assert games[app.BMW_GAME_KEY]["source_directory"] == r"D:\old-archive"
    assert games[app.BMW_GAME_KEY]["current_save_directory"] == r"E:\old-current"
    assert games[app.PBZ_GAME_KEY] == {"source_directory": "", "current_save_directory": ""}
    assert app.initial_game_key(legacy) == app.BMW_GAME_KEY

    current = {
        "game": app.PBZ_GAME_KEY,
        "games": {
            app.BMW_GAME_KEY: {"source_directory": "bmw-archive", "current_save_directory": "bmw-current"},
            app.PBZ_GAME_KEY: {"source_directory": "pbz-archive", "current_save_directory": "pbz-current"},
        },
    }
    games = app.normalize_game_settings(current)
    assert app.initial_game_key(current) == app.PBZ_GAME_KEY
    assert games[app.BMW_GAME_KEY]["source_directory"] == "bmw-archive"
    assert games[app.PBZ_GAME_KEY]["current_save_directory"] == "pbz-current"


def main() -> None:
    base = Path(tempfile.mkdtemp(prefix="save-manager-self-check-"))
    try:
        check_black_myth_rules(base)
        check_black_myth_detection(base)
        check_pbz_rules(base)
        check_pbz_injection_helpers()
        check_settings_isolation()
        print("self-check passed")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
