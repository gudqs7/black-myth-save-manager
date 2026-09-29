from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import black_myth_save_manager as app  # noqa: E402


def main() -> None:
    base = Path(tempfile.mkdtemp(prefix="bmw-self-check-"))
    try:
        source = base / "archive" / "main" / "save"
        destination = base / "game" / "76561190000000000"
        source.mkdir(parents=True)
        destination.mkdir(parents=True)
        (source / "ArchiveSaveFile.2.sav").write_bytes(b"new")
        (source / "UserSettingSaveGame.sav").write_bytes(b"source-settings")
        (destination / "ArchiveSaveFile.1.sav").write_bytes(b"old")
        (destination / "ArchiveSaveFile.2.sav").write_bytes(b"old")
        (destination / "UserSettingSaveGame.sav").write_bytes(b"current-settings")

        warnings = app.apply_save_folder(source, destination)
        assert warnings == []
        assert (destination / "ArchiveSaveFile.2.sav").read_bytes() == b"new"
        assert not (destination / "ArchiveSaveFile.1.sav").exists()
        assert (destination / "UserSettingSaveGame.sav").read_bytes() == b"current-settings"

        locked_destination = base / "game" / "locked"
        locked_destination.mkdir()
        (locked_destination / "UserSettingSaveGame.sav").write_bytes(b"locked-settings")
        warnings = app.apply_save_folder(source, locked_destination, allow_locked=True)
        assert warnings == []
        assert (locked_destination / "UserSettingSaveGame.sav").read_bytes() == b"locked-settings"

        empty = base / "archive" / "empty"
        empty.mkdir()
        root, nodes = app.scan_save_tree(base / "archive")
        assert root.total_save_dirs == 1
        assert str(empty) in nodes
        print("self-check passed")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
