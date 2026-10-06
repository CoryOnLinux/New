#!/usr/bin/env python3
"""flacmeta: health checks, safe tag fixes and ReplayGain 2.0 for a FLAC library.

  flacmeta check PATH...                     read-only report
  flacmeta fix PATH... [--album-year] [--apply]
  flacmeta replaygain PATH... [--force] [--apply]
  flacmeta info FILE...
  flacmeta spectrogram FILE [-o PNG]
  flacmeta undo LOG [--apply]

Nothing is written without --apply. Every write goes: copy (a reflink on btrfs) -> edit the
copy -> check the audio frames are byte-identical -> undo record -> atomic rename.
"""
import argparse
import base64
import concurrent.futures as cf
import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import struct
import subprocess
import sys
import threading
from collections import Counter, defaultdict

try:
    from mutagen import MutagenError
    from mutagen.flac import FLAC
except ImportError:
    sys.exit('flacmeta: needs mutagen: sudo pacman -S --needed python-mutagen')
try:
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view
except ImportError:
    np = None

# Spectral analysis: one excerpt per file. Thresholds come from calibration on genuine 24-bit
# recordings (cliff 4-21 dB; downsampled to 48 kHz: 27-30 dB at 23.1 kHz), the same recordings
# upsampled with soxr (56-108 dB) or ffmpeg's default resampler (23-73 dB, its images blur the edge)
# and MP3 sources (10-57 dB, edge 16-20 kHz). MP3 V0 and AAC 256k are not detectable.
EXCERPT_S = 60
BAND_HZ = 100
UPSAMPLE_CLIFF_DB = 30.0      # hi-res files
UPSAMPLE_CLIFF_DB_48K = 40.0  # 48 kHz files: genuine ones can show ~30 dB, a little above 22 kHz
LOSSY_CLIFF_DB = 25.0
LOSSY_MAX_EDGE_HZ = 20600  # edges are band centres: a 20.5 kHz brick wall reads 20550

COVER_MIN_PX = 500
COVER_MAX_PX = 3000
COVER_MAX_BYTES = 2 << 20
COVER_FILES = ('cover.jpg', 'cover.png', 'folder.jpg', 'folder.png', 'front.jpg', 'front.png')

REQUIRED_TAGS = ('TITLE', 'ARTIST', 'ALBUM', 'ALBUMARTIST', 'TRACKNUMBER', 'DATE')
OPTIONAL_TAGS = ('TRACKTOTAL', 'DISCNUMBER', 'DISCTOTAL', 'GENRE')
SINGLE_TAGS = {'TITLE', 'ALBUM', 'DATE', 'TRACKNUMBER', 'TRACKTOTAL', 'DISCNUMBER', 'DISCTOTAL', 'ISRC',
               'BARCODE', 'UPC', 'LABEL', 'COPYRIGHT', 'MEDIATYPE', 'ITUNESADVISORY', 'LYRICS',
               'REPLAYGAIN_TRACK_GAIN', 'REPLAYGAIN_TRACK_PEAK', 'REPLAYGAIN_ALBUM_GAIN',
               'REPLAYGAIN_ALBUM_PEAK', 'REPLAYGAIN_REFERENCE_LOUDNESS'}
TEXT_TAGS = {'LYRICS', 'UNSYNCEDLYRICS', 'COMMENT', 'DESCRIPTION'}  # multi-line: never trimmed
ALBUM_TAGS = ('ALBUM', 'ALBUMARTIST', 'DATE', 'GENRE', 'LABEL', 'BARCODE', 'COPYRIGHT', 'DISCTOTAL', 'MEDIATYPE')
SYNONYMS = {'TOTALTRACKS': 'TRACKTOTAL', 'TOTALDISCS': 'DISCTOTAL'}
RG_TAGS = ('REPLAYGAIN_TRACK_GAIN', 'REPLAYGAIN_TRACK_PEAK', 'REPLAYGAIN_ALBUM_GAIN', 'REPLAYGAIN_ALBUM_PEAK')
RSGAIN_ARGS = ['custom', '--album', '--tagmode=i', '--loudness=-18', '--clip-mode=p', '--quiet']

DATE_RE = re.compile(r'^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$')
SLASH_RE = re.compile(r'^\s*(\d+)\s*/\s*(\d+)\s*$')
ISRC_RE = re.compile(r'^[A-Z]{2}[A-Z0-9]{3}\d{7}$')
GAIN_RE = re.compile(r'^[+-]?\d+(\.\d+)? dB$')
PEAK_RE = re.compile(r'^\d+(\.\d+)?$')
DISC_DIR_RE = re.compile(r'^(disc|disk|cd)\s*\d+\b', re.I)
FILE_NUM_RE = re.compile(r'^(\d{1,3})\s*[-.]\s')
YEAR_DIR_RE = re.compile(r'\((\d{4})\)\s*$')
TMP_PREFIX = '.flacmeta-'
MAX_NUMBER = 999  # track/disc numbers above this are reported as malformed, not used for gap checks

LEVELS = ('ERROR', 'WARN', 'INFO')
BLOCK_NAMES = {0: 'STREAMINFO', 1: 'PADDING', 2: 'APPLICATION', 3: 'SEEKTABLE', 4: 'VORBIS_COMMENT',
               5: 'CUESHEET', 6: 'PICTURE'}
STATE_DIR = os.path.join(os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state'), 'flacmeta')

HOLDING_LOCK = False  # this process holds the write lock
STOP = threading.Event()  # set on Ctrl+C/SIGTERM: workers finish their step, clean up, and don't replace


class BadFile(Exception):
    pass


@dataclasses.dataclass
class Finding:
    level: str
    code: str
    message: str
    path: str | None = None  # None: about the whole album
    fixable: bool = False


@dataclasses.dataclass
class Layout:
    size: int
    flac_start: int   # offset of "fLaC" (> 0 when an ID3v2 tag is glued in front)
    audio_start: int  # first audio frame
    audio_end: int    # end of audio frames (< size when ID3v1/APEv2 tags are glued at the end)
    blocks: list      # (type, data offset, length)

    def block(self, path, btype):
        for t, off, n in self.blocks:
            if t == btype:
                with open(path, 'rb') as f:
                    f.seek(off)
                    return f.read(n)
        return None


@dataclasses.dataclass
class Track:
    path: str
    layout: Layout | None = None
    tags: list = dataclasses.field(default_factory=list)  # [(key, value)] in file order, keys as stored
    rate: int = 0
    bits: int = 0
    channels: int = 0
    samples: int = 0
    md5: int = 0
    pictures: list = dataclasses.field(default_factory=list)
    seektable: bool = False
    findings: list = dataclasses.field(default_factory=list)
    eff_bits: int | None = None
    spectrum: dict | None = None
    seen_stat: tuple | None = None  # stat_key() when it was read: a later write refuses if the file changed since
    comment_problems: list = dataclasses.field(default_factory=list)
    unwritable: str = ''  # why fix/replaygain won't replace this file ('' if they will)

    def get(self, key):
        return [v for k, v in self.tags if k.upper() == key]

    def first(self, key):
        vals = self.get(key)
        return vals[0].strip() if vals else ''

    def add(self, level, code, message, fixable=False):
        self.findings.append(Finding(level, code, message, self.path, fixable))

    @property
    def duration(self):
        return self.samples / self.rate if self.rate else 0.0


# ---------------------------------------------------------------------------------------------
# File layout, images, small helpers


def run(cmd, **kw):
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, **kw)


def tail(text, n=200):
    text = text.decode(errors='replace') if isinstance(text, bytes) else text
    lines = [l for l in text.strip().splitlines() if l.strip()]
    line = lines[-1] if lines else 'no error output'
    return line if len(line) <= n else '...' + line[-n:]  # the reason is at the end, after any long path


def stat_key(st):
    """Changes whenever the file's content or metadata is touched (ctime can't be set back)."""
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def fsync_path(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_layout(path):
    """Parse where the FLAC stream, its metadata blocks and its audio frames are."""
    with open(path, 'rb') as f:
        size = os.fstat(f.fileno()).st_size
        head = f.read(10)
        start = 0
        if head[:3] == b'ID3' and len(head) == 10:
            n = 0
            for b in head[6:10]:
                n = (n << 7) | (b & 0x7F)
            start = 10 + n + (10 if head[5] & 0x10 else 0)
        f.seek(start)
        if f.read(4) != b'fLaC':
            raise BadFile('not a FLAC file (no fLaC marker)')
        pos, blocks = start + 4, []
        while True:
            hdr = f.read(4)
            if len(hdr) < 4:
                raise BadFile('metadata is truncated')
            btype, length = hdr[0] & 0x7F, int.from_bytes(hdr[1:4], 'big')
            blocks.append((btype, pos + 4, length))
            pos += 4 + length
            if pos > size:
                raise BadFile(f'{BLOCK_NAMES.get(btype, btype)} block runs past the end of the file')
            f.seek(pos)
            if hdr[0] & 0x80:
                break
        end = size
        while True:  # ID3v1 and APEv2 tags glued to the end, in either order
            if end - pos >= 128:
                f.seek(end - 128)
                if f.read(3) == b'TAG':
                    end -= 128
                    continue
            if end - pos >= 32:
                f.seek(end - 32)
                foot = f.read(32)
                if foot[:8] == b'APETAGEX':
                    total = int.from_bytes(foot[12:16], 'little')
                    total += 32 if int.from_bytes(foot[20:24], 'little') & 0x80000000 else 0
                    if 32 <= total <= end - pos:
                        end -= total
                        continue
            break
        return Layout(size, start, pos, end, blocks)


def audio_digest(path, layout):
    h = hashlib.blake2b(digest_size=20)
    with open(path, 'rb') as f:
        f.seek(layout.audio_start)
        left = layout.audio_end - layout.audio_start
        while left > 0:
            chunk = f.read(min(left, 4 << 20))
            if not chunk:
                raise BadFile('file shrank while reading')
            h.update(chunk)
            left -= len(chunk)
    return h.hexdigest()


def image_info(data):
    """(mime, width, height, depth, colors) from the image header, or None if unreadable.

    depth/colors follow libFLAC (metaflac --import-picture-from): palette images are depth 24."""
    if data[:8] == b'\x89PNG\r\n\x1a\n' and len(data) >= 26 and data[12:16] == b'IHDR':
        w, h, bitdepth, ctype = struct.unpack('>IIBB', data[16:26])
        colors, pos = 0, 8
        while ctype == 3 and pos + 8 <= len(data):
            ln, typ = struct.unpack('>I4s', data[pos:pos + 8])
            if typ == b'PLTE':
                colors = ln // 3
                break
            pos += 12 + ln
        depth = 24 if ctype == 3 else bitdepth * {0: 1, 2: 3, 4: 2, 6: 4}.get(ctype, 1)
        return ('image/png', w, h, depth, colors) if depth and w and h else None
    if data[:3] == b'\xff\xd8\xff':
        pos = 2
        while pos + 4 <= len(data):
            if data[pos] != 0xFF:
                return None
            marker = data[pos + 1]
            if marker == 0xFF:
                pos += 1
                continue
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                pos += 2
                continue
            ln = struct.unpack('>H', data[pos + 2:pos + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                if pos + 10 > len(data):
                    return None
                prec, h, w, comps = struct.unpack('>BHHB', data[pos + 4:pos + 10])
                return ('image/jpeg', w, h, prec * comps, 0) if prec and comps and w and h else None
            pos += 2 + ln
        return None
    if data[:6] in (b'GIF87a', b'GIF89a') and len(data) >= 11:
        w, h, packed = struct.unpack('<HHB', data[6:11])
        return 'image/gif', w, h, 24, 1 << ((packed & 7) + 1)
    return None


def header_problem(p, info):
    """What is wrong with a PICTURE header ('' if nothing): Qobuz leaves the size 0x0."""
    if (p.width, p.height) != info[1:3]:
        return f'header says {p.width}x{p.height}, image is {info[1]}x{info[2]}'
    return 'header leaves the colour depth unset' if not p.depth and info[3] else ''


def header_wrong(p, info):
    """The PICTURE header's size is wrong or unset (Qobuz leaves 0x0). Other depth/colors
    conventions are accepted as they are."""
    return bool(header_problem(p, info))


def comment_problems(path, layout):
    """Vorbis comments mutagen cannot write back unchanged: not UTF-8, no '=', or an invalid key."""
    raw = layout.block(path, 4)
    if raw is None:
        return []
    problems, pos = [], 0

    def take(n):
        nonlocal pos
        chunk = raw[pos:pos + n]
        if len(chunk) < n:
            raise BadFile('VORBIS_COMMENT block is truncated')
        pos += n
        return chunk
    try:
        vendor = take(int.from_bytes(take(4), 'little'))
        try:
            vendor.decode('utf-8')
        except UnicodeDecodeError:
            problems.append('the vendor string is not UTF-8')
        for _ in range(int.from_bytes(take(4), 'little')):
            c = take(int.from_bytes(take(4), 'little'))
            key, eq, _ = c.partition(b'=')
            try:
                text = c.decode('utf-8')
            except UnicodeDecodeError:
                problems.append(f'{key.decode("ascii", "replace")[:30]} is not UTF-8')
                continue
            if not eq or not key or any(b < 0x20 or b > 0x7D for b in key):
                problems.append(f'{text[:30]!r} is not KEY=value')
    except BadFile as e:
        problems.append(str(e))
    return problems

def picture_header(p):
    return {'type': p.type, 'mime': p.mime, 'desc': p.desc, 'width': p.width, 'height': p.height,
            'depth': p.depth, 'colors': p.colors, 'sha1': hashlib.sha1(p.data).hexdigest()}


def int_tag(value):
    value = value.strip()
    return int(value) if value.isascii() and value.isdigit() and int(value) > 0 else None


def fmt_rate(rate):
    return f'{rate / 1000:g} kHz'


def fmt_size(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024


def album_dir(path):
    d = os.path.dirname(os.path.abspath(path))
    return os.path.dirname(d) if DISC_DIR_RE.match(os.path.basename(d)) else d


def album_files_on_disk(adir):
    """Every FLAC file of an album: the folder itself plus its Disc NN subfolders (None if unreadable)."""
    found = set()
    try:
        for d in [adir] + [os.path.join(adir, s) for s in sorted(os.listdir(adir))
                           if DISC_DIR_RE.match(s) and os.path.isdir(os.path.join(adir, s))]:
            for name in os.listdir(d):
                if name.lower().endswith('.flac') and not name.startswith(TMP_PREFIX) and os.path.isfile(os.path.join(d, name)):
                    found.add(os.path.join(d, name))
    except OSError:
        return None
    return found


def expand(paths):
    """(FLAC files under the given paths, sorted; folders that could not be read; leftover temp files)."""
    out, seen, errors, leftovers = [], set(), [], []
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isdir(p):
            files = []
            for d, dirs, names in os.walk(p, onerror=errors.append):
                dirs.sort()
                files += [os.path.join(d, n) for n in names if n.lower().endswith('.flac') and not n.startswith(TMP_PREFIX)]
                leftovers += [os.path.join(d, n) for n in names if n.startswith(TMP_PREFIX)]
        elif os.path.isfile(p):
            files = [p]
        else:
            sys.exit(f'flacmeta: no such file or directory: {p}')
        for f in sorted(files):
            if f not in seen:
                seen.add(f)
                out.append(f)
    return out, errors, sorted(leftovers)


def display_root(paths):
    root = os.path.commonpath([os.path.abspath(p) for p in paths])
    return os.path.dirname(root) if os.path.isfile(root) else root


def show(path, root):
    r = os.path.relpath(path, root)
    return os.path.basename(path) if r == '.' else r


# ---------------------------------------------------------------------------------------------
# Loading and per-file checks


def load_track(path):
    t = Track(path)
    try:
        st = os.stat(path)
        layout = read_layout(path)
        f = FLAC(path)
        t.comment_problems = comment_problems(path, layout)
    except (BadFile, MutagenError, OSError, ValueError, struct.error) as e:
        t.add('ERROR', 'unreadable', f'cannot read FLAC metadata: {e}')
        return t
    t.layout, t.seen_stat = layout, stat_key(st)  # set only when both parsers accepted the file
    try:
        writable_stat(path)
    except BadFile as e:
        t.unwritable = str(e)
    if t.comment_problems:
        t.unwritable = f"has Vorbis comments that can't be edited safely ({t.comment_problems[0]})"
    t.tags = list(f.tags) if f.tags is not None else []
    t.rate, t.bits, t.channels = f.info.sample_rate, f.info.bits_per_sample, f.info.channels
    t.samples, t.md5 = f.info.total_samples, f.info.md5_signature
    t.pictures = f.pictures
    t.seektable = f.seektable is not None
    return t


def check_stream(t):
    lay = t.layout
    if lay.flac_start:
        t.add('WARN', 'id3-glued', f'ID3v2 tag ({fmt_size(lay.flac_start)}) glued in front of the FLAC stream', True)
    if lay.audio_end < lay.size:
        t.add('WARN', 'id3-glued', f'ID3v1/APE tag ({fmt_size(lay.size - lay.audio_end)}) glued to the end of the file', True)
    if not t.md5:
        t.add('WARN', 'md5-missing', 'STREAMINFO has no MD5 signature, so decoding cannot be verified '
                                     '(re-encode with flac to add one)')
    if not t.seektable:
        t.add('INFO', 'no-seektable', 'no SEEKTABLE (slow seeking in some players)', can_add_seektable(t))
    if not t.samples:
        t.add('WARN', 'no-length', 'STREAMINFO does not give the number of samples')


def synonym_plan(tags, syn):
    """How fix resolves a synonym such as TOTALTRACKS: ('rename', value), ('drop', why),
    ('conflict', why), or None when the file doesn't have it. check and fix both use this."""
    canon = SYNONYMS[syn]
    if not any(k.upper() == syn for k, _ in tags):
        return None
    syn_vals = sorted({v.strip() for k, v in tags if k.upper() == syn and v.strip()})
    canon_vals = sorted({v.strip() for k, v in tags if k.upper() == canon and v.strip()})
    if not syn_vals:
        return 'drop', f'{syn} is empty'
    if not canon_vals:
        if len(syn_vals) == 1:
            return 'rename', syn_vals[0]
        return 'conflict', f'{syn} has several values: {", ".join(syn_vals)}'
    if syn_vals == canon_vals:
        return 'drop', f'{syn} repeats {canon}'
    return 'conflict', f'{syn} {", ".join(syn_vals)} disagrees with {canon} {", ".join(canon_vals)}'


def check_tags(t):
    if t.comment_problems:
        t.add('WARN', 'tag-unreadable', f'{len(t.comment_problems)} Vorbis comment(s) can\'t be edited safely '
              f'({t.comment_problems[0]}); fix and replaygain leave this file alone')
    for key in ('TRACKNUMBER', 'TRACKTOTAL', 'DISCNUMBER', 'DISCTOTAL'):
        n = int_tag(t.first(key).split('/')[0])
        if n and n > MAX_NUMBER:
            t.add('WARN', 'tag-malformed', f'{key} {n} is implausibly large')
    seen = Counter((k.upper(), v) for k, v in t.tags)
    for (key, value), n in sorted(seen.items()):
        if n > 1:
            t.add('WARN', 'tag-duplicate', f'{key} has the same value {n} times', True)
    keys = Counter(k.upper() for k, _ in t.tags)
    for k, v in t.tags:
        key = k.upper()
        if not v.strip():
            t.add('WARN', 'tag-empty', f'{key} is empty', True)
        elif v != v.strip() and key not in TEXT_TAGS:
            t.add('INFO', 'tag-whitespace', f'{key} has leading/trailing whitespace: {v!r}', True)
    for key in REQUIRED_TAGS:
        if not t.first(key):
            t.add('WARN', 'tag-missing', f'{key} is missing')
    for key in OPTIONAL_TAGS:
        number = {'TRACKTOTAL': 'TRACKNUMBER', 'DISCTOTAL': 'DISCNUMBER'}.get(key)
        if t.first(key) or (number and SLASH_RE.match(t.first(number))):
            continue  # a "3/12" number is reported (and fixed) as malformed instead
        renamed = any((synonym_plan(t.tags, s) or ('',))[0] == 'rename' for s, c in SYNONYMS.items() if c == key)
        t.add('INFO', 'tag-missing-optional', f'{key} is missing', renamed)
    for key in sorted(keys):
        if key in SINGLE_TAGS and len({v.strip() for k, v in t.tags if k.upper() == key and v.strip()}) > 1:
            t.add('WARN', 'tag-multiple', f'{key} has several different values: ' +
                  ', '.join(repr(v) for v in t.get(key))[:150])
        if key in SYNONYMS:
            action, detail = synonym_plan(t.tags, key)
            if action == 'conflict':
                t.add('WARN', 'tag-conflict', detail)
            else:
                t.add('INFO', 'tag-synonym', f'{key} should be {SYNONYMS[key]}', True)
    date = t.first('DATE')
    if date and not valid_date(date):
        t.add('WARN', 'tag-malformed', f'DATE {date!r} is not YYYY, YYYY-MM or YYYY-MM-DD')
    for num, total in (('TRACKNUMBER', 'TRACKTOTAL'), ('DISCNUMBER', 'DISCTOTAL')):
        v = t.first(num)
        if SLASH_RE.match(v):
            t.add('INFO', 'tag-malformed', f'{num} {v!r} holds the total too (should be split into {total})', True)
        elif v and int_tag(v) is None:
            t.add('WARN', 'tag-malformed', f'{num} {v!r} is not a positive number')
        tv = t.first(total)
        if tv and int_tag(tv) is None:
            t.add('WARN', 'tag-malformed', f'{total} {tv!r} is not a positive number')
        elif tv and int_tag(v) and int_tag(v) > int_tag(tv):
            t.add('WARN', 'tag-malformed', f'{num} {v} is larger than {total} {tv}')
    isrc = t.first('ISRC')
    if isrc and not ISRC_RE.match(isrc.replace('-', '').upper()):
        t.add('WARN', 'tag-malformed', f'ISRC {isrc!r} is not a valid ISRC')
    barcode = t.first('BARCODE')
    if barcode and not (barcode.isdigit() and len(barcode) in (8, 12, 13, 14)):
        t.add('INFO', 'tag-malformed', f'BARCODE {barcode!r} is not an 8/12/13/14-digit UPC/EAN')
    check_replaygain(t)
    check_filename(t)


def valid_date(v):
    m = DATE_RE.match(v)
    if not m:
        return False
    y, mo, d = int(m[1]), int(m[2] or 1), int(m[3] or 1)
    try:
        dt.date(y, mo, d)
    except ValueError:
        return False
    return 1000 <= y <= 2100


def check_replaygain(t):
    have = {k: t.first(k) for k in RG_TAGS if t.first(k)}
    for k, v in have.items():
        if not (GAIN_RE if k.endswith('GAIN') else PEAK_RE).match(v):
            t.add('WARN', 'replaygain-malformed', f'{k} {v!r} is malformed')
    if not have:
        t.add('INFO', 'replaygain-missing', 'no ReplayGain tags')
    elif len(have) < len(RG_TAGS):
        missing = ', '.join(k.removeprefix('REPLAYGAIN_') for k in RG_TAGS if k not in have)
        t.add('WARN', 'replaygain-partial', f'ReplayGain incomplete: missing {missing}')


def check_filename(t):
    """'05 - ' is track 5; '105 - ' is disc 1 track 5 on multi-disc albums but track 105 on big single-disc ones."""
    m = FILE_NUM_RE.match(os.path.basename(t.path))
    track = int_tag(t.first('TRACKNUMBER').split('/')[0])
    if not m or not track:
        return
    n, disc = int(m[1]), disc_of(t)
    if n == track or (len(m[1]) == 3 and (n // 100, n % 100) == (disc, track)):
        return
    if len(m[1]) == 3 and n >= 100:
        t.add('WARN', 'filename-mismatch', f'file name says disc {n // 100} track {n % 100} (or track {n}), '
                                           f'tags say disc {disc} track {track}')
    else:
        t.add('WARN', 'filename-mismatch', f'file name says track {n}, TRACKNUMBER says {track}')


def check_pictures(t):
    if not t.pictures:
        t.add('WARN', 'cover-missing', 'no embedded cover')
        return
    fronts = [p for p in t.pictures if p.type == 3]
    if not fronts:
        t.add('INFO', 'cover-missing', 'embedded picture(s) not marked as front cover')
    elif len(fronts) > 1:
        t.add('INFO', 'cover-duplicate', f'{len(fronts)} embedded front covers')
    for i, p in enumerate(t.pictures, 1):
        if p.mime == '-->':
            continue  # picture given by URL
        info = image_info(p.data)
        if info is None:
            t.add('ERROR', 'cover-bad', f'picture #{i} is not a readable JPEG, PNG or GIF ({fmt_size(len(p.data))})')
            continue
        mime, w, h, depth, colors = info
        if p.mime.lower() != mime:
            t.add('WARN', 'cover-mime', f'picture #{i} is {mime} but its MIME type says {p.mime!r}', True)
        if header_wrong(p, info):
            t.add('INFO', 'cover-header', f'picture #{i} {header_problem(p, info)}', True)
        if p.type == 3 and min(w, h) < COVER_MIN_PX:
            t.add('WARN', 'cover-small', f'front cover is only {w}x{h} (< {COVER_MIN_PX} px)')
        if p.type == 3 and (max(w, h) > COVER_MAX_PX or len(p.data) > COVER_MAX_BYTES):
            t.add('INFO', 'cover-big', f'front cover is {w}x{h}, {fmt_size(len(p.data))} (some players struggle '
                                       f'above {COVER_MAX_PX} px or {fmt_size(COVER_MAX_BYTES)})')


# ---------------------------------------------------------------------------------------------
# Audio checks: full decode with MD5 check, effective bit depth, spectrum


def flac_error(stderr):
    """'lost sync x3; ERROR while decoding data' from flac -t's stderr."""
    text = stderr.decode(errors='replace')
    codes = Counter(m.lower().replace('_', ' ') for m in re.findall(r'ERROR_STATUS_(\w+)', text))
    parts = [c + (f' x{n}' if n > 1 else '') for c, n in codes.items()]
    parts += [l.split(': ', 1)[-1].strip() for l in text.splitlines() if 'ERROR' in l and 'error code' not in l]
    return '; '.join(parts) or tail(text)


def check_decode(t):
    if shutil.which('flac'):
        lay = t.layout
        if lay.flac_start or lay.audio_end < lay.size:
            # flac -t loses sync on tags glued to the end: test only the FLAC stream itself
            with open(t.path, 'rb') as f:
                f.seek(lay.flac_start)
                data = f.read(lay.audio_end - lay.flac_start)
            r = subprocess.run(['flac', '-t', '-s', '-'], input=data, capture_output=True)
        else:
            r = run(['flac', '-t', '-s', t.path])
        if r.returncode:
            code = 'md5-mismatch' if b'MD5' in r.stderr else 'decode-error'
            t.add('ERROR', code, f'flac -t failed: {flac_error(r.stderr)}')
    else:
        r = run(['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error', '-i', 'file:' + t.path, '-map', '0:a:0', '-f', 'null', '-'])
        if r.returncode or r.stderr.strip():
            t.add('ERROR', 'decode-error', f'ffmpeg decode failed: {tail(r.stderr)}')


def decode_excerpt(t):
    """60 s of PCM as signed 32-bit little-endian, from ~30% into the track."""
    start = max(0.0, t.duration * 0.3 - EXCERPT_S / 2) if t.duration > EXCERPT_S * 1.5 else 0.0
    r = run(['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error', '-ss', f'{start:.2f}', '-t', str(EXCERPT_S),
             '-i', 'file:' + t.path, '-map', '0:a:0', '-c:a', 'pcm_s32le', '-f', 's32le', 'pipe:1'])
    if r.returncode or not r.stdout:
        raise BadFile(f'ffmpeg could not decode an excerpt: {tail(r.stderr)}')
    return r.stdout


def effective_bits(raw):
    """Bits in use: 32 minus the low bits that are zero in every sample (None for digital silence)."""
    for k in range(4):  # byte k of each little-endian sample, least significant first
        lane = raw[k::4]
        if lane.count(0) != len(lane):
            low = 0
            for v in set(lane):
                low |= v
            return 32 - (8 * k + (low & -low).bit_length() - 1)
    return None


def band_levels(raw, rate, channels):
    """Average level per 100 Hz band in dB (0 dB = full-scale sine), Welch method, Kaiser(20) window."""
    x = np.frombuffer(raw, '<i4')
    x = x[: len(x) - len(x) % channels].reshape(-1, channels)
    n = 1 << (13 + max(0, round(np.log2(rate / 48000))))  # ~5-6 Hz bins at every rate
    if len(x) < 4 * n:
        return None
    w = np.kaiser(n, 20.0)
    acc, count = np.zeros(n // 2 + 1), 0
    for c in range(channels):
        frames = sliding_window_view(x[:, c], n)[:: n // 2]
        for i in range(0, len(frames), 64):
            s = np.fft.rfft(frames[i:i + 64] / 2.0**31 * w, axis=1)
            acc += (s.real**2 + s.imag**2).sum(axis=0)
            count += len(s)
    power = acc / count * 4 / w.sum()**2
    idx = (np.fft.rfftfreq(n, 1 / rate) // BAND_HZ).astype(int)
    nb = int(rate / 2 // BAND_HZ)
    keep = idx < nb
    band = np.bincount(idx[keep], weights=power[keep], minlength=nb) / np.maximum(np.bincount(idx[keep], minlength=nb), 1)
    return 10 * np.log10(band + 1e-30)


def find_cliff(levels, rate):
    """The sharpest drop above 12 kHz after which nothing comes back: (edge Hz, drop dB)."""
    top = int((rate / 2 - 500) // BAND_HZ)
    best = (0, -1e9, 0.0, 0.0)
    for i in range(12000 // BAND_HZ, top - 3):
        below = float(levels[i - 8:i - 2].mean())
        above = float(np.percentile(levels[i + 2:top], 90))
        if below - above > best[1]:
            best = (i, below - above, below, above)
    i, drop, below, above = best
    if i == 0 or drop <= 0:
        return None
    mid, j = (below + above) / 2, top - 1
    while j > i - 10 and levels[j] <= mid:
        j -= 1
    return j * BAND_HZ + BAND_HZ // 2, drop  # middle of the last band with content


def upsampled_from(rate, edge):
    """The source rate a cliff at `edge` Hz points to, or None. Resamplers cut a little below the
    old Nyquist (soxr: 21.2-21.4 kHz from 44.1, 23.1-23.5 from 48, which is also where genuine 48 kHz
    material ends), and some leave images that reach a few kHz above it, so the windows are wider
    than the Nyquist frequencies themselves."""
    from_44 = 20800 <= edge <= 22200  # an edge lower than this is a lossy source, checked separately
    if rate == 48000:
        return '44.1 kHz' if from_44 else None
    if rate > 48000 and edge <= 28000:
        return '44.1 kHz' if from_44 else '48 kHz' if 22900 <= edge <= 23700 else '44.1 or 48 kHz'
    if rate >= 176400 and edge <= 50000:
        return '88.2 kHz' if edge <= 45000 else '96 kHz' if edge <= 48500 else '88.2 or 96 kHz'
    return None


def check_audio(t, spectral=True):
    try:
        raw = decode_excerpt(t)
    except BadFile as e:
        t.add('WARN', 'analysis-failed', str(e))
        return
    t.eff_bits = effective_bits(raw)
    if t.eff_bits is not None and t.eff_bits < t.bits:
        if t.bits > 16 >= t.eff_bits:
            t.add('WARN', 'padded-bit-depth', f'{t.bits}-bit file holds {t.eff_bits}-bit audio (the low bits are always zero)')
        else:
            t.add('INFO', 'unused-bits', f'{t.bits}-bit file uses only {t.eff_bits} bits')
    if not (spectral and np is not None) or t.eff_bits is None:  # None: digital silence, nothing to measure
        return
    levels = band_levels(raw, t.rate, t.channels)
    cliff = find_cliff(levels, t.rate) if levels is not None else None
    if cliff is None:
        return
    edge, drop = cliff
    t.spectrum = {'edge_hz': edge, 'cliff_db': round(drop, 1)}
    threshold = UPSAMPLE_CLIFF_DB_48K if t.rate == 48000 else UPSAMPLE_CLIFF_DB
    src = upsampled_from(t.rate, edge) if drop >= threshold else None
    if src:
        t.add('WARN', 'upsampled', f'probably upsampled from {src}: nothing above {edge / 1000:.1f} kHz '
                                   f'({drop:.0f} dB cliff)')
    if edge <= LOSSY_MAX_EDGE_HZ and drop >= LOSSY_CLIFF_DB:
        t.add('WARN', 'lossy-source', f'possible lossy source: sharp cutoff at {edge / 1000:.1f} kHz ({drop:.0f} dB cliff); '
                                      'check with flacmeta spectrogram')


# ---------------------------------------------------------------------------------------------
# Albums


@dataclasses.dataclass
class Album:
    dir: str
    tracks: list
    complete: bool
    findings: list = dataclasses.field(default_factory=list)

    def add(self, level, code, message, fixable=False):
        self.findings.append(Finding(level, code, message, None, fixable))

    def consensus(self, key, tracks=None):
        """The values every track that has `key` agrees on (a tuple), or None."""
        vals = {album_value(t, key) for t in (tracks or self.tracks) if t.first(key)}
        return vals.pop() if len(vals) == 1 else None

    def consensus_int(self, key, tracks=None):
        vals = self.consensus(key, tracks)
        return int_tag(vals[0]) or 0 if vals and len(vals) == 1 else 0


def album_value(t, key):
    """A track's values for `key`, stripped and without repeats (those are reported per track)."""
    return tuple(dict.fromkeys(v.strip() for v in t.get(key) if v.strip()))


def disc_of(t):
    d = int_tag(t.first('DISCNUMBER').split('/')[0])
    if d:
        return d
    m = re.search(r'\d+', os.path.basename(os.path.dirname(t.path))) if DISC_DIR_RE.match(os.path.basename(os.path.dirname(t.path))) else None
    return int(m[0]) if m else 1


def group_albums(tracks):
    by_dir = defaultdict(list)
    for t in tracks:
        by_dir[album_dir(t.path)].append(t)
    albums = []
    for d in sorted(by_dir):
        on_disk = album_files_on_disk(d)
        albums.append(Album(d, by_dir[d], on_disk is not None and on_disk <= {t.path for t in by_dir[d]}))
        if on_disk is None:
            albums[-1].add('ERROR', 'unreadable-folder', 'the album folder (or a disc folder in it) cannot be read')
    return albums


def check_album(a):
    tracks = [t for t in a.tracks if t.layout]
    if not tracks:
        return
    for key in ALBUM_TAGS:
        vals = Counter('; '.join(album_value(t, key)) for t in tracks)
        if len(vals) > 1:
            fill = '' in vals and len(vals) == 2 and not any(t.unwritable for t in tracks if not album_value(t, key))
            parts = ', '.join(f'{v!r} x{n}' if v else f'missing x{n}' for v, n in vals.most_common(4))
            a.add('WARN', 'album-inconsistent', f'{key} differs between tracks: {parts}', fill)
    for key in ('REPLAYGAIN_ALBUM_GAIN', 'REPLAYGAIN_ALBUM_PEAK'):
        vals = {t.first(key) for t in tracks if t.first(key)}
        if len(vals) > 1:
            a.add('WARN', 'replaygain-album-mismatch', f'{key} differs between tracks; rerun: flacmeta replaygain --force')
    formats = Counter(f'{t.bits}/{fmt_rate(t.rate)}' for t in tracks)
    if len(formats) > 1:
        a.add('INFO', 'album-mixed-format', 'tracks have different formats: ' + ', '.join(f'{f} x{n}' for f, n in formats.items()))
    m = YEAR_DIR_RE.search(os.path.basename(a.dir))
    date = a.consensus('DATE')
    if m and date and date[0][:4] != m[1]:
        a.add('INFO', 'folder-year-mismatch', f'folder says {m[1]}, DATE says {date[0]}')
    if not any(os.path.isfile(os.path.join(a.dir, n)) for n in COVER_FILES):
        a.add('INFO', 'cover-file-missing', 'no cover.jpg (or folder.jpg/front.jpg) in the album folder')
    if not a.complete:
        a.add('INFO', 'album-partial-scan', 'only part of this album was scanned: track numbering not checked')
        return
    discs, numbers = defaultdict(list), {}
    for t in tracks:
        d = disc_of(t)
        if d <= MAX_NUMBER:
            discs[d].append(t)
    for d, dts in discs.items():
        numbers[d] = Counter(int_tag(t.first('TRACKNUMBER').split('/')[0]) for t in dts)
        numbers[d].pop(None, None)
        for n in [n for n in numbers[d] if n > MAX_NUMBER]:  # reported per file as malformed
            del numbers[d][n]
    present = sum(len(n) for n in numbers.values())
    # Qobuz writes the whole album's count into TRACKTOTAL on every disc; other taggers write the
    # disc's count. On a multi-disc album (by folders or by DISCTOTAL), a total larger than every
    # disc's highest track number is taken as album-wide.
    sane = lambda n: n if n <= MAX_NUMBER else 0  # absurd totals are reported per file
    album_total = sane(a.consensus_int('TRACKTOTAL'))
    disc_total = sane(a.consensus_int('DISCTOTAL'))
    multi_disc = len(discs) > 1 or disc_total > 1 or set(discs) - {1}
    whole_album = multi_disc and album_total and all(album_total > max(n, default=0) for n in numbers.values())
    for d, nums in sorted(numbers.items()):
        for n, c in sorted(nums.items()):
            if c > 1:
                a.add('WARN', 'track-duplicate', f'disc {d} track {n} appears {c} times')
        total = 0 if whole_album else sane(a.consensus_int('TRACKTOTAL', discs[d]))
        expected = max([total] + list(nums))
        missing = sorted(set(range(1, expected + 1)) - set(nums))
        if missing:
            label = f'disc {d}: ' if len(discs) > 1 or d != 1 else ''
            a.add('WARN', 'album-incomplete', f'{label}missing track(s) {", ".join(map(str, missing[:20]))}'
                  f'{"..." if len(missing) > 20 else ""} ({len(nums)} of {expected} present)')
    if whole_album and present < album_total:
        a.add('WARN', 'album-incomplete', f'{present} of {album_total} tracks present (TRACKTOTAL)')
    missing = sorted(set(range(1, max([disc_total] + list(discs)) + 1)) - set(discs))
    if missing and discs:
        a.add('WARN', 'album-incomplete', f'missing disc(s) {", ".join(map(str, missing))} of {max(disc_total, max(discs))}')


# ---------------------------------------------------------------------------------------------
# check


def analyse(path, opts):
    t = load_track(path)
    if not t.layout:
        return t
    try:
        check_stream(t)
        check_tags(t)
        check_pictures(t)
        if not opts.quick:
            check_decode(t)
            check_audio(t, spectral=not opts.no_spectrum)
    except Exception as e:  # one odd file must not end a library-wide scan
        t.add('ERROR', 'check-failed', f'flacmeta could not finish checking this file: {type(e).__name__}: {e}')
    if t.unwritable:
        for f in t.findings:
            f.fixable = False
        if not t.comment_problems:  # those already have their own tag-unreadable finding
            t.add('WARN', 'not-rewritable', f'fix and replaygain leave this file alone: it {t.unwritable}')
    return t


def scan(paths, opts, label='Scanning'):
    files, opts.walk_errors, opts.leftovers = expand(paths)
    for e in opts.walk_errors:
        print(f'flacmeta: cannot read {e.filename}: {e.strerror}', file=sys.stderr)
    if opts.leftovers and other_writer_running():
        for f in opts.leftovers:
            print(f'flacmeta: working file of a flacmeta run in progress (leave it): {f}', file=sys.stderr)
        opts.leftovers = []
    for f in opts.leftovers:
        print(f'flacmeta: leftover temp file from an interrupted run (safe to delete): {f}', file=sys.stderr)
    if not files:
        sys.exit('flacmeta: no .flac files found')
    tracks, tty = [], sys.stderr.isatty()
    ex = cf.ThreadPoolExecutor(opts.jobs)
    try:
        futs = [ex.submit(analyse, p, opts) for p in files]
        for i, fut in enumerate(cf.as_completed(futs), 1):
            tracks.append(fut.result())
            if tty and (i % 10 == 0 or i == len(files)):
                print(f'\r{label} {i}/{len(files)} files', end='', file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    ex.shutdown()
    if tty:
        print('\r' + ' ' * 40 + '\r', end='', file=sys.stderr)
    tracks.sort(key=lambda t: t.path)
    albums = group_albums(tracks)
    for a in albums:
        try:
            check_album(a)
        except Exception as e:  # one odd album must not end a library-wide scan
            a.add('ERROR', 'check-failed', f'flacmeta could not finish checking this album: {type(e).__name__}: {e}')
    return albums


def collapse(findings, ntracks):
    """Print one line for a finding that every track of the album has."""
    groups = defaultdict(list)
    for f in findings:
        groups[(f.level, f.code, f.message, f.fixable)].append(f)
    out = []
    for (level, code, message, fixable), fs in groups.items():
        if ntracks > 2 and fs[0].path and len({f.path for f in fs}) == ntracks:
            out.append(Finding(level, code, message, '*', fixable))
        else:
            out.extend(fs)
    return out


def cmd_check(opts):
    if not opts.quick:
        if np is None and not opts.no_spectrum:
            print('note: python-numpy is not installed, so lossy/upsampling checks are skipped '
                  '(sudo pacman -S --needed python-numpy)', file=sys.stderr)
        if not shutil.which('flac'):
            print('note: flac is not installed: decoding is checked with ffmpeg, without the MD5 check', file=sys.stderr)
    albums = scan(opts.paths, opts)
    root = display_root(opts.paths)
    show_levels = LEVELS if opts.verbose else LEVELS[:2]
    totals, fixable, hidden_fixable, ntracks = Counter(), 0, 0, 0
    for path in opts.leftovers:
        totals[('WARN', 'leftover-temp')] += 1
    covered = [a.dir for a in albums if any(f.code == 'unreadable-folder' for f in a.findings)]
    for e in opts.walk_errors:  # an album's unreadable disc folder is already counted with the album
        if not any(e.filename == d or e.filename.startswith(d + os.sep) for d in covered):
            totals[('ERROR', 'unreadable-folder')] += 1
    for a in albums:
        ntracks += len(a.tracks)
        allf = a.findings + [f for t in a.tracks for f in t.findings]
        for f in allf:
            totals[(f.level, f.code)] += 1
            if f.fixable and f.level in show_levels:
                fixable += 1
            elif f.fixable:
                hidden_fixable += 1
        if opts.json:
            for t in a.tracks:
                print(json.dumps({'type': 'track', 'path': t.path, 'album': a.dir, 'rate': t.rate, 'bits': t.bits,
                                  'effective_bits': t.eff_bits, **(t.spectrum or {}),
                                  'findings': [dataclasses.asdict(f) for f in t.findings]}))
            for f in a.findings:
                print(json.dumps({'type': 'album', 'album': a.dir, **dataclasses.asdict(f)}))
            continue
        shown = [f for f in collapse(allf, len(a.tracks)) if f.level in show_levels]
        if not shown:
            continue
        fmts = sorted({f'{t.bits}/{fmt_rate(t.rate)}' for t in a.tracks if t.rate})
        print(f'{show(a.dir, root)}  [{", ".join([f"{len(a.tracks)} tracks"] + fmts)}]')
        order = {lv: i for i, lv in enumerate(LEVELS)}
        for f in sorted(shown, key=lambda f: (order[f.level], f.path or '', f.code)):
            where = 'album' if f.path is None else 'all tracks' if f.path == '*' else os.path.relpath(f.path, a.dir)
            print(f'  {f.level:<5} {where}: {f.message}{"  [fixable]" if f.fixable else ""}')
        print()
    if opts.json:
        return 1 if any(lv == 'ERROR' for lv, _ in totals) else 0
    print(f'Checked {ntracks} files in {len(albums)} albums.')
    if totals:
        for (level, code), n in sorted(totals.items(), key=lambda kv: (LEVELS.index(kv[0][0]), kv[0][1])):
            print(f'  {level:<5} {code:<26} {n}')
        if fixable:
            print(f'{fixable} of the findings listed can be fixed: flacmeta fix PATH   (preview), then add --apply')
        if not opts.verbose and any(lv == 'INFO' for lv, _ in totals):
            more = f' ({hidden_fixable} of them fixable)' if hidden_fixable else ''
            print(f'INFO findings are counted but not listed{more}: add -v to list them.')
    else:
        print('No problems found.')
    return 1 if any(lv == 'ERROR' for lv, _ in totals) else 0


# ---------------------------------------------------------------------------------------------
# Safe rewrite: copy -> edit copy -> verify audio -> undo record -> replace


class UndoLog:
    def __init__(self, action):
        self.action, self.path, self.fh = action, None, None
        self.lock = threading.Lock()

    def write(self, record):
        with self.lock:
            if self.fh is None:
                os.makedirs(STATE_DIR, exist_ok=True)
                stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
                self.path = os.path.join(STATE_DIR, f'{self.action}-{stamp}-{os.getpid()}.jsonl')
                self.fh = open(self.path, 'a', encoding='utf-8')
            self.fh.write(json.dumps({'v': 1, 'time': dt.datetime.now().isoformat(timespec='seconds'),
                                      'action': self.action, **record}) + '\n')
            self.fh.flush()
            os.fsync(self.fh.fileno())

    def close(self):
        if self.fh:
            self.fh.close()


def write_lock():
    os.makedirs(STATE_DIR, exist_ok=True)
    global HOLDING_LOCK
    fh = open(os.path.join(STATE_DIR, 'lock'), 'w')
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit('flacmeta: another flacmeta run is writing files; wait for it to finish')
    HOLDING_LOCK = True
    return fh


def other_writer_running():
    """True while another flacmeta process holds the write lock."""
    if HOLDING_LOCK:
        return False
    try:
        with open(os.path.join(STATE_DIR, 'lock')) as fh:
            fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        pass
    return False


def tmp_name(path):
    """A hidden temp name next to path, short enough for any original name (NAME_MAX is 255 bytes)."""
    return os.path.join(os.path.dirname(path), f'{TMP_PREFIX}{secrets.token_hex(4)}.flac')


def writable_stat(path):
    """os.stat(path), refusing files that replacing would break: symlinks and hard-linked files."""
    if os.path.islink(path):
        raise BadFile('is a symbolic link: run flacmeta on the file it points to')
    st = os.stat(path)
    if st.st_nlink > 1:
        raise BadFile(f'has {st.st_nlink} hard links, which replacing it would split')
    if os.geteuid() != 0 and st.st_uid != os.geteuid():
        raise BadFile('belongs to another user, so replacing it would change its owner (run as that user or root)')
    return st


def make_copy(path, layout, lo=0, hi=None, prefix=b'', suffix=b''):
    """Copy path[lo:hi] (with optional bytes around it) to a hidden temp file next to it. The copy
    stays owner-writable while flacmeta edits it; copy_attrs() gives it the original's mode at the end."""
    tmp = tmp_name(path)
    hi = layout.size if hi is None else hi
    try:
        if lo == 0 and hi == layout.size and not prefix and not suffix:
            r = run(['cp', '--reflink=auto', '--', path, tmp])
            if r.returncode:
                raise BadFile(f'cp failed: {tail(r.stderr)}')
        else:
            with open(path, 'rb') as src, open(tmp, 'xb') as dst:
                dst.write(prefix)
                src.seek(lo)
                left = hi - lo
                while left > 0:
                    chunk = src.read(min(left, 4 << 20))
                    if not chunk:
                        raise BadFile('file shrank while copying')
                    dst.write(chunk)
                    left -= len(chunk)
                dst.write(suffix)
        os.chmod(tmp, 0o600)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return tmp


def copy_attrs(src, dst):
    """Owner (where allowed), mode and extended attributes (ACLs included) of src, onto dst."""
    st = os.stat(src)
    try:
        os.chown(dst, st.st_uid, st.st_gid)
    except PermissionError:  # not root: the owner is already ours, try to keep the group
        with contextlib.suppress(PermissionError):
            os.chown(dst, -1, st.st_gid)
    new = os.stat(dst)
    if (new.st_uid, new.st_gid) != (st.st_uid, st.st_gid):
        raise BadFile("can't keep its owner and group (run as root, or as its owner and a member of its group)")
    os.chmod(dst, stat.S_IMODE(st.st_mode))
    try:
        names = os.listxattr(src)
    except OSError:
        names = []
    for name in names:
        with contextlib.suppress(OSError):
            os.setxattr(dst, name, os.getxattr(src, name))


def verify_copy(path, layout, digest, tmp):
    """The copy must hold the same STREAMINFO and byte-identical audio frames."""
    new = read_layout(tmp)
    if new.block(tmp, 0) != layout.block(path, 0):
        raise BadFile('STREAMINFO changed in the copy')
    if audio_digest(tmp, new) != digest:
        raise BadFile('audio frames differ in the copy')
    return new


def replace(path, tmp, stat0, check_stop=True):
    fsync_path(tmp)
    if stat_key(os.stat(path)) != stat0:
        raise BadFile('file changed while flacmeta was working on it; run again')
    if check_stop and STOP.is_set():
        raise KeyboardInterrupt
    os.replace(tmp, path)
    fsync_path(os.path.dirname(path))


def file_state(path, layout=None):
    layout = layout or read_layout(path)
    f = FLAC(path)
    with open(path, 'rb') as fh:
        prefix = fh.read(layout.flac_start)
        fh.seek(layout.audio_end)
        suffix = fh.read()
    return {'tags': [list(kv) for kv in (f.tags or [])], 'pictures': [picture_header(p) for p in f.pictures],
            'seektable': f.seektable is not None,
            'prefix': base64.b64encode(prefix).decode(), 'suffix': base64.b64encode(suffix).decode()}


def set_pictures(f, headers):
    """Give f's PICTURE blocks these headers, in file order; their image data must be what was planned for."""
    pics = f.pictures
    if [hashlib.sha1(p.data).hexdigest() for p in pics] != [h['sha1'] for h in headers]:
        raise BadFile('the embedded pictures are not the ones the plan was made for')
    for p, h in zip(pics, headers):
        p.type, p.mime, p.desc = h['type'], h['mime'], h['desc']
        p.width, p.height, p.depth, p.colors = h['width'], h['height'], h['depth'], h['colors']


def rewrite(path, target, log, expect=None, note=None):
    """Bring the file to `target` (tags, picture headers, seektable, glued bytes) the safe way.
    `expect` is the stat_key() the plan was made from: if the file changed since, nothing is written."""
    stat0 = stat_key(writable_stat(path))
    if expect is not None and stat0 != expect:
        raise BadFile('changed since flacmeta read it (another program?); run again')
    layout = read_layout(path)
    problems = comment_problems(path, layout)
    if problems:
        raise BadFile(f"its Vorbis comments can't be edited safely ({problems[0]})")
    before = file_state(path, layout)
    digest = audio_digest(path, layout)
    if target['prefix'] is None:
        target = dict(target, prefix=before['prefix'], suffix=before['suffix'])
    tmp = None
    try:
        if (target['prefix'], target['suffix']) != (before['prefix'], before['suffix']):
            tmp = make_copy(path, layout, layout.flac_start, layout.audio_end,
                            base64.b64decode(target['prefix']), base64.b64decode(target['suffix']))
        else:
            tmp = make_copy(path, layout)
        f = FLAC(tmp)
        if f.tags is None:
            f.add_tags()
        if [list(kv) for kv in f.tags] != target['tags']:
            del f.tags[:]
            f.tags.extend(tuple(kv) for kv in target['tags'])
        set_pictures(f, target['pictures'])
        f.save(deleteid3=False)
        if target['seektable'] != before['seektable']:
            cmd = ['metaflac', '--add-seekpoint=10s', tmp] if target['seektable'] else \
                ['metaflac', '--remove', '--block-type=SEEKTABLE', tmp]
            r = run(cmd)
            if r.returncode:
                raise BadFile(f'metaflac failed: {tail(r.stderr)}')
        copy_attrs(path, tmp)
        verify_copy(path, layout, digest, tmp)
        after = file_state(tmp)
        for key in ('tags', 'pictures', 'seektable', 'prefix', 'suffix'):
            if after[key] != target[key]:
                raise BadFile(f'the copy did not come out as planned ({key})')
        if STOP.is_set():
            raise KeyboardInterrupt
        log.write({'path': path, 'note': note, 'before': before, 'after': after})
        replace(path, tmp, stat0)
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def run_writes(jobs, items, work):
    """Run work(item) in parallel; a failing item is reported and the others go on. On Ctrl+C,
    SIGTERM or anything unexpected, running items finish or clean up before this raises."""
    done, failed = 0, []
    ex = cf.ThreadPoolExecutor(jobs)
    futs = {ex.submit(work, it): it for it in items}
    try:
        for fut in cf.as_completed(futs):
            label = futs[fut][0]
            try:
                fut.result()
                done += 1
            except KeyboardInterrupt:
                pass
            except Exception as e:
                failed.append(label)
                print(f'FAILED {label}: {e}', file=sys.stderr)
    except BaseException:
        STOP.set()
        ex.shutdown(wait=True, cancel_futures=True)
        raise
    ex.shutdown()
    return done, failed


def apply_writes(action, jobs, items, work):
    """run_writes with an undo log: (done, failed, log). The log's path is printed even on interrupt."""
    log = UndoLog(action)
    try:
        done, failed = run_writes(jobs, items, lambda it: work(it, log))
    except KeyboardInterrupt:
        if log.path:
            print(f'\nflacmeta: files changed before the interrupt are recorded in {log.path}', file=sys.stderr)
        raise
    finally:
        log.close()
    return done, failed, log


# ---------------------------------------------------------------------------------------------
# fix


def plan_fix(t, album, album_year):
    """(target for rewrite() or None, list of changes to show). Notes in parentheses are not changes."""
    if t.unwritable:
        return None, [f'(skipped: it {t.unwritable})']
    changes, tags, seen, handled = [], [], set(), set()
    synonyms = {syn: synonym_plan(t.tags, syn) for syn in SYNONYMS}
    for k, v in t.tags:
        key = k.upper()
        action, detail = synonyms.get(key) or ('', '')
        if action in ('drop', 'rename'):  # 'conflict': left as it is, check reports it
            if key not in handled:
                handled.add(key)
                if action == 'drop':
                    changes.append(f'remove {key} ({detail})')
                else:
                    changes.append(f'{key}={detail!r} -> {SYNONYMS[key]}')
                    tags.append([SYNONYMS[key], detail])
                    seen.add((SYNONYMS[key], detail))
            continue
        nv = v if key in TEXT_TAGS else v.strip()
        if not nv.strip():
            changes.append(f'remove empty {key}')
            continue
        if nv != v:
            changes.append(f'{key}: trim whitespace {v!r} -> {nv!r}')
        if (key, nv) in seen:
            changes.append(f'remove duplicate {key}={nv!r}')
            continue
        seen.add((key, nv))
        tags.append([k, nv])
    for num, total in (('TRACKNUMBER', 'TRACKTOTAL'), ('DISCNUMBER', 'DISCTOTAL')):
        for i, (k, v) in enumerate(tags):
            m = SLASH_RE.match(v) if k.upper() == num else None
            if m:
                tags[i] = [k, m[1]]
                changes.append(f'{num} {v!r} -> {m[1]!r}')
                if int_tag(m[2]) and not any(kk.upper() == total for kk, _ in tags):
                    tags.append([total, m[2]])
                    changes.append(f'add {total}={m[2]!r}')
    for key in ALBUM_TAGS:
        vals = album.consensus(key)
        if vals and not any(k.upper() == key for k, _ in tags):
            tags += [[key, v] for v in vals]
            changes.append(f'add {key}={"; ".join(vals)!r} (as on the rest of the album)')
    if album_year:
        years = {v.strip()[:4] for tr in album.tracks for v in tr.get('DATE') if DATE_RE.match(v.strip())}
        year = years.pop() if len(years) == 1 else None
        blocked = [os.path.basename(tr.path) for tr in album.tracks if tr.unwritable or not tr.layout]
        for i, (k, v) in enumerate(tags):
            if k.upper() != 'ALBUM':
                continue
            if blocked:  # renaming the others would split the album in two
                changes.append(f'(--album-year: skipped, {blocked[0]} in this album can\'t be changed)')
            elif not year:
                changes.append('(--album-year: skipped, the album\'s tracks have no DATE year they agree on)')
            elif not v.rstrip().endswith(f'({year})'):
                tags[i] = [k, f'{v.rstrip()} ({year})']
                changes.append(f'ALBUM {v!r} -> {tags[i][1]!r}')
    pictures = []
    for i, p in enumerate(t.pictures, 1):
        h = picture_header(p)
        info = image_info(p.data) if p.mime != '-->' else None
        if info:
            mime, w, hh, depth, colors = info
            if p.mime.lower() != mime:
                changes.append(f'picture #{i}: MIME {p.mime!r} -> {mime!r}')
                h['mime'] = mime
            if header_wrong(p, info):
                changes.append(f'picture #{i}: header {p.width}x{p.height}, depth {p.depth} -> {w}x{hh}, depth {depth}')
                h.update(width=w, height=hh, depth=depth, colors=colors)
        pictures.append(h)
    strip = t.layout.flac_start > 0 or t.layout.audio_end < t.layout.size
    if strip:
        changes.append(f'strip glued ID3/APE tags ({fmt_size(t.layout.flac_start + t.layout.size - t.layout.audio_end)})')
    seek = not t.seektable and can_add_seektable(t)
    if seek:
        changes.append('add SEEKTABLE (a point every 10 s)')
    if all(c.startswith('(') for c in changes):
        return None, changes
    # prefix/suffix None: keep whatever bytes are glued on now
    return {'tags': tags, 'pictures': pictures, 'seektable': t.seektable or seek,
            'prefix': '' if strip else None, 'suffix': '' if strip else None}, changes


def can_add_seektable(t):
    return bool(shutil.which('metaflac')) and t.samples > 0


def cmd_fix(opts):
    lock = write_lock() if opts.apply else None  # held from before the scan, so the plan can't go stale
    opts.quick, opts.no_spectrum = True, True
    albums = scan(opts.paths, opts, 'Reading')
    root = display_root(opts.paths)
    plans = []
    for a in albums:
        for t in a.tracks:
            if not t.layout:
                print(f'skip {show(t.path, root)}: unreadable', file=sys.stderr)
                continue
            target, changes = plan_fix(t, a, opts.album_year)
            if changes:
                print(show(t.path, root))
                for c in changes:
                    print(f'    {c}')
            if target:
                plans.append((show(t.path, root), t, target))
    if not plans:
        print('Nothing to fix.')
        return 0
    if not opts.apply:
        print(f'\n{len(plans)} files would change. Run again with --apply to write them.')
        return 0
    done, failed, log = apply_writes('fix', opts.jobs, plans,
                                     lambda it, log: rewrite(it[1].path, it[2], log, expect=it[1].seen_stat))
    print(f'\nChanged {done} files{f", {len(failed)} failed" if failed else ""}.')
    if log.path:
        print(f'Undo log: {log.path}\n  preview undo: flacmeta undo {log.path}')
    del lock
    return 1 if failed else 0


# ---------------------------------------------------------------------------------------------
# replaygain


def rg_done(album):
    for t in album.tracks:
        if not all(t.first(k) and (GAIN_RE if k.endswith('GAIN') else PEAK_RE).match(t.first(k)) for k in RG_TAGS):
            return False
    return all(len({t.first(k) for t in album.tracks}) == 1 for k in RG_TAGS[2:])


def non_rg(tags):
    return Counter((k.upper(), v) for k, v in tags if not k.upper().startswith('REPLAYGAIN_'))


def rg_order(before_tags, rsgain_tags):
    """The file's own tags, in its own order and key case, with rsgain's REPLAYGAIN_* values
    in place of the old ones (rsgain/TagLib re-sorts and upper-cases every key)."""
    new = {}
    for k, v in rsgain_tags:
        if k.upper().startswith('REPLAYGAIN_'):
            new.setdefault(k.upper(), v)
    out, used = [], set()
    for k, v in before_tags:
        key = k.upper()
        if not key.startswith('REPLAYGAIN_'):
            out.append([k, v])
        elif key in new and key not in used:
            out.append([k, new[key]])
            used.add(key)
    rank = {k: i for i, k in enumerate(RG_TAGS)}
    return out + [[k, v] for k, v in sorted(new.items(), key=lambda kv: rank.get(kv[0], len(rank))) if k not in used]


def decodes(path):
    if shutil.which('flac'):
        return run(['flac', '-t', '-s', path]).returncode == 0
    r = run(['ffmpeg', '-nostdin', '-v', 'error', '-i', 'file:' + path, '-map', '0:a:0', '-f', 'null', '-'])
    return r.returncode == 0 and not r.stderr.strip()


def without_rg(state):
    return ([kv for kv in state['tags'] if not kv[0].upper().startswith('REPLAYGAIN_')],
            state['pictures'], state['seektable'], state['prefix'], state['suffix'])


def replaygain_album(album, log):
    """rsgain on copies of every track of the album at once (one album gain). The tracks are then
    replaced back to back, so an album is never left with two different album gains."""
    prep = []
    for t in sorted(album.tracks, key=lambda t: t.path):
        stat0 = stat_key(writable_stat(t.path))
        if stat0 != t.seen_stat:
            raise BadFile(f'{os.path.basename(t.path)} changed since flacmeta read it; run again')
        if not decodes(t.path):  # a damaged track would still get a gain, and skew the album's
            raise BadFile(f"{os.path.basename(t.path)} does not decode (see flacmeta check), so its ReplayGain can't be measured")
        layout = read_layout(t.path)
        tags = FLAC(t.path).tags
        vendor = tags.vendor if tags is not None else None
        prep.append((t, stat0, layout, audio_digest(t.path, layout), file_state(t.path, layout), vendor))
    tmps = []
    try:
        for t, _, layout, *_ in prep:
            tmps.append(make_copy(t.path, layout))
        r = run(['rsgain'] + RSGAIN_ARGS + ['--'] + tmps)
        if r.returncode:
            raise BadFile(f'rsgain failed: {tail(r.stderr or r.stdout)}')
        afters = []
        for (t, _, layout, digest, before, vendor), tmp in zip(prep, tmps):
            name = os.path.basename(t.path)
            f = FLAC(tmp)
            tagged = list(f.tags or [])
            rg = {k.upper(): v for k, v in tagged if k.upper().startswith('REPLAYGAIN_')}
            if not all(k in rg for k in RG_TAGS):
                raise BadFile(f'rsgain did not write all ReplayGain tags to {name}')
            if not PEAK_RE.match(rg['REPLAYGAIN_TRACK_PEAK']) or not GAIN_RE.match(rg['REPLAYGAIN_TRACK_GAIN']):
                raise BadFile(f'rsgain wrote unreadable values to {name}')
            del f.tags[:]
            f.tags.extend(tuple(kv) for kv in rg_order(before['tags'], tagged))
            if vendor is not None:
                f.tags.vendor = vendor
            f.save(deleteid3=False)
            copy_attrs(t.path, tmp)
            verify_copy(t.path, layout, digest, tmp)
            after = file_state(tmp)
            if without_rg(after) != without_rg(before):
                raise BadFile(f'rsgain changed more than the ReplayGain tags of {name}')
            afters.append(after)
        for t, stat0, *_ in prep:
            if stat_key(os.stat(t.path)) != stat0:
                raise BadFile(f'{os.path.basename(t.path)} changed while flacmeta was working on it; run again')
        if STOP.is_set():
            raise KeyboardInterrupt
        for (t, _, _, _, before, _), after in zip(prep, afters):
            log.write({'path': t.path, 'note': 'replaygain', 'before': before, 'after': after})
        replaced = 0
        try:
            for (t, stat0, *_), tmp in zip(prep, tmps):
                replace(t.path, tmp, stat0, check_stop=False)  # no stopping halfway through an album
                replaced += 1
        except (BadFile, OSError) as e:
            raise BadFile(f'album left partly tagged ({replaced} of {len(prep)} tracks replaced; '
                          f'all are in the undo log): {e}')
    finally:
        for tmp in tmps:
            if os.path.exists(tmp):
                os.unlink(tmp)


def replaygain_blocker(album):
    """Why this album can't be ReplayGain-tagged safely, or None."""
    if any(not t.layout for t in album.tracks):
        return 'has unreadable files'
    if not album.complete:
        return 'only part of the album was given (album gain needs every track)'
    for t in album.tracks:
        name = os.path.basename(t.path)
        if t.layout.flac_start or t.layout.audio_end < t.layout.size:
            return f'{name} has glued ID3/APE tags (rsgain would rewrite them): run flacmeta fix --apply first'
        if t.unwritable:
            return f'{name} {t.unwritable}'
    return None


def cmd_replaygain(opts):
    if not shutil.which('rsgain'):
        sys.exit('flacmeta: needs rsgain (chaotic-aur): sudo pacman -S --needed rsgain')
    lock = write_lock() if opts.apply else None  # held from before the scan, so the plan can't go stale
    opts.quick, opts.no_spectrum = True, True
    albums = scan(opts.paths, opts, 'Reading')
    root = display_root(opts.paths)
    todo, skipped = [], 0
    for a in albums:
        blocker = replaygain_blocker(a)
        if blocker:
            print(f'skip {show(a.dir, root)}: {blocker}', file=sys.stderr)
        elif rg_done(a) and not opts.force:
            skipped += 1
        else:
            discs = len({disc_of(t) for t in a.tracks})
            print(f'scan  {show(a.dir, root)}  ({len(a.tracks)} tracks{f", {discs} discs" if discs > 1 else ""})')
            todo.append((show(a.dir, root), a))
    if skipped:
        print(f'skip  {skipped} album(s) that already have ReplayGain tags (--force rescans them)')
    if not todo:
        return 0
    if not opts.apply:
        print(f'\n{len(todo)} albums would be scanned (rsgain, ReplayGain 2.0, -18 LUFS). Run again with --apply.')
        return 0
    done, failed, log = apply_writes('replaygain', max(1, opts.jobs // 2), todo,
                                     lambda it, log: replaygain_album(it[1], log))
    print(f'\nTagged {done} album(s){f", {len(failed)} failed" if failed else ""}.')
    if log.path:
        print(f'Undo log: {log.path}')
    del lock
    return 1 if failed else 0


# ---------------------------------------------------------------------------------------------
# undo

STATE_FIELDS = ('tags', 'pictures', 'seektable', 'prefix', 'suffix')


def cmd_undo(opts):
    lock = write_lock() if opts.apply else None
    records = []
    try:
        with open(opts.log, encoding='utf-8', errors='replace') as fh:
            for n, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                    if not all(isinstance(r[k], dict) and all(f in r[k] for f in STATE_FIELDS) for k in ('before', 'after')):
                        raise KeyError('before/after state is incomplete')
                    records.append((r['path'], r['before'], r['after']))
                except (json.JSONDecodeError, KeyError, TypeError) as e:
                    # a record cut short by a crash: its file was never replaced (records come first)
                    print(f'warning: line {n} of the undo log is unreadable and was skipped ({e})', file=sys.stderr)
    except OSError as e:
        sys.exit(f'flacmeta: cannot read undo log: {e}')
    if not records:
        sys.exit('flacmeta: the undo log has no readable records')
    latest = {}
    for path, before, after in records:  # a file may appear more than once: first "before", last "after"
        latest.setdefault(path, {'before': before})['after'] = after
    plans = []
    for path, r in latest.items():
        try:
            st = writable_stat(path)
            now = file_state(path)
        except FileNotFoundError:
            print(f'skip {path}: file is gone')
            continue
        except (BadFile, OSError, MutagenError) as e:
            print(f'skip {path}: {e}')
            continue
        # restore only what this log changed, so later runs' changes to other fields survive
        changed = [k for k in STATE_FIELDS if r['before'][k] != r['after'][k]]
        if all(now[k] == r['before'][k] for k in changed):
            continue
        if any(now[k] != r['after'][k] for k in changed) and not opts.force:
            print(f'skip {path}: changed since this log was written (--force restores anyway)')
            continue
        print(f'restore {path}  ({", ".join(changed)})')
        plans.append((path, dict(now, **{k: r['before'][k] for k in changed}), stat_key(st)))
    if not plans:
        print('Nothing to undo.')
        return 0
    if not opts.apply:
        print(f'\n{len(plans)} files would be restored. Run again with --apply.')
        return 0
    done, failed, log = apply_writes('undo', opts.jobs, plans,
                                     lambda it, log: rewrite(it[0], it[1], log, expect=it[2], note=f'undo {opts.log}'))
    print(f'\nRestored {done} files{f", {len(failed)} failed" if failed else ""}.')
    if log.path:
        print(f'Undo log for this undo: {log.path}')
    del lock
    return 1 if failed else 0


# ---------------------------------------------------------------------------------------------
# info, spectrogram


def cmd_info(opts):
    for path in opts.files:
        t = analyse(os.path.abspath(path), opts)
        print(f'File        {t.path}')
        if not t.layout:
            for f in t.findings:
                print(f'  {f.level} {f.message}')
            continue
        ch = {1: 'mono', 2: 'stereo'}.get(t.channels, f'{t.channels} ch')
        mins, secs = divmod(t.duration, 60)
        kbps = (t.layout.audio_end - t.layout.audio_start) * 8 / t.duration / 1000 if t.duration else 0
        print(f'Format      FLAC {t.bits}-bit {fmt_rate(t.rate)} {ch}, {int(mins)}:{secs:06.3f}, '
              f'{fmt_size(t.layout.size)}, {kbps:.0f} kb/s')
        if t.eff_bits is not None:
            print(f'Bits used   {t.eff_bits} of {t.bits} (measured on up to {EXCERPT_S} s)')
        print(f'MD5         {t.md5:032x}' if t.md5 else 'MD5         not set')
        blocks = []
        for btype, _, n in t.layout.blocks:
            name = BLOCK_NAMES.get(btype, f'type {btype}')
            blocks.append(f'{name} {n // 18} points' if btype == 3 else f'{name} {fmt_size(n)}')
        print(f'Blocks      {", ".join(blocks)}')
        if t.layout.flac_start or t.layout.audio_end < t.layout.size:
            print(f'Glued tags  {fmt_size(t.layout.flac_start)} in front, {fmt_size(t.layout.size - t.layout.audio_end)} at the end')
        for i, p in enumerate(t.pictures, 1):
            info = image_info(p.data)
            real = f', image is {info[1]}x{info[2]} {info[0]}' if info else ', unreadable image'
            print(f'Picture #{i}  type {p.type}, {p.mime}, header {p.width}x{p.height}{real}, {fmt_size(len(p.data))}')
        if t.spectrum:
            edge, drop = t.spectrum['edge_hz'] / 1000, t.spectrum['cliff_db']
            if drop >= LOSSY_CLIFF_DB:
                print(f'Spectrum    nothing above {edge:.1f} kHz ({drop:.0f} dB cliff)')
            else:
                print(f'Spectrum    no sharp cutoff (largest drop {drop:.0f} dB, at {edge:.1f} kHz)')
        print('Tags')
        for k, v in t.tags:
            if '\n' in v:
                v = f'[{v.count(chr(10)) + 1} lines] {v.splitlines()[0][:60]}'
            print(f'  {k}={v[:200]}')
        if t.findings:
            print('Findings')
            for f in t.findings:
                print(f'  {f.level:<5} {f.message}{"  [fixable]" if f.fixable else ""}')
        print()
    return 0


def cmd_spectrogram(opts):
    src = os.path.abspath(opts.file)
    stem = os.path.splitext(os.path.basename(src))[0].encode()[:200].decode(errors='ignore')  # file names max 255 bytes
    out = opts.output or f'{stem} spectrogram.png'
    if os.path.exists(out) and not opts.force:
        sys.exit(f'flacmeta: {out} exists (--force overwrites it)')
    r = run(['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error', '-y', '-i', 'file:' + src, '-filter_complex',
             '[0:a:0]showspectrumpic=s=1600x800:legend=1:scale=log:win_func=bharris:drange=150[v]', '-map', '[v]',
             '-frames:v', '1', '-f', 'image2', '-c:v', 'png', '-update', '1', 'file:' + os.path.abspath(out)])
    if r.returncode:
        sys.exit(f'flacmeta: ffmpeg failed: {tail(r.stderr)}')
    print(out)
    return 0


# ---------------------------------------------------------------------------------------------


def main(argv=None):
    p = argparse.ArgumentParser(prog='flacmeta', description='Check, fix and ReplayGain-tag a FLAC library.')
    p.add_argument('-j', '--jobs', type=int, default=os.cpu_count() or 4, help='parallel files (default: CPU count)')
    jobs = argparse.ArgumentParser(add_help=False)  # so -j also works after the command
    jobs.add_argument('-j', '--jobs', type=int, default=argparse.SUPPRESS, help='parallel files (default: CPU count)')
    sub = p.add_subparsers(dest='cmd', required=True)

    def command(name, **kw):
        return sub.add_parser(name, parents=[jobs], **kw)
    c = command('check', help='read-only health report')
    c.add_argument('paths', nargs='+')
    c.add_argument('-v', '--verbose', action='store_true', help='list INFO findings too')
    c.add_argument('--quick', action='store_true', help='skip decoding: tags, covers and layout only')
    c.add_argument('--no-spectrum', action='store_true', help='skip the lossy/upsampling analysis')
    c.add_argument('--json', action='store_true', help='JSON lines: every track with its measurements and findings')
    f = command('fix', help='preview tag/cover/layout fixes; --apply writes them')
    f.add_argument('paths', nargs='+')
    f.add_argument('--album-year', action='store_true', help="ALBUM=Curtis -> 'Curtis (1970)', year from DATE")
    f.add_argument('--apply', action='store_true', help='write the changes')
    r = command('replaygain', help='ReplayGain 2.0 per album with rsgain; --apply writes')
    r.add_argument('paths', nargs='+')
    r.add_argument('--force', action='store_true', help='rescan albums that already have ReplayGain tags')
    r.add_argument('--apply', action='store_true', help='write the tags')
    i = command('info', help='everything about one or more files')
    i.add_argument('files', nargs='+')
    s = command('spectrogram', help='write a spectrogram PNG')
    s.add_argument('file')
    s.add_argument('-o', '--output', help="PNG path (default: './<name> spectrogram.png')")
    s.add_argument('--force', action='store_true', help='overwrite an existing PNG')
    u = command('undo', help='preview restoring the files in an undo log; --apply restores')
    u.add_argument('log')
    u.add_argument('--apply', action='store_true')
    u.add_argument('--force', action='store_true', help='restore files even if what the log changed was changed again since')
    opts = p.parse_args(argv)
    opts.jobs = max(1, opts.jobs)
    opts.quick = getattr(opts, 'quick', False)
    opts.no_spectrum = getattr(opts, 'no_spectrum', False)
    if not shutil.which('ffmpeg') and (opts.cmd in ('info', 'spectrogram') or (opts.cmd == 'check' and not opts.quick)):
        sys.exit('flacmeta: needs ffmpeg: sudo pacman -S --needed ffmpeg')
    for stream in (sys.stdout, sys.stderr):  # a file name that isn't UTF-8 must not crash a long run
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors='backslashreplace')

    def on_term(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)
    try:
        return {'check': cmd_check, 'fix': cmd_fix, 'replaygain': cmd_replaygain, 'info': cmd_info,
                'spectrogram': cmd_spectrogram, 'undo': cmd_undo}[opts.cmd](opts)
    except KeyboardInterrupt:
        STOP.set()
        print('\nflacmeta: interrupted; files (or an album) already being swapped in were finished, '
              'the rest were left unchanged', file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 141


if __name__ == '__main__':
    sys.exit(main())
