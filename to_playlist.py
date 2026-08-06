#!/usr/bin/env python
"""
Put the tracks named on stdin into a rekordbox playlist, one filename per line.

Companion to ``ingest.py``. The landing-zone folder is the record of what a set
is made of, but ingest deletes it after archiving, and only the *new* files get
imported -- the ones already in the collection are skipped. So capture the names
first and resolve them here, and the playlist ends up with the whole folder,
new and old alike.

Matches on basename (NFC, lowercased) since ingest never renames, then on the
same name with ingest's " (2)" collision suffix, then on the file's own
artist+title tags -- the last one needs the line to be a readable path, so feed
it full paths.

Dry-run by default -- nothing is written until ``--apply`` is passed, and the
rekordbox app has to be closed for that.

Usage:
    find ~/Music/Downloader/"Warmup BK 4" -type f > /tmp/names.txt
    uv run python ingest.py --apply
    uv run python to_playlist.py "Warmup BK #4" < /tmp/names.txt
    uv run python to_playlist.py "Warmup BK #4" --parent 67134214 --apply < /tmp/names.txt
    uv run python to_playlist.py --selftest
"""

import argparse
import collections
import os
import re
import sys
import unicodedata

from pyrekordbox.db6 import Rekordbox6Database

from ingest import normalise, read_tags

DB_DIR = os.path.expanduser("~/Library/Pioneer/rekordbox")
AUDIO_EXT = (".mp3", ".m4a", ".flac", ".wav", ".aiff", ".aif")

# The one rename ingest.py does: " (2)" before the extension when the artist
# folder already holds that filename. Match it back or the archived copy looks
# like a track that was never imported.
COPY_SUFFIX = re.compile(r" \(\d+\)(?=\.[^.]+$)")


def key(name: str) -> str:
    """Comparable basename. NFC first: macOS writes accents decomposed."""
    return unicodedata.normalize("NFC", os.path.basename(name.strip())).lower()


def base_key(name: str) -> str:
    return COPY_SUFFIX.sub("", key(name))


def index_collection(db: Rekordbox6Database):
    """-> (exact basename index, copy-suffix-stripped index, artist+title index),
    each key mapping to a list of content IDs: two artist folders can hold the
    same filename, and picking one at random would be a silent wrong track."""
    exact: dict[str, list[str]] = collections.defaultdict(list)
    stripped: dict[str, list[str]] = collections.defaultdict(list)
    tagged: dict[str, list[str]] = collections.defaultdict(list)
    for content in db.get_content():
        if not content.FolderPath:
            continue
        exact[key(content.FolderPath)].append(str(content.ID))
        stripped[base_key(content.FolderPath)].append(str(content.ID))
        artist = content.Artist.Name if content.Artist else ""
        tagged[normalise(artist, content.Title)].append(str(content.ID))
    return exact, stripped, tagged


def resolve(names: list[str], exact: dict, stripped: dict, tagged: dict = None):
    """-> (ids in input order, missing names, ambiguous names).

    Exact name wins; the stripped index is the fallback so that a real file
    named "... (2).mp3" is not made ambiguous by its own unsuffixed twin. Last
    resort is artist+title off the file's own tags, which catches the copy the
    collection filed under a differently ordered name -- that only works for a
    line that is a readable path, so feed this full paths, not bare names.
    """
    ids, missing, ambiguous = [], [], []
    for name in names:
        found = exact.get(key(name)) or stripped.get(base_key(name), [])
        if not found and tagged and os.path.isfile(name):
            found = tagged.get(normalise(*read_tags(name)), [])
        if not found:
            missing.append(name)
        elif len(found) > 1:
            ambiguous.append(name)
        else:
            ids.append(found[0])
    return ids, missing, ambiguous


def find_playlist(db: Rekordbox6Database, name: str):
    wanted = unicodedata.normalize("NFC", name).lower()
    for playlist in db.get_playlist():
        if unicodedata.normalize("NFC", playlist.Name or "").lower() == wanted:
            return playlist
    return None


def selftest() -> int:
    exact = {"músicá.mp3": ["1"], "dois.mp3": ["2", "3"], "só copia (2).mp3": ["4"]}
    stripped = {"músicá.mp3": ["1"], "dois.mp3": ["2", "3"], "só copia.mp3": ["4"]}
    ids, missing, ambiguous = resolve(
        [
            unicodedata.normalize("NFD", "/some/dir/MÚSICÁ.mp3"),  # NFD input, NFC index
            "só copia.mp3",  # landing-zone name, archived as "(2)"
            "dois.mp3",
            "nada.mp3",
        ],
        exact,
        stripped,
    )
    assert ids == ["1", "4"], ids
    assert missing == ["nada.mp3"], missing
    assert ambiguous == ["dois.mp3"], ambiguous

    # a file genuinely named "(2)" must not be dragged into its twin's ambiguity
    twins_exact = {"x (2).mp3": ["9"], "x.mp3": ["8"]}
    twins_stripped = {"x.mp3": ["8", "9"]}
    ids, missing, ambiguous = resolve(["x (2).mp3", "x.mp3"], twins_exact, twins_stripped)
    assert (ids, missing, ambiguous) == (["9", "8"], [], []), (ids, missing, ambiguous)
    print("selftest ok")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("playlist", nargs="?", help="playlist name (created if missing)")
    parser.add_argument("--parent", help="parent folder ID for a playlist that has to be created")
    parser.add_argument("--apply", action="store_true", help="write; otherwise dry-run")
    parser.add_argument("--selftest", action="store_true", help="check the matching, no database")
    args = parser.parse_args()

    if args.selftest:
        return selftest()
    if not args.playlist:
        parser.error("playlist name is required")

    names = [line for line in (raw.strip() for raw in sys.stdin) if line.endswith(AUDIO_EXT)]
    if not names:
        print("nada na entrada -- esperava um nome de arquivo por linha.")
        return 1

    db = Rekordbox6Database(db_dir=DB_DIR)
    ids, missing, ambiguous = resolve(names, *index_collection(db))

    playlist = find_playlist(db, args.playlist)
    existing = set()
    if playlist:
        existing = {str(s.ContentID) for s in db.get_playlist_songs(PlaylistID=playlist.ID)}
    to_add = [i for i in dict.fromkeys(ids) if i not in existing]

    print(f"na entrada    : {len(names)}")
    print(f"resolvidas    : {len(ids)}")
    print(f"ja na playlist: {len(ids) - len(to_add)}")
    print(f"a adicionar   : {len(to_add)}")
    print(f"playlist      : {args.playlist} ({'existe' if playlist else 'sera criada'})")
    for label, items in (("nao encontradas", missing), ("ambiguas", ambiguous)):
        if items:
            print(f"\n{label} ({len(items)}):")
            for item in items:
                print(f"    {os.path.basename(item)}")

    if not args.apply:
        print("\n(dry-run - nada gravado. rode com --apply)")
        return 0
    if not to_add and playlist:
        print("\nnada a fazer.")
        return 0

    if not playlist:
        # pyrekordbox writes masterPlaylists6.xml here too -- without that the
        # playlist exists in every query and is invisible in the app.
        playlist = db.create_playlist(name=args.playlist, parent=args.parent)
    for content_id in to_add:
        db.add_to_playlist(playlist.ID, int(content_id))
    db.commit()

    print(f"\nadicionadas: {len(to_add)} em '{args.playlist}' (ID {playlist.ID})")
    return 1 if (missing or ambiguous) else 0


if __name__ == "__main__":
    sys.exit(main())
