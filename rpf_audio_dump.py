#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rpf_audio_dump -- pull every audio stream out of a GTA VI (Xbox) audio.rpf.

Why this exists
---------------
audio.rpf stores its sound banks as `ADAT` containers inside a Rockstar RPF9
archive.  The archive's table of contents is encrypted, but the banks are plain
data, so this tool finds them by carving the archive for the `ADAT` magic and
parses each bank header directly.  Everything (Ogg Opus muxing included) is
implemented with the Python standard library: no ffmpeg, no SixNG, no numpy.

Four kinds of streams live in the banks
---------------------------------------
blocks  Opus packets in 2 KiB blocks, one 16-bit length per packet
        -> written as Ogg Opus (`.opus`), packets are copied, never re-encoded
music   Opus packets in 512 KiB pages, payload at page+0x800, 2 KiB blocks,
        blocks interleaved between N channels
        -> one Ogg Opus per channel group (`<id>_ch1.opus`, ...), no re-encode
other   XMA2 blocks (Xbox codec), 2 KiB per block
        -> RIFF/WAVE with an XMA2 format chunk (`<id>.xma.wav`), no re-encode;
           optionally converted with ffmpeg when `--ffmpeg` is given
none    placeholder stream, no payload -> nothing is written

Typical use
-----------
    python rpf_audio_dump.py --rpf audio.rpf --out D:\\audio
    python rpf_audio_dump.py --rpf audio.rpf --out D:\\audio --jobs 12
    python rpf_audio_dump.py --rpf audio.rpf --out D:\\audio --min-seconds 60
    python rpf_audio_dump.py --rpf audio.rpf --index-only

Output layout
-------------
    <out>/<bank hex>/<stream id>.opus           blocks streams
    <out>/<bank hex>/<stream id>_ch1.opus       music, multichannel banks
    <out>/<bank hex>/<stream id>.xma.wav        other streams (XMA2)
    <out>/_index.json.gz                        scan cache, reuse with --index
    <out>/_summary.json, <out>/_failed.txt
"""

import argparse
import concurrent.futures as futures
import gzip
import json
import mmap
import os
import struct
import subprocess
import sys
import time
import zlib

VERSION = "1.0.0"

# --- ADAT chunk kinds -------------------------------------------------------
CH_FORMAT = 0x76          # stream format block (samples, rate, ...)
CH_FORMAT_ALT = 0xFA      # same, alternative tag
CH_STREAM_FORMAT = 0x48   # multichannel descriptor (music banks)
CH_DATA = 0x55            # payload

BLOCK = 0x800             # 2 KiB block in both blocks and music layouts
PAGE = 0x80000            # 512 KiB page in the music layout
PRE_SKIP = 312            # Opus pre-skip used by the game's encoder

# packets per Ogg page: keeps seeking reasonable while amortising page headers
PAGE_PACKETS = 64
PAGE_BYTES = 32 * 1024

# --- Opus helpers -----------------------------------------------------------
# frame size per TOC config (RFC 6716, table 2)
FRAME = {}
for _i in range(12):
    FRAME[_i] = (480, 960, 1920, 2880)[_i & 3]
for _i in range(12, 16):
    FRAME[_i] = (480, 960)[_i & 1]
for _i in range(16, 32):
    FRAME[_i] = (120, 240, 480, 960)[_i & 3]

# bit-reversed byte table: Ogg's CRC is the MSB-first CRC-32 (poly 0x04C11DB7)
_BITREV = bytes(int("{:08b}".format(i)[::-1], 2) for i in range(256))


def ogg_crc(buf):
    """CRC-32 as Ogg wants it (poly 0x04C11DB7, init 0, no final xor).

    zlib.crc32 implements the reflected variant in C, so the data is bit
    reversed per byte, hashed, and the register is reversed back.  ~15x faster
    than a byte-at-a-time Python table loop.
    """
    r = zlib.crc32(buf.translate(_BITREV), 0xFFFFFFFF) ^ 0xFFFFFFFF
    b = _BITREV
    return (b[r & 0xFF] << 24) | (b[(r >> 8) & 0xFF] << 16) | (b[(r >> 16) & 0xFF] << 8) | b[r >> 24]


def pkt_frames(p):
    """Length of an Opus packet in samples (48 kHz)."""
    toc = p[0]
    n = 1
    c = toc & 3
    if c == 3:
        n = max(1, p[1] & 0x3F) if len(p) > 1 else 1
    elif c != 0:
        n = 2
    return FRAME[toc >> 3] * n


def pkt_ok(p):
    """RFC 6716 framing check, used to drop bytes misread from junk padding."""
    n = len(p)
    if not (1 <= n <= 1275):
        return False
    c = p[0] & 3
    if c == 0:
        return True
    if c == 1:
        return n >= 2 and (n - 1) % 2 == 0
    if c == 2:
        return n >= 3 and p[1] > 0 and 1 + p[1] < n
    if n < 3:
        return False
    v = p[1]
    fc = v & 0x3F
    if fc < 1 or fc > 48:
        return False
    if not (v & 0x40):
        return (n - 2) % fc == 0
    if n < 2 + fc:
        return False
    total = 0
    for i in range(fc):
        ln = p[2 + i]
        if ln == 0:
            return False
        total += ln
    return 2 + fc + total <= n


def ogg_header(serial, channels, title, vendor=b"rpf-audio-dump"):
    """OpusHead + OpusTags pages (BOS + comment header)."""
    head = (b"OpusHead" + bytes([1, channels]) + struct.pack("<HIH", PRE_SKIP, 48000, 0) + b"\x00")
    tags = b"TITLE=" + title.encode("utf-8", "replace")
    tag_page = (b"OpusTags" + struct.pack("<I", len(vendor)) + vendor +
                struct.pack("<I", 1) + struct.pack("<I", len(tags)) + tags)
    return (_page(serial, 0, 0, 0x02, head, [len(head)]) +
            _page(serial, 1, 0, 0x00, tag_page, _lacing(len(tag_page))))


def _lacing(n):
    segs = []
    while n >= 255:
        segs.append(255)
        n -= 255
    segs.append(n)
    return segs


def _page(serial, seq, granule, flags, body, lacing):
    hdr = (b"OggS\x00" + bytes([flags]) + struct.pack("<qII", granule, serial, seq) +
           b"\x00\x00\x00\x00" + bytes([len(lacing)]) + bytes(lacing))
    page = bytearray(hdr)
    page += body
    page[22:26] = struct.pack("<I", ogg_crc(page))
    return bytes(page)


def make_ogg(packets, channels, serial, title, total=0):
    """Mux raw Opus packets into a self-contained Ogg Opus file."""
    out = [ogg_header(serial, channels, title)]
    gran = 0
    seq = 2
    i = 0
    n = len(packets)
    end = PRE_SKIP + total if total else 0
    while i < n:
        body = []
        lac = []
        size = 0
        j = i
        while j < n and j - i < PAGE_PACKETS:
            p = packets[j]
            if body and size + len(p) > PAGE_BYTES:
                break
            body.append(p)
            lac.extend(_lacing(len(p)))
            size += len(p)
            j += 1
        gran += sum(pkt_frames(p) for p in body)
        last = j == n
        if last and end and gran > end:
            gran = end
        out.append(_page(serial, seq, gran, 0x04 if last else 0x00,
                         b"".join(body), lac))
        seq += 1
        i = j
    return b"".join(out)


# --- XMA2 -------------------------------------------------------------------
def xma2_wav(data, channels, rate, samples):
    """Wrap raw XMA2 blocks in RIFF/WAVE (format tag 0x0166) without decoding."""
    ba = 2048
    chmask = 0x4 if channels <= 1 else (0x3 if channels == 2 else (1 << channels) - 1)
    avg = max(1, (len(data) * rate) // max(1, samples))
    ext = struct.pack("<HIIIIIIIBBH", max(1, channels), chmask, samples, ba, 0, samples,
                      0, 0, 0, 3, max(1, len(data) // ba))
    body = (struct.pack("<HHIIHH", 0x0166, max(1, channels), rate, avg, ba, 16) +
            struct.pack("<H", 34) + ext)
    fmt = b"fmt " + struct.pack("<I", len(body)) + body
    dat = b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", 4 + len(fmt) + len(dat)) + b"WAVE" + fmt + dat


# --- ADAT parsing -----------------------------------------------------------
def parse_header(h, off):
    """Parse one bank header; return a bank dict or None when implausible."""
    if len(h) < 16 or h[0:4] != b"ADAT":
        return None
    flags, n, hl = struct.unpack_from("<III", h, 4)
    if flags >> 24 != 0xFF or (flags & 0xFFFF) != 1:
        return None
    if not (1 <= n <= 20000) or not (16 <= hl <= (4 << 20)) or len(h) < hl:
        return None
    p = 16
    if flags & 0x00400000:
        p += 4
    first = None
    if flags & 0x00010000:
        if p + 2 * n > hl:
            return None
        first = struct.unpack_from("<%dH" % n, h, p)
        p += 2 * n
    if flags & 0x00200000:
        p += 4 * (n + 1)
    if p + 4 * n > hl:
        return None
    raw = struct.unpack_from("<%dI" % n, h, p)
    p += 4 * n
    entries = [(v & 0x1FFFFFFF, v >> 29) for v in raw]
    total = sum(c for _, c in entries)
    if p + 8 * total != hl:
        return None
    if first is not None:
        acc = 0
        for i, (_, cnt) in enumerate(entries):
            if first[i] != acc:
                return None
            acc += cnt
    size = hl
    streams = []
    k = 0
    for sid, cnt in entries:
        chunks = []
        for _ in range(cnt):
            (r,) = struct.unpack_from("<Q", h, p + 8 * k)
            k += 1
            kind = r >> 56
            csize = (r >> 28) & 0x0FFFFFFF
            coff = r & 0x0FFFFFFF
            if csize and coff < hl:
                return None
            if coff + csize > size:
                size = coff + csize
            chunks.append((kind, coff, csize))
        streams.append({"id": sid, "chunks": chunks, "data": None, "samples": 0,
                        "rate": 0, "codec": 0, "channels": 0, "layout": "none"})
    return {"off": off, "size": size, "flags": flags, "streams": streams}


def fill_formats(mm, bank):
    """Read the small format chunks and decide each stream's layout."""
    base = bank["off"]
    for s in bank["streams"]:
        fmt = None
        sfmt = None
        data = None
        for kind, coff, csize in s["chunks"]:
            if kind in (CH_FORMAT, CH_FORMAT_ALT) and csize >= 20 and fmt is None:
                fmt = (coff, csize)
            elif kind == CH_STREAM_FORMAT and sfmt is None:
                sfmt = (coff, csize)
            elif kind == CH_DATA and data is None:
                data = (coff, csize)
        if fmt:
            b = mm[base + fmt[0]:base + fmt[0] + min(fmt[1], 64)]
            if len(b) >= 20:
                s["samples"] = struct.unpack_from("<I", b, 0)[0]
                s["rate"] = struct.unpack_from("<H", b, 8)[0]
                s["codec"] = b[0x13]
        if sfmt:
            b = mm[base + sfmt[0]:base + sfmt[0] + min(sfmt[1], 12 + 16 * 64)]
            ch = struct.unpack_from("<I", b, 8)[0] if len(b) >= 12 else 0
            if 0 < ch <= 64 and len(b) >= 12 + 16 * ch:
                s["channels"] = ch
                s["samples"] = struct.unpack_from("<I", b, 16)[0]
                s["rate"] = struct.unpack_from("<H", b, 22)[0]
                s["codec"] = b[24]
        if data:
            s["data"] = list(data)
        if data and sfmt:
            s["layout"] = "music"
        elif data and s["codec"] == 0:
            s["layout"] = "blocks"
        elif data:
            s["layout"] = "other"
        else:
            s["layout"] = "none"
        if s["layout"] == "blocks" and data and data[1] >= 3:
            t = mm[base + data[0]:base + data[0] + 3]
            if len(t) == 3:
                s["channels"] = 2 if (t[2] & 4) else 1
    return bank


def scan(path, progress=None):
    """Carve the archive for ADAT banks."""
    banks = []
    with open(path, "rb") as f:
        total = os.fstat(f.fileno()).st_size
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        pos = 0
        skip_until = 0
        hits = 0
        t0 = time.time()
        while True:
            i = mm.find(b"ADAT", pos)
            if i < 0 or i + 16 > total:
                break
            pos = i + 1
            hits += 1
            if i < skip_until:
                continue
            hl = struct.unpack_from("<I", mm, i + 12)[0]
            if not (16 <= hl <= (4 << 20)) or i + hl > total:
                continue
            bank = parse_header(mm[i:i + hl], i)
            if not bank or i + bank["size"] > total:
                continue
            fill_formats(mm, bank)
            for s in bank["streams"]:
                s["chunks"] = []          # not needed past this point
            skip_until = i + bank["size"]
            banks.append(bank)
            if progress and len(banks) % 500 == 0:
                progress("scan", i, total, len(banks))
        mm.close()
    if progress:
        progress("scan", total, total, len(banks))
    return {"archive": os.path.basename(path), "archive_size": total,
            "banks": banks, "magic_hits": hits, "scanned_in": round(time.time() - t0, 2)}


def stream_seconds(s):
    if s["layout"] == "blocks":
        per = s["channels"] or 1
        return (s["samples"] // per) / 48000.0
    if s["layout"] == "music":
        return s["samples"] / 48000.0
    if s["rate"]:
        return s["samples"] / s["rate"]
    return 0.0


# --- payload readers --------------------------------------------------------
def blocks_packets(data):
    """Opus packets of a `blocks` stream."""
    out = []
    for blk_start in range(0, len(data), BLOCK):
        blk = data[blk_start:blk_start + BLOCK]
        p = 0
        ln = len(blk)
        while p + 2 <= ln:
            n = blk[p] | (blk[p + 1] << 8)
            if n == 0 or p + 2 + n > ln:
                break
            out.append(blk[p + 2:p + 2 + n])
            p += 2 + n
    return out


def music_blocks(data):
    """Per-block packet lists of a `music` stream (payload at page+0x800)."""
    blocks = []
    for ps in range(0, len(data), PAGE):
        pe = min(ps + PAGE, len(data))
        p = ps + BLOCK
        while p < pe:
            end = min(p + BLOCK, pe)
            o = p
            pk = []
            while True:
                if o + 2 > end:
                    break
                n = data[o] | (data[o + 1] << 8)
                if not (1 <= n <= 2000) or o + 2 + n > end:
                    break
                pkt = data[o + 2:o + 2 + n]
                if not pkt_ok(pkt):
                    break
                pk.append(pkt)
                o += 2 + n
            if pk:
                blocks.append(pk)
            p += BLOCK
    return blocks


def split_channels(blocks, nstreams):
    """Blocks are interleaved round-robin between the channel groups."""
    subs = [[] for _ in range(nstreams)]
    for i, blk in enumerate(blocks):
        subs[i % nstreams].extend(blk)
    return subs


# --- exporting --------------------------------------------------------------
def bank_hex(off):
    return "%x" % off


def write_file(path, payload):
    with open(path, "wb") as f:
        f.write(payload)
    return len(payload)


def export_bank(rpf, bank, out_dir, opts, stats):
    """Write every stream of one bank; per-bank counters go into `.done`."""
    off = bank["off"]
    hexoff = bank_hex(off)
    folder = os.path.join(out_dir, hexoff)
    if not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)
    done_path = os.path.join(folder, ".done")
    if opts["resume"] and os.path.isfile(done_path):
        stats["banks_skipped"] += 1
        return
    done = {"files": 0, "bytes": 0}
    with open(rpf, "rb") as f:
        for s in bank["streams"]:
            layout = s["layout"]
            if layout == "none" or s["data"] is None:
                stats["none"] += 1
                continue
            if layout == "blocks" and opts["no_blocks"]:
                continue
            if layout == "music" and opts["no_music"]:
                continue
            if layout == "other" and opts["no_xma"]:
                continue
            if stream_seconds(s) < opts["min_seconds"]:
                stats["filtered"] += 1
                continue
            doff, dsize = s["data"]
            f.seek(off + doff)
            data = f.read(dsize)
            if not data:
                stats["empty"] += 1
                continue
            sid = s["id"]
            stem = "%08x" % sid
            serial = (off ^ sid) & 0xFFFFFFFF
            try:
                if layout == "blocks":
                    pk = blocks_packets(data)
                    if not pk:
                        stats["empty"] += 1
                        continue
                    blob = make_ogg(pk, s["channels"] or 1, serial, stem, s["samples"] or 0)
                    n = write_file(os.path.join(folder, stem + ".opus"), blob)
                    stats["blocks"] += 1
                    done["files"] += 1
                    done["bytes"] += n
                elif layout == "music":
                    blocks = music_blocks(data)
                    if not blocks:
                        stats["empty"] += 1
                        continue
                    ch = s["channels"] or 1
                    stereo = bool(blocks[0][0][0] & 4) if blocks[0] else False
                    per = 2 if stereo else 1
                    nstreams = max(1, ch // per)
                    subs = split_channels(blocks, nstreams)
                    for k, packets in enumerate(subs):
                        if not packets:
                            continue
                        name = stem + (".opus" if nstreams == 1 else "_ch%d.opus" % (k + 1))
                        blob = make_ogg(packets, per, serial + k, name[:-5], (s["samples"] or 0) if nstreams == 1 else 0)
                        n = write_file(os.path.join(folder, name), blob)
                        stats["music"] += 1
                        done["files"] += 1
                        done["bytes"] += n
                else:  # other -> XMA2
                    wav = xma2_wav(data, s["channels"] or 1, s["rate"] or 32000, s["samples"])
                    n = write_file(os.path.join(folder, stem + ".xma.wav"), wav)
                    stats["other"] += 1
                    done["files"] += 1
                    done["bytes"] += n
                    if opts["ffmpeg"]:
                        try:
                            n2 = convert_xma(opts, folder, stem)
                            if n2:
                                stats["other_mp3"] += 1
                                done["files"] += 1
                                done["bytes"] += n2
                        except Exception as exc:            # noqa: BLE001
                            stats["errors"].append("%s/%s ffmpeg: %s" % (hexoff, stem, exc))
            except Exception as exc:                        # noqa: BLE001
                stats["errors"].append("%s/%s %s: %s" % (hexoff, stem, layout, exc))
    stats["files"] += done["files"]
    stats["bytes"] += done["bytes"]
    stamp = {"bank": hexoff, "files": done["files"], "bytes": done["bytes"],
             "seconds": round(time.time(), 1)}
    try:
        with open(done_path, "w", encoding="utf-8") as fh:
            json.dump(stamp, fh)
    except OSError:
        pass


def convert_xma(opts, folder, stem):
    """Optional: decode the XMA2 WAV to mp3/flac with an external ffmpeg."""
    ext = opts["xma_format"]
    dst = os.path.join(folder, stem + "." + ext)
    if os.path.isfile(dst) and os.path.getsize(dst) > 0:
        return 0
    src = os.path.join(folder, stem + ".xma.wav")
    args = [opts["ffmpeg"], "-y", "-loglevel", "error", "-i", src,
            "-map_metadata", "-1"]
    if ext == "mp3":
        args += ["-c:a", "libmp3lame", "-b:a", opts["bitrate"]]
    elif ext == "flac":
        args += ["-c:a", "flac"]
    else:
        args += ["-c:a", "pcm_s16le"]
    args.append(dst)
    subprocess.run(args, check=True, capture_output=True,
                   creationflags=0x08000000 if os.name == "nt" else 0)
    if not opts["keep_xma"]:
        try:
            os.remove(src)
        except OSError:
            pass
    return os.path.getsize(dst)


def export_chunk(job):
    """Worker entry point: export a list of banks, return counters."""
    rpf, banks, out_dir, opts = job
    stats = {"files": 0, "bytes": 0, "blocks": 0, "music": 0, "other": 0,
             "other_mp3": 0, "none": 0, "empty": 0, "filtered": 0,
             "banks_skipped": 0, "errors": []}
    for bank in banks:
        export_bank(rpf, bank, out_dir, opts, stats)
    stats["errors"] = stats["errors"][:50]
    return stats


# --- CLI --------------------------------------------------------------------
def human(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


def free_space(path):
    try:
        st = os.statvfs(path) if hasattr(os, "statvfs") else None
        if st:
            return st.f_bavail * st.f_frsize
    except OSError:
        pass
    try:
        import ctypes
        free = ctypes.c_ulonglong(0)
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(path), None, None, ctypes.byref(free))
        return free.value
    except Exception:                                       # noqa: BLE001
        return None


def load_index(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def save_index(path, ix):
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(ix, f, separators=(",", ":"))
    os.replace(tmp, path)


def build_parser():
    ap = argparse.ArgumentParser(
        prog="rpf_audio_dump",
        description="Export every audio stream of a GTA VI (Xbox) audio.rpf. "
                    "Pure Python: no ffmpeg, no SixNG, nothing else.")
    ap.add_argument("--rpf", help="path to audio.rpf")
    ap.add_argument("--out", "-o", help="output folder (one sub folder per bank)")
    ap.add_argument("--jobs", "-j", type=int, default=min(16, (os.cpu_count() or 4) * 2),
                    help="parallel bank workers (default: %(default)s)")
    ap.add_argument("--min-seconds", type=float, default=0.0,
                    help="skip streams shorter than this (like SixNG's Min length)")
    ap.add_argument("--banks", help="comma separated bank offsets, e.g. 129b000,6927a8000")
    ap.add_argument("--limit", type=int, default=0, help="only the first N banks")
    ap.add_argument("--index", help="reuse this index file instead of scanning")
    ap.add_argument("--rescan", action="store_true", help="ignore a cached index")
    ap.add_argument("--index-only", action="store_true", help="scan and report, do not export")
    ap.add_argument("--no-blocks", action="store_true", help="skip blocks (Opus) streams")
    ap.add_argument("--no-music", action="store_true", help="skip music streams")
    ap.add_argument("--no-xma", action="store_true", help="skip other (XMA2) streams")
    ap.add_argument("--ffmpeg", help="optional path to ffmpeg: also convert XMA2 to mp3")
    ap.add_argument("--xma-format", default="mp3", choices=["mp3", "flac", "wav"],
                    help="conversion target when --ffmpeg is used (default: mp3)")
    ap.add_argument("--bitrate", default="192k", help="mp3 bitrate for --ffmpeg (default: 192k)")
    ap.add_argument("--no-keep-xma", action="store_true",
                    help="with --ffmpeg, delete the .xma.wav after conversion")
    ap.add_argument("--force", action="store_true", help="ignore .done markers and rewrite")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    ap.add_argument("--verify", type=int, default=0, help="verify N random outputs after export")
    ap.add_argument("--quiet", "-q", action="store_true")
    ap.add_argument("--version", action="version", version="rpf_audio_dump " + VERSION)
    return ap


def find_rpf(explicit):
    if explicit:
        if not os.path.isfile(explicit):
            raise SystemExit("no such file: %s" % explicit)
        return explicit
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "audio.rpf"),
                 os.path.join(os.getcwd(), "audio.rpf")):
        if os.path.isfile(cand):
            return cand
    raise SystemExit("audio.rpf not found, pass --rpf PATH")


def main(argv=None):
    args = build_parser().parse_args(argv)
    quiet = args.quiet

    def log(msg):
        if not quiet:
            print(msg, flush=True)

    rpf = find_rpf(args.rpf)
    size = os.path.getsize(rpf)
    log("archive : %s (%.1f GiB)" % (rpf, size / 2**30))

    index_path = args.index or (os.path.join(args.out, "_index.json.gz") if args.out else None)
    ix = None
    if index_path and os.path.isfile(index_path) and not args.rescan:
        log("index   : %s (cached)" % index_path)
        try:
            ix = load_index(index_path)
        except Exception as exc:                            # noqa: BLE001
            log("index   : unreadable (%s), rescanning" % exc)
            ix = None
    if ix is None:
        t0 = time.time()

        def progress(stage, done, total, banks):
            pct = 100.0 * done / total if total else 100.0
            sys.stdout.write("\rscan    : %5.1f%%  %s / %s  %d banks" %
                             (pct, human(done), human(total), banks))
            sys.stdout.flush()

        ix = scan(rpf, None if quiet else progress)
        if not quiet:
            print()
        ix["scanned_in"] = round(time.time() - t0, 2)
        if index_path:
            try:
                save_index(index_path, ix)
            except OSError as exc:
                log("index   : cannot write %s (%s)" % (index_path, exc))

    banks = ix["banks"]
    if args.banks:
        want = {int(x, 16) for x in args.banks.replace(",", " ").split()}
        banks = [b for b in banks if b["off"] in want]
    if args.limit:
        banks = banks[:args.limit]

    counts = {"blocks": 0, "music": 0, "other": 0, "none": 0}
    seconds = 0.0
    for b in banks:
        for s in b["streams"]:
            counts[s["layout"]] = counts.get(s["layout"], 0) + 1
            if s["layout"] in ("blocks", "music", "other"):
                seconds += stream_seconds(s)
    log("banks   : %d  (scanned %s, %d magic hits)" %
        (len(banks), ix.get("scanned_in", "?"), ix.get("magic_hits", 0)))
    log("streams : %d blocks, %d music, %d other, %d none" %
        (counts["blocks"], counts["music"], counts["other"], counts["none"]))
    log("audio   : %.1f hours" % (seconds / 3600.0))

    if args.index_only or not args.out:
        return 0

    os.makedirs(args.out, exist_ok=True)
    free = free_space(args.out)
    estimate = counts["blocks"] * 11_000 + counts["music"] * 1_900_000 + counts["other"] * 55_000
    if free is not None:
        log("space   : %s free, rough need %s" % (human(free), human(estimate)))
        if free < estimate * 0.25:
            raise SystemExit("not enough free space (need roughly %s)" % human(estimate))

    if args.dry_run:
        log("dry run : nothing written")
        return 0

    opts = {"resume": not args.force, "min_seconds": args.min_seconds,
            "no_blocks": args.no_blocks, "no_music": args.no_music,
            "no_xma": args.no_xma, "ffmpeg": args.ffmpeg,
            "xma_format": args.xma_format, "bitrate": args.bitrate,
            "keep_xma": not args.no_keep_xma}

    # chunk the banks so the IPC overhead stays negligible
    chunk = max(8, min(64, len(banks) // max(1, args.jobs * 4) or 8))
    jobs = [banks[i:i + chunk] for i in range(0, len(banks), chunk)]
    log("export  : %d banks in %d chunks, %d workers" % (len(banks), len(jobs), args.jobs))

    totals = {"files": 0, "bytes": 0, "blocks": 0, "music": 0, "other": 0,
              "other_mp3": 0, "none": 0, "empty": 0, "filtered": 0,
              "banks_skipped": 0, "errors": []}
    t0 = time.time()
    done = 0
    if args.jobs <= 1:
        for job in jobs:
            st = export_chunk((rpf, job, args.out, opts))
            merge(totals, st)
            done += len(job)
            report(done, len(banks), totals, t0)
    else:
        with futures.ProcessPoolExecutor(max_workers=args.jobs) as ex:
            pending = {ex.submit(export_chunk, (rpf, job, args.out, opts)): len(job) for job in jobs}
            for fut in futures.as_completed(pending):
                try:
                    st = fut.result()
                except Exception as exc:                    # noqa: BLE001
                    totals["errors"].append("worker: %s" % exc)
                    st = {}
                merge(totals, st)
                done += pending[fut]
                report(done, len(banks), totals, t0)

    elapsed = time.time() - t0
    summary = {"version": VERSION, "archive": rpf, "archive_size": size,
               "out": os.path.abspath(args.out), "banks": len(banks),
               "streams": counts, "files": totals["files"], "bytes": totals["bytes"],
               "by_layout": {k: totals[k] for k in ("blocks", "music", "other", "other_mp3", "none", "empty", "filtered")},
               "banks_skipped": totals["banks_skipped"],
               "seconds": round(elapsed, 1), "errors": len(totals["errors"]),
               "audio_hours": round(seconds / 3600.0, 2)}
    with open(os.path.join(args.out, "_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1)
    if totals["errors"]:
        with open(os.path.join(args.out, "_failed.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(totals["errors"]) + "\n")

    log("")
    log("done    : %d files, %s, %.1fs (%.0f files/s)" %
        (totals["files"], human(totals["bytes"]), elapsed,
         totals["files"] / elapsed if elapsed else 0))
    log("layouts : blocks %d, music %d, other %d (+%d converted), empty %d, filtered %d" %
        (totals["blocks"], totals["music"], totals["other"], totals["other_mp3"],
         totals["empty"], totals["filtered"]))
    if totals["banks_skipped"]:
        log("resume  : skipped %d finished banks" % totals["banks_skipped"])
    if totals["errors"]:
        log("errors  : %d (see _failed.txt)" % len(totals["errors"]))

    if args.verify:
        ok, bad = verify_outputs(args.out, args.verify, log)
        log("verify  : %d ok, %d bad" % (ok, bad))
    return 0


def merge(totals, st):
    for k, v in st.items():
        if k == "errors":
            totals["errors"].extend(v)
        else:
            totals[k] = totals.get(k, 0) + v


_last = [0.0]


def report(done, total, totals, t0):
    pct = 100.0 * done / total if total else 100.0
    now = time.time()
    if now - _last[0] < 1.0 and done < total:
        return
    _last[0] = now
    elapsed = max(1e-6, now - t0)
    rate = totals["files"] / elapsed
    eta = (total - done) / max(1e-6, done / elapsed)
    sys.stdout.write("\rexport  : %5.1f%%  %d/%d banks  %d files  %s  %s/s  eta %02d:%02d" %
                     (pct, done, total, totals["files"], human(totals["bytes"]),
                      human(int(rate)), int(eta // 60), int(eta % 60)))
    sys.stdout.flush()
    if done >= total:
        sys.stdout.write("\n")


def verify_outputs(out_dir, n, log):
    """Re-parse a few Ogg files: page structure, CRC and granule monotonicity."""
    import random
    files = []
    for root, _dirs, names in os.walk(out_dir):
        for name in names:
            if name.endswith(".opus"):
                files.append(os.path.join(root, name))
                if len(files) > 4000:
                    break
    random.shuffle(files)
    ok = bad = 0
    for path in files[:n]:
        try:
            with open(path, "rb") as f:
                blob = f.read()
            pos = 0
            gran = -1
            pages = 0
            while pos < len(blob):
                if blob[pos:pos + 4] != b"OggS":
                    raise ValueError("bad capture pattern at %d" % pos)
                nseg = blob[pos + 26]
                lac = blob[pos + 27:pos + 27 + nseg]
                body = sum(lac)
                end = pos + 27 + nseg + body
                page = bytearray(blob[pos:end])
                want = struct.unpack_from("<I", page, 22)[0]
                page[22:26] = b"\x00\x00\x00\x00"
                if ogg_crc(page) != want:
                    raise ValueError("crc mismatch")
                g = struct.unpack_from("<q", page, 6)[0]
                if g < gran:
                    raise ValueError("granule went backwards")
                gran = g
                pos = end
                pages += 1
            if pages < 3:
                raise ValueError("too few pages")
            ok += 1
        except Exception as exc:                            # noqa: BLE001
            bad += 1
            log("verify  : %s -> %s" % (os.path.basename(path), exc))
    return ok, bad


if __name__ == "__main__":
    sys.exit(main())
