"""Partial reads of Blosc-compressed Zarr chunks: block arithmetic and decoding of a few blocks.

The decoded bytes of a piece must be identical to the same slice of the fully decoded chunk.
"""

from __future__ import annotations

import numpy as np
import pytest

from engine.environment import bloscblocks as bb
from engine.environment.errors import InvalidRequest

numcodecs = pytest.importorskip("numcodecs")


def chunk(n_floats=60000, blocksize=4096, seed=0):
    rng = np.random.default_rng(seed)
    data = (np.cumsum(rng.normal(size=n_floats)) + 280.0).astype("<f4")
    codec = numcodecs.Blosc(cname="lz4", clevel=5, shuffle=1, blocksize=blocksize)
    return data, bytes(codec.encode(data))


def test_header_and_block_table():
    data, enc = chunk()
    frame = bb.parse_frame(enc[: bb.header_length_for(enc[:64])])
    assert frame.nbytes == data.nbytes and frame.typesize == 4
    assert frame.blocksize >= 4096  # Blosc raises very small block sizes
    assert frame.nblocks == -(-data.nbytes // frame.blocksize) and frame.nblocks > 2
    assert frame.cbytes == len(enc) and frame.starts[0] == 16 + 4 * frame.nblocks
    assert frame.end_of(frame.nblocks - 1) == len(enc)


@pytest.mark.parametrize(
    "a, b",
    [
        (0, 4096),
        (100, 5000),
        (65536 * 2 + 8, 65536 * 2 + 16),
        (60000, 70000),
        (0, 240000),
        (239000, 240000),
    ],
)
def test_a_piece_decodes_to_exactly_that_slice_of_the_chunk(tmp_path, a, b):
    data, enc = chunk()
    raw = data.tobytes()
    frame = bb.parse_frame(enc)
    header, b0, b1 = bb.container_header(frame, a, b, dtype="<f4", shape=[(b - a) // 4])
    body = tmp_path / "body"
    body.write_bytes(enc[b0:b1])  # what an HTTP range request would return
    path = tmp_path / "piece.rxb"
    bb.write_container(path, header, body)
    assert not body.exists()
    assert path.stat().st_size == len(header) + (b1 - b0)  # the size shown before the download
    assert bb.decode_container(path) == raw[a:b]
    assert b1 - b0 <= len(enc)


def test_the_last_short_block_is_decoded_too(tmp_path):
    data, enc = chunk(n_floats=100000)  # 400000 bytes: full blocks and one short one
    frame = bb.parse_frame(enc)
    assert frame.nbytes % frame.blocksize != 0
    a, b = frame.nbytes - 100, frame.nbytes
    header, b0, b1 = bb.container_header(frame, a, b, dtype="<f4", shape=[25])
    (tmp_path / "body").write_bytes(enc[b0:b1])
    bb.write_container(tmp_path / "p", header, tmp_path / "body")
    assert bb.decode_container(tmp_path / "p") == data.tobytes()[a:b]


def reorder(enc, order):
    """The same chunk with its compressed blocks stored in another order (as multi-threaded
    compression may write them); the table still maps block number to position."""
    import struct

    frame = bb.parse_frame(enc)
    bodies = {k: enc[frame.starts[k] : frame.end_of(k)] for k in range(frame.nblocks)}
    head_len = 16 + 4 * frame.nblocks
    starts, out = {}, b""
    for k in order:
        starts[k] = head_len + len(out)
        out += bodies[k]
    table = struct.pack(f"<{frame.nblocks}I", *(starts[k] for k in range(frame.nblocks)))
    return enc[:16] + table + out


def test_blocks_stored_out_of_order_are_read_correctly(tmp_path):
    data, enc = chunk()
    raw = data.tobytes()
    enc2 = reorder(enc, [2, 0, 3, 1])
    frame = bb.parse_frame(enc2)
    assert list(frame.starts) != sorted(frame.starts)
    for a, b in ((30000, 150000), (0, 240000), (200000, 240000), (70000, 80000)):
        header, b0, b1 = bb.container_header(frame, a, b, dtype="<f4", shape=[(b - a) // 4])
        (tmp_path / "body").write_bytes(enc2[b0:b1])  # one contiguous range, as requested
        bb.write_container(tmp_path / "p", header, tmp_path / "body")
        assert bb.decode_container(tmp_path / "p") == raw[a:b]


def test_a_small_piece_is_a_small_download():
    _data, enc = chunk(n_floats=2_000_000, blocksize=0)
    frame = bb.parse_frame(enc)
    _h, b0, b1 = bb.container_header(frame, 1000, 6000, dtype="<f4", shape=[1250])
    assert (
        frame.nblocks > 8 and (b1 - b0) < len(enc) / 4
    )  # a few kB of a long chunk lie in one or two blocks


def test_errors_are_clear(tmp_path):
    _data, enc = chunk()
    frame = bb.parse_frame(enc)
    with pytest.raises(InvalidRequest, match="outside the chunk"):
        bb.plan_range(frame, 0, frame.nbytes + 1)
    with pytest.raises(bb.BloscError, match="shorter"):
        bb.header_length_for(b"abc")
    stored = numcodecs.Blosc(cname="lz4", clevel=0, shuffle=0).encode(np.arange(100, dtype="<f4"))
    with pytest.raises(bb.BloscError, match="uncompressed"):
        bb.parse_frame(bytes(stored))
    (tmp_path / "x").write_bytes(b"not a piece at all")
    with pytest.raises(bb.BloscError, match="not a chunk piece"):
        bb.read_container(tmp_path / "x")
    header, b0, b1 = bb.container_header(frame, 0, 100, dtype="<f4", shape=[25])
    (tmp_path / "body").write_bytes(enc[b0 : b1 - 5])  # truncated body
    bb.write_container(tmp_path / "p", header, tmp_path / "body")
    with pytest.raises(bb.BloscError, match="damaged"):
        bb.decode_container(tmp_path / "p")
