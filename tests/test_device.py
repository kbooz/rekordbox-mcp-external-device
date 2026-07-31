"""
Tests for device export reading and writing (PIONEER/rekordbox/export.pdb).

The device tree is real (a temp directory laid out like a mounted stick) so
backup, atomic replace and rollback are exercised against the filesystem.
Only the PDB binary parsing/encoding is stubbed -- that is rekordbox-pdb's job.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import pytest

from rekordbox_mcp import device as device_mod
from rekordbox_mcp.device import DeviceDatabase, _normalize_rating, resolve_device

ORIGINAL_BYTES = b"ORIGINAL-PDB"
MUTATED_BYTES = b"MUTATED-PDB"


@dataclass
class _Named:
    id: int
    name: str


@dataclass
class _Node:
    id: int
    parent_id: int
    sort_order: int
    name: str
    is_folder: bool


@dataclass
class _Entry:
    entry_index: int
    track_id: int
    playlist_id: int


@dataclass
class _Track:
    id: int
    title: str = "Title"
    filename: str = "file.mp3"
    file_path: str = "/Contents/file.mp3"
    artist_id: int = 1
    album_id: int = 1
    genre_id: int = 1
    key_id: int = 1
    color_id: int = 0
    tempo: int = 12800
    duration: int = 200
    rating: int = 0
    play_count: int = 0
    bitrate: int = 320
    sample_rate: int = 44100
    date_added: str = "2024-01-01"
    comment: str = ""


class _StubPdb:
    def __init__(self, tracks, playlist_tree, playlist_entries):
        self.tracks = tracks
        self.playlist_tree = playlist_tree
        self.playlist_entries = playlist_entries
        self.artists = [_Named(1, "Artist A")]
        self.albums = [_Named(1, "Album A")]
        self.genres = [_Named(1, "House")]
        self.keys = [_Named(1, "8A")]
        self.colors = [_Named(1, "Red")]


@dataclass
class _StubEditor:
    """Stands in for rekordbox_pdb.edit.PdbEditor."""

    calls: List[tuple] = field(default_factory=list)
    raise_on_mutate: bool = False

    def create_playlist(self, name, parent_id=0, is_folder=False):
        if self.raise_on_mutate:
            raise ValueError("boom")
        self.calls.append(("create_playlist", name, parent_id, is_folder))
        return 99

    def add_to_playlist(self, playlist_id, track_id, entry_index=None):
        if self.raise_on_mutate:
            raise ValueError("boom")
        self.calls.append(("add_to_playlist", playlist_id, track_id))

    def set_track_field(self, track_id, field_name, value):
        if self.raise_on_mutate:
            raise ValueError("boom")
        self.calls.append(("set_track_field", track_id, field_name, value))

    def to_bytes(self):
        return MUTATED_BYTES


def _make_pdb(tracks=None):
    tracks = (
        tracks
        if tracks is not None
        else [
            _Track(id=10, title="Deep Cut", tempo=12400),
            _Track(id=11, title="Peak Timer", tempo=13000, rating=204),
            _Track(id=12, title="Closer", file_path=""),
        ]
    )
    tree = [
        _Node(id=1, parent_id=0, sort_order=0, name="Sets", is_folder=True),
        _Node(id=2, parent_id=1, sort_order=0, name="Warmup", is_folder=False),
    ]
    entries = [
        # Deliberately out of order, plus one entry pointing at a missing track
        _Entry(entry_index=2, track_id=10, playlist_id=2),
        _Entry(entry_index=1, track_id=11, playlist_id=2),
        _Entry(entry_index=3, track_id=999, playlist_id=2),
    ]
    return _StubPdb(tracks, tree, entries)


@pytest.fixture
def device_root(tmp_path):
    """A temp directory laid out like a mounted rekordbox device."""
    pdb = tmp_path / device_mod.PDB_RELPATH
    pdb.parent.mkdir(parents=True)
    pdb.write_bytes(ORIGINAL_BYTES)
    return tmp_path


@pytest.fixture
def harness(monkeypatch, device_root):
    """DeviceDatabase over the temp device, with PDB parsing/editing stubbed."""
    state = {"pdb": _make_pdb(), "editor": _StubEditor(), "reload_error": None}

    def fake_load(path, mtime):
        if state["reload_error"]:
            raise state["reload_error"]
        return state["pdb"]

    fake_load.cache_clear = lambda: None

    monkeypatch.setattr(device_mod, "_load_pdb", fake_load)
    monkeypatch.setattr(device_mod, "_load_editor", lambda path: state["editor"])

    state["device"] = DeviceDatabase(str(device_root))
    state["root"] = device_root
    state["pdb_path"] = device_root / device_mod.PDB_RELPATH
    return state


@pytest.fixture
def stub_device(harness):
    return harness["device"]


def _backups(root: Path) -> List[Path]:
    return sorted((root / device_mod.PDB_RELPATH).parent.glob("export_backup_*.pdb"))


# -- reading -----------------------------------------------------------------


def test_normalize_rating_handles_both_scales():
    assert _normalize_rating(0) == 0
    assert _normalize_rating(4) == 4
    assert _normalize_rating(204) == 4  # 51-per-star encoding
    assert _normalize_rating(255) == 5
    assert _normalize_rating(None) == 0
    assert _normalize_rating(999) == 5  # clamped, never trips Track's le=5


def test_track_mapping(stub_device, device_root):
    track = next(t for t in stub_device.tracks if t.id == "10")

    assert track.title == "Deep Cut"
    assert track.artist == "Artist A"
    assert track.album == "Album A"
    assert track.genre == "House"
    assert track.key == "8A"
    assert track.bpm == 124.0  # tempo is stored as BPM * 100
    assert track.file_path == str(device_root / "Contents/file.mp3")


def test_track_without_file_path_has_none(stub_device):
    track = next(t for t in stub_device.tracks if t.id == "12")
    assert track.file_path is None


def test_playlists_expose_tree_and_counts(stub_device):
    playlists = {p.name: p for p in stub_device.playlists}

    assert playlists["Sets"].is_folder is True
    assert playlists["Sets"].parent_id is None  # parent_id 0 means top level
    assert playlists["Warmup"].parent_id == "1"
    assert playlists["Warmup"].track_count == 3
    assert playlists["Sets"].track_count == 0


def test_playlist_tracks_are_ordered_and_skip_missing(stub_device):
    tracks = stub_device.playlist_tracks("2")

    # Sorted by entry_index, and the entry for the absent track 999 is dropped
    assert [t.title for t in tracks] == ["Peak Timer", "Deep Cut"]


def test_playlist_tracks_rejects_unknown_playlist(stub_device):
    with pytest.raises(ValueError, match="not found"):
        stub_device.playlist_tracks("42")

    with pytest.raises(ValueError, match="Invalid playlist ID"):
        stub_device.playlist_tracks("not-a-number")


def test_search_matches_across_fields(stub_device):
    assert [t.title for t in stub_device.search("peak")] == ["Peak Timer"]
    assert len(stub_device.search("house")) == 3  # genre matches every track
    assert len(stub_device.search("", limit=2)) == 2
    assert stub_device.search("nothing here") == []


def test_resolve_device_accepts_mount_point_and_pdb_path(device_root):
    assert resolve_device(str(device_root)) == device_root
    assert resolve_device(str(device_root / device_mod.PDB_RELPATH)) == device_root


def test_resolve_device_rejects_path_without_export(tmp_path):
    with pytest.raises(ValueError, match="No rekordbox export found"):
        resolve_device(str(tmp_path))


def test_resolve_device_errors_when_nothing_connected(monkeypatch):
    monkeypatch.setattr(device_mod, "find_devices", lambda: [])
    with pytest.raises(ValueError, match="No connected rekordbox device"):
        resolve_device()


def test_resolve_device_requires_choice_when_ambiguous(monkeypatch):
    monkeypatch.setattr(
        device_mod,
        "find_devices",
        lambda: [{"path": "/Volumes/A"}, {"path": "/Volumes/B"}],
    )
    with pytest.raises(ValueError, match="Multiple rekordbox devices"):
        resolve_device()


# -- writing -----------------------------------------------------------------


def test_create_playlist_writes_and_backs_up(harness):
    result = harness["device"].create_playlist("Club Set", parent_id="1")

    assert result["playlist_id"] == "99"
    assert harness["pdb_path"].read_bytes() == MUTATED_BYTES

    backups = _backups(harness["root"])
    assert len(backups) == 1
    assert backups[0].read_bytes() == ORIGINAL_BYTES
    assert result["backup_path"] == str(backups[0])


def test_create_playlist_validates_name_and_parent(harness):
    device = harness["device"]

    with pytest.raises(ValueError, match="cannot be empty"):
        device.create_playlist("   ")

    # Node 2 is a playlist, so it cannot hold children
    with pytest.raises(ValueError, match="not a folder"):
        device.create_playlist("Nested", parent_id="2")

    with pytest.raises(ValueError, match="not found"):
        device.create_playlist("Nested", parent_id="404")

    # Nothing was written, so no backup should be left behind
    assert _backups(harness["root"]) == []
    assert harness["pdb_path"].read_bytes() == ORIGINAL_BYTES


def test_add_tracks_reports_added_and_skipped(harness):
    result = harness["device"].add_tracks_to_playlist("2", ["10", "999", "nope", "11"])

    assert result["added"] == ["10", "11"]
    assert result["skipped"] == ["999", "nope"]
    assert harness["editor"].calls == [
        ("add_to_playlist", 2, 10),
        ("add_to_playlist", 2, 11),
    ]


def test_add_tracks_rejects_folder_target(harness):
    with pytest.raises(ValueError, match="not a playlist"):
        harness["device"].add_tracks_to_playlist("1", ["10"])


def test_add_tracks_rejects_when_no_id_exists(harness):
    with pytest.raises(ValueError, match="None of the given track IDs"):
        harness["device"].add_tracks_to_playlist("2", ["999"])
    assert _backups(harness["root"]) == []


def test_set_track_field_validates_field_and_range(harness):
    device = harness["device"]

    with pytest.raises(ValueError, match="not editable"):
        device.set_track_field("10", "artist_id", 3)

    with pytest.raises(ValueError, match="must be between 0 and 5"):
        device.set_track_field("10", "rating", 9)

    with pytest.raises(ValueError, match="not found"):
        device.set_track_field("777", "rating", 3)

    assert harness["pdb_path"].read_bytes() == ORIGINAL_BYTES
    assert _backups(harness["root"]) == []


def test_set_track_field_applies_patch(harness):
    result = harness["device"].set_track_field("10", "rating", 5)

    assert result["field"] == "rating" and result["value"] == 5
    assert harness["editor"].calls == [("set_track_field", 10, "rating", 5)]
    assert harness["pdb_path"].read_bytes() == MUTATED_BYTES


def test_failed_mutation_leaves_export_and_stick_untouched(harness):
    harness["editor"].raise_on_mutate = True

    with pytest.raises(ValueError, match="boom"):
        harness["device"].create_playlist("Doomed")

    assert harness["pdb_path"].read_bytes() == ORIGINAL_BYTES
    assert _backups(harness["root"]) == []  # no orphan backup on the stick


def test_unreadable_result_is_rolled_back(harness):
    """If the written file won't parse, the backup is restored automatically."""
    original_load = device_mod._load_pdb

    def fail_after_write(path, mtime):
        if Path(path).read_bytes() == MUTATED_BYTES:
            raise ValueError("corrupt page header")
        return original_load(path, mtime)

    fail_after_write.cache_clear = lambda: None
    device_mod._load_pdb = fail_after_write
    try:
        with pytest.raises(RuntimeError, match="restored the original"):
            harness["device"].create_playlist("Doomed")
    finally:
        device_mod._load_pdb = original_load

    assert harness["pdb_path"].read_bytes() == ORIGINAL_BYTES
    assert len(_backups(harness["root"])) == 1  # kept as evidence


def test_track_loss_triggers_rollback(harness):
    """A write that drops track rows is treated as corruption, not success."""
    shrunk = _make_pdb(tracks=[_Track(id=10)])

    def load_shrunk(path, mtime):
        if Path(path).read_bytes() == MUTATED_BYTES:
            return shrunk
        return _make_pdb()

    load_shrunk.cache_clear = lambda: None
    # Restored by the monkeypatch fixture that installed the original stub
    device_mod._load_pdb = load_shrunk

    with pytest.raises(RuntimeError, match="track count dropped from 3 to 1"):
        harness["device"].create_playlist("Doomed")

    assert harness["pdb_path"].read_bytes() == ORIGINAL_BYTES


def test_backups_do_not_collide_within_the_same_second(harness):
    harness["device"].create_playlist("One")
    harness["pdb_path"].write_bytes(ORIGINAL_BYTES)  # simulate a second edit round
    harness["device"].create_playlist("Two")

    assert len(_backups(harness["root"])) == 2
