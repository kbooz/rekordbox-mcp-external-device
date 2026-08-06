#!/usr/bin/env python
"""Restaura cues e beatgrid do pendrive /Volumes/Kbooz para a coleção do HD.

O rekordbox reanalisou as faixas depois da cópia para o HD, jogando fora a grade
manual e sem trazer os cues. Os ANLZ do pendrive (PIONEER/USBANLZ) são a única
cópia sobrevivente: PCO2 traz hotcues/memories, PQTZ/PQT2 traz a grade.

  uv run python restore_from_usb.py             # relatório, não grava nada
  uv run python restore_from_usb.py --apply     # grava (rekordbox FECHADO)
  uv run python restore_from_usb.py --selftest  # checa parser e splice

Casa ANLZ com faixa por tamanho de arquivo (empate por md5) porque o export
trunca o nome do arquivo no pendrive. Só preenche o que falta: nunca sobrescreve
cue existente nem ocupa slot de hotcue já usado.
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import unicodedata
import uuid
from collections import defaultdict
from datetime import datetime, timezone

# ponytail: pyrekordbox 0.4.3 valida Consts do ANLZ que o rekordbox 7 mudou.
# Só leio tags com ele, nunca reescrevo via build(), então relaxar é seguro.
import construct.core as _cc

_orig_const_parse = _cc.Const._parse


def _lenient_const(self, stream, context, path):
    try:
        return _orig_const_parse(self, stream, context, path)
    except _cc.ConstError:
        return self.value


_cc.Const._parse = _lenient_const

from pyrekordbox import Rekordbox6Database  # noqa: E402
from pyrekordbox.anlz import AnlzFile  # noqa: E402
from pyrekordbox.db6.tables import DjmdCue  # noqa: E402
from sqlalchemy import text  # noqa: E402

USB = "/Volumes/Kbooz"
USBANLZ = USB + "/PIONEER/USBANLZ"
RB_DIR = os.path.expanduser("~/Library/Pioneer/rekordbox")
SHARE = os.path.join(RB_DIR, "share")
BACKUP_DIR = "/Volumes/KBOOZHD/Music"

# Derivado empiricamente das 910 faixas em que os dois lados concordam:
# djmdCue.Kind pula o 4, então os slots A..H são 1,2,3,5,6,7,8,9.
HOT2KIND = {1: 1, 2: 2, 3: 3, 4: 5, 5: 6, 6: 7, 7: 8, 8: 9}

# PCO2 guarda a cor como RGB; o banco guarda um índice de tabela.
RGB2CTI = {
    (255, 0, 23): 43,
    (0, 196, 255): 8,
    (51, 255, 0): 23,
    (77, 0, 255): 60,
    (0, 255, 48): 19,
    (255, 140, 0): 36,
    (0, 0, 255): 1,
    (255, 232, 0): 32,
    (255, 94, 0): 38,
    (0, 255, 0): 21,
}

NO_LOOP = 0xFFFFFFFF
MEM_TOLERANCE_MS = 15


def guard_rekordbox_closed():
    if subprocess.run(["pgrep", "-f", "rekordbo[x][.]app"],
                      capture_output=True).returncode == 0:
        sys.exit("rekordbox está aberto — feche antes de gravar.")


def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " +00:00"


# --------------------------------------------------------------------------
# leitura do pendrive
# --------------------------------------------------------------------------

def parse_pco2(raw):
    """Lê as tags PCO2 do .EXT byte a byte.

    pyrekordbox 0.4.3 assume len_entry=48 em toda entrada PCP2, mas memory cues
    usam 44 (não carregam os 4 bytes de cor no fim) — o que fazia 216 faixas,
    justamente as com memories, falharem no parse.
    """
    hot, mem = [], []
    for start, end in find_tags(raw, b"PCO2"):
        body = raw[start:end]
        len_header = int.from_bytes(body[4:8], "big")
        is_hot = int.from_bytes(body[12:16], "big") == 1
        pos = len_header
        while pos + 44 <= len(body):
            e = body[pos:]
            if e[:4] != b"PCP2":
                break
            len_entry = int.from_bytes(e[8:12], "big")
            if len_entry < 44:
                break
            len_comment = int.from_bytes(e[40:44], "big")
            entry = {
                "hot_cue": int.from_bytes(e[12:16], "big"),
                "type": e[16],
                "time": int.from_bytes(e[20:24], "big"),
                "loop_time": int.from_bytes(e[24:28], "big"),
                "loop_enumerator": int.from_bytes(e[36:38], "big"),
                "loop_denominator": int.from_bytes(e[38:40], "big"),
                "comment": e[44:44 + len_comment].decode("utf-16-be", "replace").rstrip("\x00"),
            }
            tail = 44 + len_comment
            if len_entry >= tail + 4:
                # color_code é exatamente o djmdCue.ColorTableIndex
                entry["color_code"] = e[tail]
                entry["color_red"], entry["color_green"], entry["color_blue"] = \
                    e[tail + 1], e[tail + 2], e[tail + 3]
            (hot if is_hot else mem).append(entry)
            pos += len_entry
    return hot, mem


def read_usb_anlz(dat):
    """Devolve {'ppth', 'hot', 'mem', 'beats', 'tempo'} de um par ANLZ."""
    rec = {"ppth": None, "hot": [], "mem": [], "beats": 0, "tempo": None, "ext_ok": False}
    for tag in AnlzFile.parse_file(dat).tags:
        tn = type(tag).__name__
        if tn == "PPTHAnlzTag":
            rec["ppth"] = tag.get()
        elif tn == "PQTZAnlzTag":
            entries = tag.content.entries
            rec["beats"] = len(entries)
            if entries:
                rec["tempo"] = entries[0].tempo
    ext = dat[:-4] + ".EXT"
    if os.path.exists(ext):
        rec["hot"], rec["mem"] = parse_pco2(open(ext, "rb").read())
        rec["ext_ok"] = True
    return rec


def index_usb():
    out = []
    for dirpath, _, filenames in os.walk(USBANLZ):
        if "ANLZ0000.DAT" not in filenames:
            continue
        dat = os.path.join(dirpath, "ANLZ0000.DAT")
        try:
            rec = read_usb_anlz(dat)
        except Exception as exc:
            out.append({"dat": dat, "err": str(exc)[:70]})
            continue
        rec["dat"] = dat
        ppth = rec["ppth"]
        if ppth:
            for cand in (USB + ppth,
                         USB + "/" + unicodedata.normalize("NFC", ppth.lstrip("/")),
                         USB + "/" + unicodedata.normalize("NFD", ppth.lstrip("/"))):
                if os.path.isfile(cand):
                    rec["file"] = cand
                    rec["size"] = os.path.getsize(cand)
                    break
        out.append(rec)
    return out


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# splice de tag ANLZ em bytes crus (nunca reserializa o resto do arquivo)
# --------------------------------------------------------------------------

def find_tags(raw, magic):
    """Devolve [(inicio, fim)] de todas as tags `magic` dentro do ANLZ."""
    out = []
    pos = int.from_bytes(raw[4:8], "big")  # len_header do PMAI
    end = int.from_bytes(raw[8:12], "big")
    while pos + 12 <= min(end, len(raw)):
        len_tag = int.from_bytes(raw[pos + 8:pos + 12], "big")
        if len_tag <= 0:
            break
        if raw[pos:pos + 4] == magic:
            out.append((pos, pos + len_tag))
        pos += len_tag
    return out


def find_tag(raw, magic):
    """Devolve (inicio, fim) da primeira tag `magic`, ou None."""
    hits = find_tags(raw, magic)
    return hits[0] if hits else None


def splice_tag(dst_path, src_path, magic):
    """Copia a tag `magic` de src para dst, corrigindo len_file. Devolve bool."""
    dst = open(dst_path, "rb").read()
    src = open(src_path, "rb").read()
    a, b = find_tag(dst, magic) or (None, None)
    c, d = find_tag(src, magic) or (None, None)
    if a is None or c is None:
        return False
    new = dst[:a] + src[c:d] + dst[b:]
    new = new[:8] + len(new).to_bytes(4, "big") + new[12:]
    with open(dst_path, "wb") as fh:
        fh.write(new)
    return True


# --------------------------------------------------------------------------
def cue_rows_for(rec, existing):
    """Cues do pendrive que faltam no banco. `existing` = (kinds ocupados, tempos de memory)."""
    used_kinds, mem_times = existing
    rows = []
    for entry in rec["hot"]:
        kind = HOT2KIND.get(int(entry["hot_cue"]))
        if kind is None or kind in used_kinds:
            continue
        used_kinds.add(kind)
        rows.append(_row(entry, kind))
    for entry in rec["mem"]:
        t = int(entry["time"])
        if any(abs(t - m) <= MEM_TOLERANCE_MS for m in mem_times):
            continue
        mem_times.append(t)
        rows.append(_row(entry, 0))
    return rows


def _row(entry, kind):
    t = int(entry["time"])
    loop_time = int(entry.get("loop_time", NO_LOOP))
    is_loop = loop_time not in (0, NO_LOOP)
    if "color_code" in entry:
        cti = entry["color_code"]
    else:
        rgb = (entry.get("color_red", 0), entry.get("color_green", 0),
               entry.get("color_blue", 0))
        cti = RGB2CTI.get(rgb, 0)
    num = int(entry.get("loop_enumerator", 0) or 0)
    den = int(entry.get("loop_denominator", 0) or 0)
    return dict(
        InMsec=t, InFrame=int(t * 0.15), InMpegFrame=0, InMpegAbs=0,
        OutMsec=loop_time if is_loop else -1,
        OutFrame=int(loop_time * 0.15) if is_loop else 0,
        OutMpegFrame=0, OutMpegAbs=0,
        Kind=kind, Color=255, ColorTableIndex=cti, ActiveLoop=0,
        Comment=(entry.get("comment") or ""),
        BeatLoopSize=((num << 16) | den) if is_loop and num else 0,
        CueMicrosec=0, InPointSeekInfo=None, OutPointSeekInfo=None,
    )


def selftest():
    """Checa as duas peças que mexem em bytes: o parser de PCO2 e o splice."""
    import glob
    import tempfile

    exts = sorted(glob.glob(USBANLZ + "/*/*/ANLZ0000.EXT"))
    assert exts, f"pendrive não montado em {USB}"

    # 1. parse_pco2 tem que concordar com o pyrekordbox em todo arquivo que ele lê,
    #    e ainda ler os que ele não lê.
    checked = rescued = 0
    for path in exts:
        raw = open(path, "rb").read()
        hot, mem = parse_pco2(raw)
        try:
            ref = {"hot": [], "mem": []}
            for tag in AnlzFile.parse(raw).tags:
                if type(tag).__name__ == "PCO2AnlzTag":
                    bucket = "hot" if str(tag.content.type) == "hotcue" else "mem"
                    ref[bucket].extend(dict(e) for e in tag.content.entries)
        except Exception:
            rescued += bool(hot or mem)
            continue
        key = lambda es: sorted((int(e["hot_cue"]), int(e["time"])) for e in es)  # noqa: E731
        assert key(hot) == key(ref["hot"]), path
        assert key(mem) == key(ref["mem"]), path
        checked += 1
    assert checked, "nenhum arquivo conferido"
    print(f"parse_pco2: {checked} arquivos idênticos ao pyrekordbox, "
          f"{rescued} que só este parser lê")

    # 2. splice_tag tem que trocar a grade e deixar o resto byte a byte igual.
    src = exts[0][:-4] + ".DAT"
    local = next(p for p in sorted(glob.glob(SHARE + "/PIONEER/USBANLZ/*/*/ANLZ0000.DAT"))
                 if find_tag(open(p, "rb").read(), b"PQTZ"))
    with tempfile.TemporaryDirectory() as tmp:
        dst = os.path.join(tmp, "ANLZ0000.DAT")
        shutil.copy2(local, dst)
        before, want = read_usb_anlz(dst), read_usb_anlz(src)
        assert splice_tag(dst, src, b"PQTZ")
        after = read_usb_anlz(dst)
        assert (after["beats"], after["tempo"]) == (want["beats"], want["tempo"]), after
        assert after["ppth"] == before["ppth"], "splice mexeu no PPTH"
        new, old = open(dst, "rb").read(), open(local, "rb").read()
        assert int.from_bytes(new[8:12], "big") == len(new), "len_file incoerente"
        AnlzFile.parse(new)
        for magic in (b"PPTH", b"PVBR", b"PWAV", b"PWV2", b"PCOB"):
            a, b = find_tag(old, magic), find_tag(new, magic)
            assert (a is None) == (b is None), magic
            if a:
                assert old[a[0]:a[1]] == new[b[0]:b[1]], magic
    print(f"splice_tag: grade {before['beats']}b/{before['tempo']} -> "
          f"{after['beats']}b/{after['tempo']}, resto intacto")
    print("PASS")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only-playlist", help="ID de playlist para limitar o escopo")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if args.apply:
        guard_rekordbox_closed()  # dry-run é só leitura, pode rodar com ele aberto
    if not os.path.isdir(USBANLZ):
        sys.exit(f"pendrive não montado: {USBANLZ}")

    print("indexando ANLZ do pendrive…")
    usb = index_usb()
    errs = [u for u in usb if "err" in u]
    print(f"  {len(usb)} pares ANLZ, {len(errs)} ilegíveis, "
          f"{sum(1 for u in usb if u.get('file'))} com arquivo resolvido")

    db = Rekordbox6Database(db_dir=RB_DIR)
    rows = db.session.execute(text(
        "select ID, FolderPath, FileNameL, BPM, AnalysisDataPath, FileSize, UUID "
        "from djmdContent where rb_local_deleted=0 or rb_local_deleted is null")).all()
    hd = {r[0]: dict(id=r[0], path=r[1], name=r[2], bpm=r[3], anlz=r[4],
                     size=r[5], uuid=r[6]) for r in rows}
    by_size = defaultdict(list)
    for c in hd.values():
        by_size[c["size"]].append(c["id"])

    scope = None
    if args.only_playlist:
        scope = {r[0] for r in db.session.execute(text(
            "select ContentID from djmdSongPlaylist where PlaylistID=:p"),
            {"p": args.only_playlist}).all()}
        print(f"  escopo limitado a {len(scope)} faixas da playlist")

    # casamento por tamanho de arquivo (nomes no pendrive vêm truncados);
    # empate desfeito por md5.
    matched, ambiguous, unmatched = {}, [], []
    for u in usb:
        if not u.get("file"):
            continue
        cands = by_size.get(u["size"], [])
        if len(cands) == 1:
            matched[cands[0]] = u
        elif not cands:
            unmatched.append(u)
        else:
            digest = md5(u["file"])
            hit = [c for c in cands if os.path.isfile(hd[c]["path"])
                   and md5(hd[c]["path"]) == digest]
            if len(hit) == 1:
                matched[hit[0]] = u
            else:
                ambiguous.append(u)
    print(f"  casadas: {len(matched)}  ambíguas: {len(ambiguous)}  "
          f"sem par no HD: {len(unmatched)}")

    existing = defaultdict(lambda: (set(), []))
    for cid, kind, inms in db.session.execute(text(
            "select ContentID, Kind, InMsec from djmdCue "
            "where rb_local_deleted=0 or rb_local_deleted is null")).all():
        kinds, mems = existing[cid]
        if kind == 0:
            mems.append(inms)
        else:
            kinds.add(kind)

    # ---- planejamento ----
    cue_plan, grid_plan, no_ext = [], [], []
    for cid, u in matched.items():
        if scope and cid not in scope:
            continue
        if not u["ext_ok"]:
            no_ext.append(cid)
        if u["hot"] or u["mem"]:
            new = cue_rows_for(u, existing[cid])
            if new:
                cue_plan.append((cid, new))
        local_dat = SHARE + (hd[cid]["anlz"] or "")
        if not hd[cid]["anlz"] or not os.path.exists(local_dat):
            continue
        try:
            local = read_usb_anlz(local_dat)
        except Exception:
            continue
        same_tempo = local["tempo"] == u["tempo"]
        noise = same_tempo and abs(local["beats"] - u["beats"]) <= 2
        if u["tempo"] and (local["beats"], local["tempo"]) != (u["beats"], u["tempo"]) \
                and not noise:
            grid_plan.append((cid, local, u))

    print(f"\ncues a restaurar: {sum(len(p[1]) for p in cue_plan)} "
          f"em {len(cue_plan)} faixas")
    if no_ext:
        print(f"  aviso: {len(no_ext)} faixas casadas sem .EXT no pendrive (sem cues)")
    print(f"grade a restaurar: {len(grid_plan)} faixas")
    for cid, local, u in grid_plan[:15]:
        print(f"   {hd[cid]['bpm'] / 100:>7.2f} -> {u['tempo'] / 100:>7.2f} BPM "
              f"({local['beats']}->{u['beats']} beats)  {hd[cid]['name'][:46]}")
    if len(grid_plan) > 15:
        print(f"   … +{len(grid_plan) - 15}")

    if not args.apply:
        print("\n(dry-run — nada gravado; rode com --apply)")
        return

    # ---- backup ----
    stamp = datetime.now().strftime("%F")
    for src, tag in ((os.path.join(RB_DIR, "master.db"), "master.db"),
                     (os.path.join(RB_DIR, "masterPlaylists6.xml"), "masterPlaylists6.xml")):
        dst = f"{BACKUP_DIR}/_Backup-{tag}-{stamp}-pre-restore-usb"
        shutil.copy2(src, dst)
        print("backup:", dst)
    anlz_backup = f"{BACKUP_DIR}/_Backup-anlz-{stamp}-pre-restore-usb.tar"
    if grid_plan:
        dirs = [os.path.dirname(hd[c]["anlz"]).lstrip("/") for c, _, _ in grid_plan]
        subprocess.run(["tar", "-cf", anlz_backup, "-C", SHARE] + dirs, check=True)
        print("backup:", anlz_backup)

    # ---- grava cues ----
    ts = now_str()          # para SQL cru
    ts_dt = datetime.now(timezone.utc)   # o ORM do pyrekordbox serializa datetime
    inserted = 0
    with db.session.no_autoflush:
        for cid, new in cue_plan:
            for row in new:
                db.session.add(DjmdCue(
                    ID=str(db.generate_unused_id(DjmdCue)), ContentID=cid,
                    ContentUUID=hd[cid]["uuid"], UUID=str(uuid.uuid4()),
                    rb_data_status=0, rb_local_data_status=0, rb_local_deleted=0,
                    rb_local_synced=0, usn=None, rb_local_usn=None,
                    created_at=ts_dt, updated_at=ts_dt, **row))
                inserted += 1
    print(f"\ninseridos {inserted} cues")

    # ---- grava grade ----
    fixed = 0
    for cid, _local, u in grid_plan:
        local_dat = SHARE + hd[cid]["anlz"]
        ok = splice_tag(local_dat, u["dat"], b"PQTZ")
        local_ext, usb_ext = local_dat[:-4] + ".EXT", u["dat"][:-4] + ".EXT"
        if os.path.exists(local_ext) and os.path.exists(usb_ext):
            splice_tag(local_ext, usb_ext, b"PQT2")
        if ok:
            db.session.execute(
                text("update djmdContent set BPM=:b, updated_at=:t where ID=:i"),
                {"b": u["tempo"], "t": ts, "i": cid})
            fixed += 1
    print(f"grade corrigida em {fixed} faixas")

    db.commit()
    print("commit ok")

    # ---- verificação ----
    s = db.session.execute
    print("\n-- verificação --")
    print("faixas com cue:", s(text(
        "select count(distinct ContentID) from djmdCue where rb_local_deleted=0")).scalar())
    print("cues totais:", s(text(
        "select count(*) from djmdCue where rb_local_deleted=0")).scalar())
    print("cues com Kind inválido:", s(text(
        "select count(*) from djmdCue where Kind not in (0,1,2,3,5,6,7,8,9)")).scalar())
    print("cues órfãos:", s(text(
        "select count(*) from djmdCue c where not exists "
        "(select 1 from djmdContent t where t.ID=c.ContentID)")).scalar())
    bad = 0
    for cid, _l, _u in grid_plan:
        p = SHARE + hd[cid]["anlz"]
        try:
            AnlzFile.parse_file(p)
        except Exception:
            bad += 1
    print("ANLZ ilegíveis após splice:", bad)
    print("\nAgora abra o rekordbox e confira a árvore e os cues.")


if __name__ == "__main__":
    main()
