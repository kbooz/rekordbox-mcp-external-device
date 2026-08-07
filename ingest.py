#!/usr/bin/env python
"""
Drain the download landing zones into the organised library on the external drive.

Scans every landing zone, drops anything already in the collection or duplicated
between zones, files the survivors under ``Music/Artists/<Artist>/`` and registers
them in rekordbox.

Dry-run by default -- nothing is copied or written until ``--apply`` is passed.
Imported tracks arrive **unanalysed**; run *Analyze Tracks* in rekordbox afterwards.

Usage:
    uv run python ingest.py                 # report only
    uv run python ingest.py --apply         # do it
    uv run python ingest.py --keep-source   # copy instead of move
"""

import argparse
import collections
import hashlib
import os
import re
import shutil
import sys
import unicodedata

from mutagen import File as MutagenFile
from pyrekordbox.db6 import Rekordbox6Database

DB_DIR = os.path.expanduser("~/Library/Pioneer/rekordbox")
LIBRARY = "/Volumes/KBOOZHD/Music/Artists"
LANDING_ZONES = [
    os.path.expanduser("~/Music/Downloader"),
    "/Volumes/KBOOZHD/Music/Downloads",
]
AUDIO_EXT = (".mp3", ".m4a", ".flac", ".wav", ".aiff", ".aif")

# A track already in the collection is never re-imported. These are the tests,
# cheapest first -- a file only reaches the next test if the previous misses.
#   1. filename        2. artist+title tag
#   3. audio hash      4. same title, size within 12%
SIZE_TOLERANCE = 0.12


def normalise(*parts: object) -> str:
    """Lowercase alphanumerics only -- survives punctuation and casing drift.

    NFC first: macOS stores accents decomposed (NFD) while rekordbox exports use
    NFC, so "Huve" with an umlaut compares unequal byte-wise without this.
    """
    joined = unicodedata.normalize("NFC", "".join(str(p or "") for p in parts))
    return re.sub(r"[^a-z0-9]", "", joined.lower())


def filename_key(path: str) -> str:
    """Comparable basename -- same NFC caveat as normalise()."""
    return unicodedata.normalize("NFC", os.path.basename(path)).lower()


def lead_artist(name: str) -> str:
    """First credited artist -- the one that names the folder."""
    return re.split(r"[,;&/]| feat| ft\.| Feat| x ", name or "")[0].strip()


def folder_name(name: str) -> str:
    cleaned = re.sub(r"[/:]", "-", name or "").strip().strip(".")
    return cleaned or "_Sem-Artista"


def audio_hash(path: str, size: int) -> str:
    """Size plus first and last megabyte. Collisions on real audio are negligible."""
    digest = hashlib.sha256(str(size).encode())
    with open(path, "rb") as handle:
        digest.update(handle.read(1 << 20))
        if size > (2 << 20):
            handle.seek(-(1 << 20), os.SEEK_END)
            digest.update(handle.read(1 << 20))
    return digest.hexdigest()


def read_tags(path: str) -> tuple[str, str]:
    try:
        tags = MutagenFile(path, easy=True)
        return (tags.get("artist") or [""])[0], (tags.get("title") or [""])[0]
    except Exception:  # noqa: BLE001 - unreadable tags are not fatal, treat as blank
        return "", ""


def register(db: Rekordbox6Database, path: str):
    """add_content() only records the file -- the tags have to be carried over
    explicitly, or the track shows up in rekordbox with no title and no artist."""
    content = db.add_content(path)
    try:
        tags = MutagenFile(path, easy=True) or {}
    except Exception:  # noqa: BLE001
        return content

    def tag(name):
        return (tags.get(name) or [""])[0].strip()

    if tag("title"):
        content.Title = tag("title")
    elif not content.Title:
        content.Title = os.path.splitext(os.path.basename(path))[0]
    for value, getter, adder, field in (
        (tag("artist"), db.get_artist, db.add_artist, "ArtistID"),
        (tag("album"), db.get_album, db.add_album, "AlbumID"),
        (tag("genre"), db.get_genre, db.add_genre, "GenreID"),
    ):
        if not value:
            continue
        row = getter(Name=value).first() or adder(value)
        setattr(content, field, row.ID)
    return content


class Collection:
    """Everything rekordbox already knows, indexed for duplicate lookups."""

    def __init__(self, db: Rekordbox6Database) -> None:
        self.filenames: set[str] = set()
        self.artist_titles: set[str] = set()
        self.by_size: dict[int, list[str]] = collections.defaultdict(list)
        self.sizes_by_title: dict[str, list[int]] = collections.defaultdict(list)
        for content in db.get_content():
            # get_content() still hands back soft-deleted rows -- a track the
            # user removed from rekordbox must not block its re-import.
            if getattr(content, "rb_local_deleted", 0):
                continue
            path = content.FolderPath or ""
            if not path or not os.path.exists(path):
                continue
            size = os.path.getsize(path)
            self.filenames.add(filename_key(path))
            artist = content.Artist.Name if content.Artist else ""
            self.artist_titles.add(normalise(artist, content.Title))
            self.by_size[size].append(path)
            self.sizes_by_title[normalise(content.Title)].append(size)

    def duplicate_reason(self, path: str, artist: str, title: str) -> str | None:
        if filename_key(path) in self.filenames:
            return "mesmo nome de arquivo"
        if normalise(artist, title) in self.artist_titles:
            return "mesmo artista+titulo"
        size = os.path.getsize(path)
        if size in self.by_size:
            mine = audio_hash(path, size)
            if any(audio_hash(other, size) == mine for other in self.by_size[size]):
                return "audio identico"
        known = self.sizes_by_title.get(normalise(title))
        if known and any(abs(s - size) / max(s, size) < SIZE_TOLERANCE for s in known):
            return "mesmo titulo, tamanho ~igual"
        return None


def scan(collection: Collection) -> tuple[list[dict], list[dict]]:
    """Split every file in the landing zones into keepers and skips."""
    keep: list[dict] = []
    skip: list[dict] = []
    claimed: set[str] = set()
    seen_here: dict[str, str] = {}

    for zone in LANDING_ZONES:
        if not os.path.isdir(zone):
            continue
        for root, _dirs, files in os.walk(zone):
            for name in sorted(files):
                if name.startswith("._") or not name.lower().endswith(AUDIO_EXT):
                    continue
                path = os.path.join(root, name)
                artist, title = read_tags(path)

                reason = collection.duplicate_reason(path, artist, title)
                if reason:
                    skip.append({"path": path, "reason": reason})
                    continue

                # duplicated between the two landing zones
                key = normalise(artist, title) or name.lower()
                if key in seen_here:
                    skip.append({"path": path, "reason": f"duplicata de {seen_here[key]}"})
                    continue
                seen_here[key] = path

                folder = os.path.join(LIBRARY, folder_name(lead_artist(artist)))
                dest = os.path.join(folder, name)
                if dest in claimed or os.path.exists(dest):
                    stem, ext = os.path.splitext(name)
                    n = 2
                    while (
                        os.path.join(folder, f"{stem} ({n}){ext}") in claimed
                        or os.path.exists(os.path.join(folder, f"{stem} ({n}){ext}"))
                    ):
                        n += 1
                    dest = os.path.join(folder, f"{stem} ({n}){ext}")
                claimed.add(dest)
                keep.append({"path": path, "dest": dest, "artist": artist, "title": title})

    return keep, skip


def report(keep: list[dict], skip: list[dict]) -> None:
    gigabytes = sum(os.path.getsize(k["path"]) for k in keep) / 1e9
    print(f"a importar : {len(keep)}  ({gigabytes:.1f} GB)")
    print(f"ignorados  : {len(skip)}")
    for reason, count in collections.Counter(s["reason"] for s in skip).most_common():
        print(f"    {count:5}  {reason}")
    folders = collections.Counter(os.path.basename(os.path.dirname(k["dest"])) for k in keep)
    print(f"pastas de artista tocadas: {len(folders)}")
    for name, count in folders.most_common(10):
        print(f"    {count:5}  {name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write; otherwise dry-run")
    parser.add_argument(
        "--keep-source", action="store_true", help="copy instead of moving out of the zone"
    )
    args = parser.parse_args()

    if not os.path.isdir(LIBRARY):
        print(f"biblioteca nao encontrada: {LIBRARY} -- o HD esta plugado?")
        return 1

    db = Rekordbox6Database(db_dir=DB_DIR)
    keep, skip = scan(Collection(db))
    report(keep, skip)

    if not keep:
        print("\nnada para importar.")
        return 0
    if not args.apply:
        print("\n(dry-run - nada gravado. rode com --apply)")
        return 0

    print("\nimportando...")
    imported = failed = 0
    for i, item in enumerate(keep, 1):
        try:
            os.makedirs(os.path.dirname(item["dest"]), exist_ok=True)
            shutil.copy2(item["path"], item["dest"])
            source_size = os.path.getsize(item["path"])
            if audio_hash(item["dest"], os.path.getsize(item["dest"])) != audio_hash(
                item["path"], source_size
            ):
                raise OSError("copia divergiu do original -- origem preservada")
            register(db, item["dest"])
            if not args.keep_source:
                os.remove(item["path"])
            imported += 1
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
            failed += 1
            if failed <= 10:
                print(f"  FALHA {os.path.basename(item['path'])}: {type(exc).__name__} {exc}")
        if i % 100 == 0:
            db.commit()
            print(f"  {i}/{len(keep)}...", flush=True)
    db.commit()

    print(f"\nimportadas: {imported} | falhas: {failed}")
    print("as novas entram SEM analise -- rode 'Analyze Tracks' no rekordbox.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
