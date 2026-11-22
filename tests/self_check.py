from __future__ import annotations

import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import black_myth_save_manager as app  # noqa: E402
import trainer_core  # noqa: E402


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

    mods = app.normalize_mod_settings({})
    assert mods[app.BMW_GAME_KEY]["library_directory"].endswith(r"mod\黑猴")
    assert mods[app.PBZ_GAME_KEY]["library_directory"].endswith(r"mod\影之刃")

    column_widths = app.normalize_column_widths(
        {
            "mod_tree": {"name": 312.4, "target": "bad", "too_small": 1},
            "trainer_table": {"feature": 280, "too_large": 99999},
            "": {"name": 200},
        }
    )
    assert column_widths == {
        "mod_tree": {"name": 312},
        "trainer_table": {"feature": 280},
    }
    assert app.normalize_column_widths(None) == {}


def check_trainer_settings() -> None:
    defaults = trainer_core.default_trainer_settings()
    assert defaults[app.BMW_GAME_KEY]["hotkeys"]["infinite_health"] == "数字键 2"
    assert defaults[app.BMW_GAME_KEY]["hotkeys"]["super_damage"] == "Ctrl+数字键 1"
    assert defaults[app.PBZ_GAME_KEY]["hotkeys"]["unlock_nightmare"] == "Alt+数字键 2"
    assert defaults[app.PBZ_GAME_KEY]["values"]["damage_multiplier"] == 2.0
    assert [spec.key for spec in trainer_core.BMW_FEATURES][-1] == "game_speed"
    assert trainer_core.FEATURE_BY_KEY[app.BMW_GAME_KEY]["edit_attack"].action == "value_toggle"

    normalized = trainer_core.normalize_trainer_settings(
        {
            app.BMW_GAME_KEY: {
                "hotkeys": {
                    "infinite_health": "Alt+F2",
                    "super_damage": "",
                },
                "values": {"game_speed": 3.25, "edit_attack": 1500},
            },
            app.PBZ_GAME_KEY: {
                "hotkeys": {"game_speed": "Shift+F8", "unlock_nightmare": ""},
                "values": {"damage_multiplier": 3.5},
            },
        }
    )
    assert normalized[app.BMW_GAME_KEY]["hotkeys"]["infinite_health"] == "Alt+F2"
    assert normalized[app.BMW_GAME_KEY]["hotkeys"]["super_damage"] == ""
    assert normalized[app.BMW_GAME_KEY]["values"]["game_speed"] == 3.25
    assert normalized[app.BMW_GAME_KEY]["values"]["edit_attack"] == 1500
    assert normalized[app.PBZ_GAME_KEY]["hotkeys"]["game_speed"] == "Shift+F8"
    assert normalized[app.PBZ_GAME_KEY]["hotkeys"]["unlock_nightmare"] == ""
    assert normalized[app.PBZ_GAME_KEY]["values"]["damage_multiplier"] == 3.5

    ctrl_1 = trainer_core.parse_hotkey("Ctrl+数字键 1")
    assert ctrl_1.modifiers == 0x0002
    assert ctrl_1.virtual_key == 0x61
    decimal = trainer_core.parse_hotkey("数字键 .")
    assert decimal.virtual_key == 0x6E
    assert trainer_core.parse_hotkey("Shift+F4").virtual_key == 0x73
    assert trainer_core.parse_hotkey("", allow_empty=True) is None

    class FakeKeyEvent:
        def __init__(self, keysym: str, state: int = 0) -> None:
            self.keysym = keysym
            self.state = state

    captured = trainer_core.hotkey_from_tk_event(
        FakeKeyEvent("KP_2", 0x20000), modifiers=0x0001
    )
    assert captured.modifiers == 0x0001
    assert captured.virtual_key == 0x62
    assert captured.display == "Alt+数字键 2"


def check_trainer_patterns() -> None:
    bmw = Path(r"E:\SteamLibrary\steamapps\common\BlackMythWukong\b1\Binaries\Win64\b1-Win64-Shipping.exe")
    pbz = Path(r"E:\SteamLibrary\steamapps\common\PBZ\platform\steam\PBZ.exe")
    if bmw.is_file():
        matches = trainer_core.scan_executable_patterns(bmw, trainer_core.BMW_PATTERNS)
        assert set(matches) == set(trainer_core.BMW_PATTERNS)
        assert trainer_core.find_code_caves(bmw)
    if pbz.is_file():
        matches = trainer_core.scan_executable_patterns(pbz, trainer_core.PBZ_PATTERNS)
        assert set(matches) == set(trainer_core.PBZ_PATTERNS)
        assert trainer_core.find_code_caves(pbz)


def make_zip(path: Path, files: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)


def check_mod_detection(base: Path) -> None:
    bmw_library = base / "mods" / "bmw"
    make_zip(
        bmw_library / "TestCSharp.zip",
        {
            "b1/Binaries/Win64/CSharpLoader/Mods/TestCSharp/TestCSharp.dll": b"dll",
            "说明.txt": b"ignored",
        },
    )
    make_zip(
        bmw_library / "TestCSharpLoader.zip",
        {
            "b1/Binaries/Win64/CSharpLoader/CSharpManager.bin": b"manager",
            "b1/Binaries/Win64/CSharpLoader/b1cs.ini": b"[Settings]",
            "b1/Binaries/Win64/version.dll": b"proxy",
            "README.md": b"ignored",
        },
    )
    make_zip(bmw_library / "TestPak.zip", {"TestPak.pak": b"pak"})
    bmw = {item.name: item for item in app.scan_mod_library(bmw_library, app.BMW_GAME)}
    assert bmw["TestCSharp"].mod_type == "csharp_mod"
    assert bmw["TestCSharp"].target_root == "b1/Binaries/Win64/CSharpLoader/Mods/TestCSharp"
    loader = bmw["TestCSharpLoader"]
    assert loader.mod_type == "csharp_loader"
    assert loader.target_root == "b1/Binaries/Win64"
    assert {item.target_relative for item in loader.files} == {
        "b1/Binaries/Win64/CSharpLoader/CSharpManager.bin",
        "b1/Binaries/Win64/CSharpLoader/b1cs.ini",
        "b1/Binaries/Win64/version.dll",
    }
    assert bmw["TestPak"].mod_type == "pak_mod"
    assert bmw["TestPak"].target_root == "b1/Content/Paks/~mods"

    pbz_library = base / "mods" / "pbz"
    make_zip(
        pbz_library / "UE4SS-Core.zip",
        {
            "UE4SS-PBZ/dwmapi.dll": b"proxy",
            "UE4SS-PBZ/ue4ss/UE4SS.dll": b"core",
            "UE4SS-PBZ/ue4ss/Mods/Builtin/Scripts/main.lua": b"return",
        },
    )
    make_zip(
        pbz_library / "TestLua.zip",
        {
            "Mods/TestLua/Scripts/main.lua": b"return",
            "Mods/TestLua/enabled.txt": b"",
        },
    )
    make_zip(
        pbz_library / "WrappedLua.zip",
        {
            "WrappedLua/Scripts/main.lua": b"return",
            "WrappedLua/dlls/main.dll": b"dll",
            "WrappedLua/enabled.txt": b"",
        },
    )
    pbz = {item.name: item for item in app.scan_mod_library(pbz_library, app.PBZ_GAME)}
    assert pbz["UE4SS-Core"].mod_type == "ue4ss_core"
    assert pbz["UE4SS-Core"].target_root == "platform/steam"
    assert pbz["TestLua"].mod_type == "ue4ss_mod"
    assert pbz["TestLua"].target_root == "platform/steam/ue4ss/Mods/TestLua"
    assert pbz["WrappedLua"].mod_type == "ue4ss_mod"
    assert pbz["WrappedLua"].target_root == "platform/steam/ue4ss/Mods/WrappedLua"


def check_steam_launch_metadata(base: Path) -> None:
    library = base / "steam-library"
    steamapps = library / "steamapps"
    steamapps.mkdir(parents=True)
    manifest = steamapps / "appmanifest_123456.acf"
    write_bytes(
        manifest,
        b'"AppState"\n{\n\t"appid"\t\t"123456"\n\t"installdir"\t\t"BlackMythWukong"\n}\n',
    )

    assert app.parse_steam_appmanifest(manifest) == ("123456", "BlackMythWukong")
    assert app.steamapps_directories({library}) == {steamapps}
    assert app.find_game_steam_app_id(app.BMW_GAME, {library}) == "123456"
    assert app.find_game_steam_app_id(app.PBZ_GAME, {library}) == "4115450"

    launch_targets: list[str] = []
    original_startfile = app.os.startfile
    app.os.startfile = launch_targets.append
    try:
        ok, _message = app.launch_game_from_steam(app.PBZ_GAME)
    finally:
        app.os.startfile = original_startfile
    assert ok
    assert launch_targets == ["steam://rungameid/4115450"]


def check_mod_install_uninstall(base: Path) -> None:
    bmw_game = base / "game-bmw"
    (bmw_game / "b1" / "Binaries" / "Win64").mkdir(parents=True)
    (bmw_game / "b1" / "Content" / "Paks").mkdir(parents=True)
    library = base / "install-library"
    make_zip(
        library / "TestMod.zip",
        {
            "b1/Binaries/Win64/CSharpLoader/Mods/TestMod/TestMod.dll": b"new-mod",
            "b1/Binaries/Win64/CSharpLoader/Mods/TestMod/config.json": b"{}",
        },
    )
    descriptor = app.scan_mod_source(library / "TestMod.zip", app.BMW_GAME)
    metadata = base / "mod-installations.json"
    backup_root = base / "mod-backups"
    target = bmw_game / "b1" / "Binaries" / "Win64" / "CSharpLoader" / "Mods" / "TestMod" / "TestMod.dll"
    write_bytes(target, b"original")

    result = app.install_mod(descriptor, bmw_game, metadata, backup_root)
    assert result.ok and result.installation is not None
    assert target.read_bytes() == b"new-mod"
    installations = app.load_mod_installations(metadata)
    assert len(installations) == 1 and len(installations[0].files) == 2
    assert installations[0].backups

    with target.open("rb"):
        locked = app.uninstall_mod(installations[0], metadata)
    assert not locked.ok
    assert any("占用" in error or "WinError 32" in error for error in locked.errors)
    partial = app.load_mod_installations(metadata)
    assert len(partial) == 1 and partial[0].status == "partial"

    retried = app.uninstall_mod(partial[0], metadata)
    assert retried.ok
    assert target.read_bytes() == b"original"
    assert app.load_mod_installations(metadata) == []

    make_zip(
        library / "TestCSharpLoader.zip",
        {
            "b1/Binaries/Win64/CSharpLoader/CSharpManager.bin": b"manager",
            "b1/Binaries/Win64/CSharpLoader/b1cs.ini": b"[Settings]",
            "b1/Binaries/Win64/version.dll": b"proxy",
            "README.md": b"ignored",
        },
    )
    loader_descriptor = app.scan_mod_source(library / "TestCSharpLoader.zip", app.BMW_GAME)
    loader_manager = bmw_game / "b1" / "Binaries" / "Win64" / "CSharpLoader" / "CSharpManager.bin"
    loader_proxy = bmw_game / "b1" / "Binaries" / "Win64" / "version.dll"
    write_bytes(loader_proxy, b"original-proxy")
    loader_result = app.install_mod(loader_descriptor, bmw_game, metadata, backup_root)
    assert loader_result.ok and loader_result.installation is not None
    assert loader_manager.read_bytes() == b"manager"
    assert loader_proxy.read_bytes() == b"proxy"
    loader_installation = app.load_mod_installations(metadata)
    assert len(loader_installation) == 1 and len(loader_installation[0].files) == 3
    loader_uninstall = app.uninstall_mod(loader_installation[0], metadata)
    assert loader_uninstall.ok
    assert not loader_manager.exists()
    assert loader_proxy.read_bytes() == b"original-proxy"
    assert app.load_mod_installations(metadata) == []


def main() -> None:
    base = Path(tempfile.mkdtemp(prefix="save-manager-self-check-"))
    try:
        check_black_myth_rules(base)
        check_black_myth_detection(base)
        check_pbz_rules(base)
        check_pbz_injection_helpers()
        check_settings_isolation()
        check_trainer_settings()
        check_trainer_patterns()
        check_mod_detection(base)
        check_steam_launch_metadata(base)
        check_mod_install_uninstall(base)
        print("self-check passed")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
