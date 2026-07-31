"""
Rekordbox Device Export Access

Read and write rekordbox exports on USB sticks / SD cards
(``PIONEER/rekordbox/export.pdb``).

This is the DeviceSQL format the CDJs read. It is a plain binary format --
NOT encrypted -- and is unrelated to the SQLCipher-encrypted local ``master.db``
handled by ``database.py``. The newer ``exportLibrary.db`` sitting next to it
*is* encrypted, but CDJs read the .pdb, so that is what we parse.

Writes patch the export in place. Every write takes a timestamped backup
(``export_backup_*.pdb``) beside the export, writes via a temp file + rename so
a crash can't leave a half-written database, and restores the backup if the
result no longer parses. Note that rekordbox regenerates the whole export on
its next sync, so edits made here do not survive re-exporting from rekordbox.
"""

import os
import shutil
import string
import sys
from datetime import datetime
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from .models import Playlist, Track

# Location of the export database relative to the device mount point
PDB_RELPATH = Path("PIONEER/rekordbox/export.pdb")


@lru_cache(maxsize=4)
def _load_pdb(pdb_path: str, mtime: float):
    """
    Parse an export.pdb file.

    Cached per (path, mtime) so repeated tool calls don't re-parse a multi-MB
    file; re-exporting to the device changes mtime and busts the cache.
    """
    try:
        from rekordbox_pdb import Database
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise RuntimeError(
            "Reading device exports requires the 'rekordbox-pdb' package. "
            "Install it with: uv sync"
        ) from exc

    logger.info(f"Parsing device export: {pdb_path}")
    return Database.from_file(pdb_path)


def _mount_roots() -> List[Path]:
    """Candidate mount points to scan for a rekordbox export."""
    if sys.platform == "darwin":
        return sorted(p for p in Path("/Volumes").glob("*") if p.is_dir())

    if sys.platform == "win32":
        return [
            Path(f"{letter}:/")
            for letter in string.ascii_uppercase
            if Path(f"{letter}:/").exists()
        ]

    # Linux: /media/<user>/<label>, /mnt/<label>, /run/media/<user>/<label>
    roots: List[Path] = []
    for base in (Path("/media"), Path("/mnt"), Path("/run/media")):
        if base.is_dir():
            roots.extend(p for p in base.glob("*") if p.is_dir())
            roots.extend(p for p in base.glob("*/*") if p.is_dir())
    return roots


def find_devices() -> List[Dict[str, Any]]:
    """
    Find mounted devices that contain a rekordbox export.

    Returns:
        List of dicts with the device name, mount path, and export.pdb path
    """
    devices: List[Dict[str, Any]] = []

    for root in _mount_roots():
        pdb = root / PDB_RELPATH
        try:
            if pdb.is_file():
                devices.append(
                    {
                        "name": root.name,
                        "path": str(root),
                        "pdb_path": str(pdb),
                    }
                )
        except OSError:
            # Unreadable or disappearing mount -- skip it rather than blow up
            continue

    return devices


def resolve_device(device_path: Optional[str] = None) -> Path:
    """
    Resolve which device to read.

    Args:
        device_path: Device mount point (or a direct path to export.pdb).
            When omitted, auto-detects -- but only if exactly one device is
            connected, so we never silently read the wrong stick.

    Returns:
        Path to the device mount point
    """
    if device_path:
        root = Path(device_path)
        if root.name == "export.pdb":
            root = root.parent.parent.parent
        if not (root / PDB_RELPATH).is_file():
            raise ValueError(
                f"No rekordbox export found at {root} (expected {PDB_RELPATH})"
            )
        return root

    devices = find_devices()
    if not devices:
        raise ValueError(
            "No connected rekordbox device export found. Connect a USB stick or "
            "SD card that was exported from rekordbox."
        )
    if len(devices) > 1:
        names = ", ".join(d["path"] for d in devices)
        raise ValueError(
            f"Multiple rekordbox devices connected ({names}). "
            "Pass device_path to choose one."
        )
    return Path(devices[0]["path"])


def _load_editor(pdb_path: str):
    """Open an export.pdb for in-place editing (never cached -- writes must be fresh)."""
    try:
        from rekordbox_pdb.edit import PdbEditor
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise RuntimeError(
            "Writing device exports requires the 'rekordbox-pdb' package. "
            "Install it with: uv sync"
        ) from exc

    return PdbEditor.from_file(pdb_path)


# Numeric track fields safe to patch in place, with their valid ranges.
# Everything else in the row is either an ID into another table or bookkeeping
# that readers don't consult -- patching those blind would corrupt the export.
EDITABLE_TRACK_FIELDS: Dict[str, range] = {
    "rating": range(0, 6),
    "color_id": range(0, 9),
    "play_count": range(0, 65536),
    "tempo": range(0, 65536 * 65536),  # BPM * 100
    "year": range(0, 65536),
    "track_number": range(0, 65536 * 65536),
    "disc_number": range(0, 65536),
}


def _normalize_rating(raw: Optional[int]) -> int:
    """
    Normalize a PDB rating to the 0-5 scale used by models.Track.

    Depending on the export version the field holds either 0-5 directly or
    0/51/102/.../255 (one 51-step per star).
    """
    rating = raw or 0
    if rating > 5:
        rating = round(rating / 51)
    return max(0, min(5, rating))


class DeviceDatabase:
    """
    View of a rekordbox export.pdb on a mounted device.

    Reads are cheap and safe. Writes patch the export in place, which is
    inherently riskier than editing the local library: rekordbox overwrites
    the whole export on its next sync, and the players are unforgiving about
    malformed rows. Every write therefore takes a timestamped backup first,
    writes atomically, and rolls back if the result won't parse.
    """

    def __init__(self, device_path: Optional[str] = None):
        self.root = resolve_device(device_path)
        self._reload()

    @property
    def pdb_path(self) -> Path:
        """Path to the export database on the device."""
        return self.root / PDB_RELPATH

    def _reload(self) -> None:
        """(Re)parse the export and rebuild the name lookups."""
        pdb = self.pdb_path
        self._db = _load_pdb(str(pdb), pdb.stat().st_mtime)

        self._artists = {a.id: a.name for a in self._db.artists}
        self._albums = {a.id: a.name for a in self._db.albums}
        self._genres = {g.id: g.name for g in self._db.genres}
        self._keys = {k.id: k.name for k in self._db.keys}
        self._colors = {c.id: c.name for c in self._db.colors}

        # cached_property values are derived from _db, so drop them too
        self.__dict__.pop("tracks", None)
        self.__dict__.pop("playlists", None)

    def _to_track(self, raw: Any) -> Track:
        """Map a PDB track row onto the shared Track model."""
        relative = (raw.file_path or "").lstrip("/")

        return Track(
            id=str(raw.id),
            title=raw.title or raw.filename or "",
            artist=self._artists.get(raw.artist_id, ""),
            album=self._albums.get(raw.album_id),
            genre=self._genres.get(raw.genre_id),
            # PDB stores BPM as int * 100, same convention as the local database
            bpm=(raw.tempo or 0) / 100,
            key=self._keys.get(raw.key_id),
            rating=_normalize_rating(raw.rating),
            play_count=max(0, raw.play_count or 0),
            length=max(0, raw.duration or 0),
            # Absolute path on the mounted device, so the file is directly usable
            file_path=str(self.root / relative) if relative else None,
            date_added=raw.date_added or None,
            bitrate=raw.bitrate or None,
            sample_rate=raw.sample_rate or None,
            color=self._colors.get(raw.color_id),
            comments=raw.comment or None,
        )

    @cached_property
    def tracks(self) -> List[Track]:
        """All tracks on the device."""
        return [self._to_track(t) for t in self._db.tracks]

    @cached_property
    def playlists(self) -> List[Playlist]:
        """
        Playlist tree from the device.

        Device exports have no smart playlists -- rekordbox materializes them
        into plain playlists at export time -- so is_smart_playlist is always False.
        """
        counts: Dict[int, int] = {}
        for entry in self._db.playlist_entries:
            counts[entry.playlist_id] = counts.get(entry.playlist_id, 0) + 1

        nodes = sorted(
            self._db.playlist_tree, key=lambda n: (n.parent_id, n.sort_order)
        )
        return [
            Playlist(
                id=str(node.id),
                name=node.name,
                # parent_id 0 means "top level" in the PDB tree
                parent_id=str(node.parent_id) if node.parent_id else None,
                is_folder=node.is_folder,
                is_smart_playlist=False,
                track_count=0 if node.is_folder else counts.get(node.id, 0),
            )
            for node in nodes
        ]

    def playlist_tracks(self, playlist_id: str) -> List[Track]:
        """Tracks in a device playlist, in the DJ-visible order."""
        try:
            pid = int(playlist_id)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid playlist ID: {playlist_id}")

        if not any(node.id == pid for node in self._db.playlist_tree):
            raise ValueError(f"Playlist {playlist_id} not found on device {self.root}")

        by_id = {t.id: t for t in self._db.tracks}
        entries = sorted(
            (e for e in self._db.playlist_entries if e.playlist_id == pid),
            key=lambda e: e.entry_index,
        )
        # Entries can outlive their track rows on a partially re-exported stick
        return [
            self._to_track(by_id[e.track_id]) for e in entries if e.track_id in by_id
        ]

    def search(self, query: str = "", limit: int = 50) -> List[Track]:
        """Substring search over title, artist, album and genre."""
        needle = query.strip().lower()
        matches = [
            track
            for track in self.tracks
            if not needle
            or needle
            in f"{track.title} {track.artist} {track.album or ''} {track.genre or ''}".lower()
        ]
        return matches[:limit]

    def summary(self) -> Dict[str, Any]:
        """Counts for a quick overview of what is on the device."""
        return {
            "name": self.root.name,
            "path": str(self.root),
            "total_tracks": len(self._db.tracks),
            "total_playlists": sum(1 for p in self.playlists if not p.is_folder),
            "total_folders": sum(1 for p in self.playlists if p.is_folder),
        }

    # -- writes ---------------------------------------------------------------

    def backup(self) -> Path:
        """Copy the current export.pdb to a timestamped backup beside it."""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        destination = self.pdb_path.with_name(f"export_backup_{stamp}.pdb")

        # A second write inside the same second must not clobber the first backup
        suffix = 1
        while destination.exists():
            destination = self.pdb_path.with_name(f"export_backup_{stamp}_{suffix}.pdb")
            suffix += 1

        shutil.copy2(self.pdb_path, destination)
        logger.info(f"Backed up device export to {destination}")
        return destination

    def _write(self, mutate: Callable[[Any], Dict[str, Any]]) -> Dict[str, Any]:
        """
        Apply a mutation to the export, safely.

        Backup -> mutate -> atomic replace -> re-parse to prove the result is
        readable, restoring the backup if it isn't. The backup is only kept when
        the file was actually touched, so failed calls don't litter the stick.
        """
        pdb = self.pdb_path
        tracks_before = len(self._db.tracks)
        backup = self.backup()

        try:
            editor = _load_editor(str(pdb))
            result = mutate(editor)
            payload = editor.to_bytes()
        except Exception:
            # Nothing was written yet -- drop the backup we just made
            backup.unlink(missing_ok=True)
            raise

        # Write beside the target, then rename: a crash mid-write can't leave a
        # half-written export.pdb that the CDJ would choke on.
        temporary = pdb.with_name(pdb.name + ".tmp")
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, pdb)
        except Exception:
            temporary.unlink(missing_ok=True)
            backup.unlink(missing_ok=True)
            raise

        _load_pdb.cache_clear()

        try:
            self._reload()
            if len(self._db.tracks) < tracks_before:
                raise ValueError(
                    f"track count dropped from {tracks_before} to {len(self._db.tracks)}"
                )
        except Exception as exc:
            shutil.copy2(backup, pdb)
            _load_pdb.cache_clear()
            self._reload()
            raise RuntimeError(
                f"Write produced an unusable export.pdb ({exc}); "
                f"restored the original from {backup}"
            ) from exc

        return {"backup_path": str(backup), **result}

    def create_playlist(
        self,
        name: str,
        parent_id: Optional[str] = None,
        is_folder: bool = False,
    ) -> Dict[str, Any]:
        """Create a playlist or folder on the device."""
        if not name.strip():
            raise ValueError("Playlist name cannot be empty")

        parent = self._require_node(parent_id, must_be_folder=True) if parent_id else 0

        def mutate(editor: Any) -> Dict[str, Any]:
            new_id = editor.create_playlist(name, parent_id=parent, is_folder=is_folder)
            return {
                "playlist_id": str(new_id),
                "name": name,
                "parent_id": str(parent) if parent else None,
                "is_folder": is_folder,
            }

        return self._write(mutate)

    def add_tracks_to_playlist(
        self, playlist_id: str, track_ids: List[str]
    ) -> Dict[str, Any]:
        """Append tracks to a device playlist, in the order given."""
        pid = self._require_node(playlist_id, must_be_folder=False)

        known = {t.id for t in self._db.tracks}
        wanted: List[int] = []
        missing: List[str] = []
        for raw_id in track_ids:
            try:
                tid = int(raw_id)
            except (TypeError, ValueError):
                missing.append(str(raw_id))
                continue
            if tid in known:
                wanted.append(tid)
            else:
                missing.append(str(raw_id))

        if not wanted:
            raise ValueError(
                f"None of the given track IDs exist on device {self.root}: {track_ids}"
            )

        def mutate(editor: Any) -> Dict[str, Any]:
            for tid in wanted:
                editor.add_to_playlist(pid, tid)
            return {
                "playlist_id": playlist_id,
                "added": [str(t) for t in wanted],
                "skipped": missing,
            }

        return self._write(mutate)

    def set_track_field(self, track_id: str, field: str, value: int) -> Dict[str, Any]:
        """
        Patch a numeric field on a device track (rating, color, tempo, ...).

        Only fields in EDITABLE_TRACK_FIELDS are allowed -- the rest of the row
        is either a foreign key or bookkeeping that must stay consistent.
        """
        if field not in EDITABLE_TRACK_FIELDS:
            allowed = ", ".join(sorted(EDITABLE_TRACK_FIELDS))
            raise ValueError(f"Field '{field}' is not editable. Allowed: {allowed}")

        allowed_range = EDITABLE_TRACK_FIELDS[field]
        if value not in allowed_range:
            raise ValueError(
                f"{field} must be between {allowed_range.start} and "
                f"{allowed_range.stop - 1}, got {value}"
            )

        try:
            tid = int(track_id)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid track ID: {track_id}")

        if not any(t.id == tid for t in self._db.tracks):
            raise ValueError(f"Track {track_id} not found on device {self.root}")

        def mutate(editor: Any) -> Dict[str, Any]:
            editor.set_track_field(tid, field, value)
            return {"track_id": track_id, "field": field, "value": value}

        return self._write(mutate)

    def _require_node(self, node_id: Optional[str], must_be_folder: bool) -> int:
        """Resolve a playlist/folder ID, raising if it's missing or the wrong kind."""
        try:
            nid = int(node_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError(f"Invalid playlist ID: {node_id}")

        node = next((n for n in self._db.playlist_tree if n.id == nid), None)
        if node is None:
            raise ValueError(f"Playlist {node_id} not found on device {self.root}")
        if must_be_folder and not node.is_folder:
            raise ValueError(f"'{node.name}' is a playlist, not a folder")
        if not must_be_folder and node.is_folder:
            raise ValueError(f"'{node.name}' is a folder, not a playlist")

        return nid
