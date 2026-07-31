"""
Tests for device export reading (PIONEER/rekordbox/export.pdb).

Uses stub PDB rows rather than a fixture .pdb binary -- the parsing itself is
rekordbox-pdb's job, what we verify here is our mapping and tree logic.
"""

from dataclasses import dataclass
from pathlib import Path

import pytest

from rekordbox_mcp import device as device_mod
from rekordbox_mcp.device import DeviceDatabase, _normalize_rating, resolve_device


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


@pytest.fixture
def stub_device(monkeypatch, tmp_path):
    """A DeviceDatabase backed by stub PDB rows, rooted at a temp 'mount point'."""
    tracks = [
        _Track(id=10, title="Deep Cut", tempo=12400),
        _Track(id=11, title="Peak Timer", tempo=13000, rating=204),
        _Track(id=12, title="Closer", file_path=""),
    ]
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

    monkeypatch.setattr(device_mod, "resolve_device", lambda p=None: tmp_path)
    monkeypatch.setattr(
        device_mod, "_load_pdb", lambda *a: _StubPdb(tracks, tree, entries)
    )
    monkeypatch.setattr(Path, "stat", lambda self: type("S", (), {"st_mtime": 0.0})())

    return DeviceDatabase()


def test_normalize_rating_handles_both_scales():
    assert _normalize_rating(0) == 0
    assert _normalize_rating(4) == 4
    assert _normalize_rating(204) == 4  # 51-per-star encoding
    assert _normalize_rating(255) == 5
    assert _normalize_rating(None) == 0
    assert _normalize_rating(999) == 5  # clamped, never trips Track's le=5


def test_track_mapping(stub_device, tmp_path):
    track = next(t for t in stub_device.tracks if t.id == "10")

    assert track.title == "Deep Cut"
    assert track.artist == "Artist A"
    assert track.album == "Album A"
    assert track.genre == "House"
    assert track.key == "8A"
    assert track.bpm == 124.0  # tempo is stored as BPM * 100
    assert track.file_path == str(tmp_path / "Contents/file.mp3")


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
