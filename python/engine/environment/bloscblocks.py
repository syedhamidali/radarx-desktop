"""Read part of a Blosc-compressed Zarr chunk without downloading the whole chunk.

A Zarr v2 chunk written with the Blosc codec is a small header, a table of the byte positions of
its blocks, and the blocks themselves, each compressed on its own (Blosc 1 frame format, 16-byte
header: version, versionlz, flags, typesize, nbytes, blocksize, cbytes, then ``int32`` block
starts). So the bytes of one level and a few rows of a huge chunk live in a few consecutive
blocks, and an HTTP ``Range`` request can fetch just those.

This module does the arithmetic and rebuilds, for every block fetched, a tiny valid Blosc frame
that the ordinary decoder (``numcodecs``, a dependency of Zarr) reads. Nothing here decodes
compressed data itself.

A piece of a chunk is stored in the cache as a *container*: ``b"RXB1"``, a 4-byte header length,
a JSON header (everything needed to decode: block size, flags, where the blocks start, which part
of the chunk's raw bytes the caller asked for) and the compressed blocks. The container is
self-describing, so it can be read offline long after the listing that produced it is gone.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine.environment.errors import EnvironmentDataError, InvalidRequest

MAGIC = b"RXB1"
HEADER_BYTES = 16


class BloscError(EnvironmentDataError):
    """The chunk is not a Blosc 1 frame this reader understands."""


@dataclass(frozen=True)
class Frame:
    """The facts in a Blosc frame header."""

    flags: int
    typesize: int
    nbytes: int  # size of the chunk when decompressed
    blocksize: int
    cbytes: int  # size of the whole compressed chunk, header included
    starts: tuple[int, ...]  # byte position of each block in the compressed chunk

    @property
    def nblocks(self) -> int:
        return len(self.starts)

    @property
    def memcpyed(self) -> bool:
        return bool(self.flags & 0x02)

    def end_of(self, block: int) -> int:
        """First byte after block *block* in the compressed chunk.

        Blocks are usually stored in order, but a chunk compressed by several threads can have them
        in any order (the table maps block number to position), so the end of a block is the next
        larger start in the table, or the end of the chunk.
        """
        here = self.starts[block]
        later = [s for s in self.starts if s > here]
        return min(later) if later else self.cbytes


def blocks_needed(nbytes: int, blocksize: int) -> int:
    return -(-nbytes // blocksize)


def header_length_for(first_bytes: bytes) -> int:
    """How many bytes from the start of a chunk are needed to know every block position."""
    if len(first_bytes) < HEADER_BYTES:
        raise BloscError("The chunk is shorter than a Blosc header.")
    nbytes, blocksize = struct.unpack_from("<II", first_bytes, 4)
    if blocksize <= 0 or nbytes <= 0:
        raise BloscError("The chunk's Blosc header is not valid.")
    return HEADER_BYTES + 4 * blocks_needed(nbytes, blocksize)


def parse_frame(data: bytes) -> Frame:
    """Parse the header and block table; *data* must hold at least :func:`header_length_for`."""
    need = header_length_for(data)
    if len(data) < need:
        raise BloscError("The chunk header was cut short.")
    version, _versionlz, flags, typesize = data[0], data[1], data[2], data[3]
    nbytes, blocksize, cbytes = struct.unpack_from("<III", data, 4)
    if version not in (2,):
        raise BloscError(f"Blosc format version {version} is not supported.")
    if flags & 0x02:
        raise BloscError("The chunk was stored uncompressed (memcpy); partial reads do not apply.")
    if flags & 0x10:
        raise BloscError("The chunk uses a Blosc 2 feature (delta/frame) that is not supported.")
    n = blocks_needed(nbytes, blocksize)
    starts = struct.unpack_from(f"<{n}I", data, HEADER_BYTES)
    first_data = HEADER_BYTES + 4 * n
    if len(set(starts)) != n or min(starts) < first_data or max(starts) >= cbytes:
        raise BloscError("The chunk's block table is not valid.")
    return Frame(flags, typesize, nbytes, blocksize, cbytes, tuple(starts))


def plan_range(frame: Frame, raw_start: int, raw_end: int) -> tuple[int, int, int, int]:
    """Blocks covering the raw (decompressed) bytes ``[raw_start, raw_end)``.

    Returns ``(first_block, last_block, byte_start, byte_end)``: the compressed byte range
    ``[byte_start, byte_end)`` of the chunk to request.
    """
    if not 0 <= raw_start < raw_end <= frame.nbytes:
        raise InvalidRequest("The requested part lies outside the chunk.")
    first = raw_start // frame.blocksize
    last = (raw_end - 1) // frame.blocksize
    needed = range(first, last + 1)
    return first, last, min(frame.starts[k] for k in needed), max(frame.end_of(k) for k in needed)


# -- containers --------------------------------------------------------------------------------


def container_header(
    frame: Frame, raw_start: int, raw_end: int, *, dtype: str, shape: list[int]
) -> tuple[bytes, int, int]:
    """``(header bytes, byte_start, byte_end)``: the container's first bytes and what to fetch.

    The header length is known before the download, so the size shown to the user is exact:
    ``len(header) + (byte_end - byte_start)``.
    """
    first, last, b0, b1 = plan_range(frame, raw_start, raw_end)
    meta = {
        "flags": frame.flags,
        "typesize": frame.typesize,
        "nbytes": frame.nbytes,
        "blocksize": frame.blocksize,
        "first_block": first,
        "last_block": last,
        # where each needed block lies in the fetched bytes: [offset, length]
        "blocks": [
            [frame.starts[k] - b0, frame.end_of(k) - frame.starts[k]]
            for k in range(first, last + 1)
        ],
        "length": b1 - b0,
        "raw_start": raw_start,
        "raw_end": raw_end,
        "dtype": dtype,
        "shape": shape,
    }
    blob = json.dumps(meta, separators=(",", ":")).encode()
    return MAGIC + struct.pack("<I", len(blob)) + blob, b0, b1


def write_container(dest: Path, header: bytes, payload_path: Path) -> None:
    """Write ``header + payload`` to *dest* (the payload file is read in blocks and removed)."""
    with open(dest, "wb") as out, open(payload_path, "rb") as src:
        out.write(header)
        while chunk := src.read(1 << 20):
            out.write(chunk)
    payload_path.unlink(missing_ok=True)


def read_container(path: Path) -> tuple[dict[str, Any], bytes]:
    data = Path(path).read_bytes()
    if data[:4] != MAGIC:
        raise BloscError(f"{Path(path).name} is not a chunk piece written by this program.")
    (n,) = struct.unpack_from("<I", data, 4)
    meta = json.loads(data[8 : 8 + n])
    return meta, data[8 + n :]


def decode_container(path: Path) -> bytes:
    """The raw bytes ``[raw_start, raw_end)`` of the chunk, decoded from the stored blocks."""
    from numcodecs import Blosc

    meta, payload = read_container(path)
    codec = Blosc()
    blocksize, nbytes = meta["blocksize"], meta["nbytes"]
    if meta["length"] != len(payload):
        raise BloscError(f"{Path(path).name} is damaged: its blocks have the wrong total size.")
    pieces = []
    for n, block in enumerate(range(meta["first_block"], meta["last_block"] + 1)):
        raw_len = min(blocksize, nbytes - block * blocksize)
        off, length = meta["blocks"][n]
        body = payload[off : off + length]
        # A one-block frame. A full block keeps the chunk's flags and block size. The last, short
        # block is stored as one stream instead of one per byte of the element size ("leftover"
        # block of the Blosc format), which a standalone frame says with the don't-split flag
        # and a block size equal to its length. (Checked against the reference decoder.)
        flags, size = meta["flags"], blocksize
        if raw_len < blocksize:
            flags, size = flags | 0x10, raw_len
        head = struct.pack(
            "<BBBBIII", 2, 1, flags, meta["typesize"], raw_len, size, HEADER_BYTES + 4 + len(body)
        )
        frame = head + struct.pack("<I", HEADER_BYTES + 4) + body
        out = bytes(codec.decode(frame))
        if len(out) != raw_len:
            raise BloscError(
                f"{Path(path).name}: a block decoded to {len(out)} bytes, not {raw_len}."
            )
        pieces.append(out)
    raw = b"".join(pieces)
    lo = meta["raw_start"] - meta["first_block"] * blocksize
    return raw[lo : lo + (meta["raw_end"] - meta["raw_start"])]
