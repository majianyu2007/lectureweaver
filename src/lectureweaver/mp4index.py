"""Read an MP4's sample tables over HTTP Range requests.

The classroom recordings keep ``moov`` at the end of the file (verified:
ftyp, free, mdat ≈ 386 MB, moov ≈ 1.6 MB). With the sample tables we know
the exact byte range of every sample, so we can fetch

* only the video keyframes (independently decodable IDR frames) for screen
  capture analysis, and
* only the AAC audio samples for speech recognition,

instead of transferring the whole file.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Callable

ReadRange = Callable[[int, int], bytes]  # (offset, length) -> bytes


@dataclass
class Track:
    track_id: int = 0
    handler: str = ""  # 'vide' | 'soun'
    timescale: int = 1
    duration: int = 0
    codec: str = ""
    sample_sizes: list[int] = field(default_factory=list)
    sample_offsets: list[int] = field(default_factory=list)
    sample_times: list[int] = field(default_factory=list)  # decode times (track timescale)
    sync_samples: set[int] | None = None  # 0-based; None = all samples are sync
    avcc: bytes = b""  # AVCDecoderConfigurationRecord
    asc: bytes = b""  # AAC AudioSpecificConfig
    sample_rate: int = 0
    channels: int = 0
    ctts: list[int] | None = None  # composition offsets per sample
    width: int = 0
    height: int = 0

    def time_of(self, i: int) -> float:
        t = self.sample_times[i] + (self.ctts[i] if self.ctts else 0)
        return t / self.timescale

    def keyframes(self) -> list[int]:
        n = len(self.sample_sizes)
        if self.sync_samples is None:
            return list(range(n))
        return sorted(i for i in self.sync_samples if i < n)


@dataclass
class Movie:
    file_size: int
    moov_offset: int
    moov_size: int
    tracks: list[Track]

    def track(self, handler: str) -> Track | None:
        return next((t for t in self.tracks if t.handler == handler), None)


def _boxes(buf: bytes, start: int = 0, end: int | None = None):
    end = len(buf) if end is None else end
    off = start
    while off + 8 <= end:
        size, typ = struct.unpack(">I4s", buf[off : off + 8])
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", buf[off + 8 : off + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - off
        if size < hdr:
            return
        yield typ.decode("latin-1"), off + hdr, off + size
        off += size


def locate_moov(read: ReadRange, file_size: int) -> tuple[int, int]:
    off = 0
    while off < file_size:
        head = read(off, 16)
        size, typ = struct.unpack(">I4s", head[:8])
        if size == 1:
            size = struct.unpack(">Q", head[8:16])[0]
        elif size == 0:
            size = file_size - off
        if typ == b"moov":
            return off, size
        if size < 8:
            break
        off += size
    raise ValueError("moov box not found")


def parse_movie(read: ReadRange, file_size: int) -> Movie:
    moov_off, moov_size = locate_moov(read, file_size)
    moov = read(moov_off, moov_size)
    tracks = []
    for typ, s, e in _boxes(moov, 8):
        if typ == "trak":
            tracks.append(_parse_trak(moov, s, e))
    return Movie(file_size, moov_off, moov_size, tracks)


def _find(buf, s, e, path: list[str]):
    for typ, cs, ce in _boxes(buf, s, e):
        if typ == path[0]:
            if len(path) == 1:
                return cs, ce
            r = _find(buf, cs, ce, path[1:])
            if r:
                return r
    return None


def _parse_trak(b: bytes, s: int, e: int) -> Track:
    t = Track()
    tkhd = _find(b, s, e, ["tkhd"])
    if tkhd:
        v = b[tkhd[0]]
        t.track_id = struct.unpack(">I", b[tkhd[0] + (20 if v == 1 else 12) : tkhd[0] + (24 if v == 1 else 16)])[0]
    mdhd = _find(b, s, e, ["mdia", "mdhd"])
    if mdhd:
        p = mdhd[0]
        if b[p] == 1:
            t.timescale, t.duration = struct.unpack(">IQ", b[p + 20 : p + 32])
        else:
            t.timescale, t.duration = struct.unpack(">II", b[p + 12 : p + 20])
    hdlr = _find(b, s, e, ["mdia", "hdlr"])
    if hdlr:
        t.handler = b[hdlr[0] + 8 : hdlr[0] + 12].decode("latin-1")
    stbl = _find(b, s, e, ["mdia", "minf", "stbl"])
    if not stbl:
        return t
    ss, se = stbl
    boxes = {typ: (cs, ce) for typ, cs, ce in _boxes(b, ss, se)}

    # stsd: codec + decoder config
    if "stsd" in boxes:
        cs, ce = boxes["stsd"]
        for typ, es, ee in _boxes(b, cs + 8, ce):
            t.codec = typ
            if typ in ("avc1", "avc3"):
                t.width, t.height = struct.unpack(">HH", b[es + 24 : es + 28])
                for t2, a, z in _boxes(b, es + 78, ee):
                    if t2 == "avcC":
                        t.avcc = b[a:z]
            elif typ == "mp4a":
                t.channels, _bits = struct.unpack(">HH", b[es + 16 : es + 20])
                t.sample_rate = struct.unpack(">I", b[es + 24 : es + 28])[0] >> 16
                for t2, a, z in _boxes(b, es + 28, ee):
                    if t2 == "esds":
                        t.asc = _asc_from_esds(b[a + 4 : z])
            break

    # sample sizes
    if "stsz" in boxes:
        p = boxes["stsz"][0]
        fixed, count = struct.unpack(">II", b[p + 4 : p + 12])
        if fixed:
            t.sample_sizes = [fixed] * count
        else:
            t.sample_sizes = list(struct.unpack(f">{count}I", b[p + 12 : p + 12 + 4 * count]))
    n = len(t.sample_sizes)

    # chunk offsets
    chunk_offsets: list[int] = []
    if "stco" in boxes:
        p = boxes["stco"][0]
        cnt = struct.unpack(">I", b[p + 4 : p + 8])[0]
        chunk_offsets = list(struct.unpack(f">{cnt}I", b[p + 8 : p + 8 + 4 * cnt]))
    elif "co64" in boxes:
        p = boxes["co64"][0]
        cnt = struct.unpack(">I", b[p + 4 : p + 8])[0]
        chunk_offsets = list(struct.unpack(f">{cnt}Q", b[p + 8 : p + 8 + 8 * cnt]))

    # sample -> chunk
    stsc: list[tuple[int, int]] = []
    if "stsc" in boxes:
        p = boxes["stsc"][0]
        cnt = struct.unpack(">I", b[p + 4 : p + 8])[0]
        for i in range(cnt):
            first, per, _desc = struct.unpack(">III", b[p + 8 + 12 * i : p + 20 + 12 * i])
            stsc.append((first, per))
    offsets = []
    si = 0
    for idx, (first, per) in enumerate(stsc):
        last = stsc[idx + 1][0] - 1 if idx + 1 < len(stsc) else len(chunk_offsets)
        for chunk in range(first, last + 1):
            off = chunk_offsets[chunk - 1]
            for _ in range(per):
                if si >= n:
                    break
                offsets.append(off)
                off += t.sample_sizes[si]
                si += 1
    t.sample_offsets = offsets

    # decode times
    times = []
    if "stts" in boxes:
        p = boxes["stts"][0]
        cnt = struct.unpack(">I", b[p + 4 : p + 8])[0]
        cur = 0
        for i in range(cnt):
            c, d = struct.unpack(">II", b[p + 8 + 8 * i : p + 16 + 8 * i])
            for _ in range(c):
                times.append(cur)
                cur += d
    t.sample_times = times[:n]

    if "ctts" in boxes:
        p = boxes["ctts"][0]
        ver = b[p]
        cnt = struct.unpack(">I", b[p + 4 : p + 8])[0]
        ct = []
        for i in range(cnt):
            c, o = struct.unpack(">Ii" if ver == 1 else ">II", b[p + 8 + 8 * i : p + 16 + 8 * i])
            ct.extend([o] * c)
        t.ctts = ct[:n]

    if "stss" in boxes:
        p = boxes["stss"][0]
        cnt = struct.unpack(">I", b[p + 4 : p + 8])[0]
        t.sync_samples = {x - 1 for x in struct.unpack(f">{cnt}I", b[p + 8 : p + 8 + 4 * cnt])}
    return t


def _asc_from_esds(d: bytes) -> bytes:
    """Find DecoderSpecificInfo (tag 0x05) inside an ES_Descriptor."""

    def read_len(i):
        n = 0
        for _ in range(4):
            c = d[i]
            i += 1
            n = (n << 7) | (c & 0x7F)
            if not c & 0x80:
                break
        return n, i

    i = 0
    while i < len(d):
        tag = d[i]
        ln, j = read_len(i + 1)
        if tag == 0x03:
            flags = d[j + 2]
            i = j + 3 + (2 if flags & 0x80 else 0)
            if flags & 0x40:
                i += 1 + d[i]
            if flags & 0x20:
                i += 2
            continue
        if tag == 0x04:
            i = j + 13
            continue
        if tag == 0x05:
            return d[j : j + ln]
        i = j + ln
    return b""


# ------------------------------------------------------------------ helpers
def avcc_to_annexb_headers(avcc: bytes) -> tuple[bytes, int]:
    """SPS/PPS in Annex-B form plus the NAL length size."""
    nal_len = (avcc[4] & 0x03) + 1
    out = b""
    i = 5
    nsps = avcc[i] & 0x1F
    i += 1
    for _ in range(nsps):
        ln = struct.unpack(">H", avcc[i : i + 2])[0]
        out += b"\x00\x00\x00\x01" + avcc[i + 2 : i + 2 + ln]
        i += 2 + ln
    npps = avcc[i]
    i += 1
    for _ in range(npps):
        ln = struct.unpack(">H", avcc[i : i + 2])[0]
        out += b"\x00\x00\x00\x01" + avcc[i + 2 : i + 2 + ln]
        i += 2 + ln
    return out, nal_len


def sample_to_annexb(sample: bytes, nal_len: int) -> bytes:
    out = bytearray()
    i = 0
    while i + nal_len <= len(sample):
        ln = int.from_bytes(sample[i : i + nal_len], "big")
        i += nal_len
        out += b"\x00\x00\x00\x01" + sample[i : i + ln]
        i += ln
    return bytes(out)


def adts_header(asc: bytes, frame_len: int, channels: int) -> bytes:
    profile = (asc[0] >> 3) - 1  # AOT - 1
    sf_index = ((asc[0] & 0x07) << 1) | (asc[1] >> 7)
    ch = ((asc[1] >> 3) & 0x0F) or channels
    full = frame_len + 7
    return bytes(
        [
            0xFF,
            0xF1,
            ((profile & 0x03) << 6) | ((sf_index & 0x0F) << 2) | ((ch >> 2) & 0x01),
            ((ch & 0x03) << 6) | ((full >> 11) & 0x03),
            (full >> 3) & 0xFF,
            ((full & 0x07) << 5) | 0x1F,
            0xFC,
        ]
    )


def coalesce(ranges: list[tuple[int, int]], max_gap: int = 0, max_span: int = 8 << 20) -> list[tuple[int, int]]:
    """Merge sorted (offset, size) ranges whose gap is <= max_gap."""
    out: list[list[int]] = []
    for off, size in sorted(ranges):
        if out and off - (out[-1][0] + out[-1][1]) <= max_gap and (off + size - out[-1][0]) <= max_span:
            out[-1][1] = max(out[-1][1], off + size - out[-1][0])
        else:
            out.append([off, size])
    return [(a, b) for a, b in out]
