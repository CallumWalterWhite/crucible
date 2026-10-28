"""FARM_FINGERPRINT: FarmHash Fingerprint64 of a STRING's UTF-8 bytes or of BYTES, as a signed
INT64 (https://cloud.google.com/bigquery/docs/reference/standard-sql/hash_functions#farm_fingerprint).

A port of farmhashna::Hash64, which Fingerprint64 is, from https://github.com/google/farmhash
(src/farmhash.cc; MIT, see NOTICE). Every operation is modulo 2**64. The code paths differ by
input length (0-16, 17-32, 33-64, over 64 bytes), and the corpus cases cover each.

Runs as a DuckDB Python function (`FUNCTION`), registered on the engine on first use.
"""

from __future__ import annotations

FUNCTION = "crucible_farm_fingerprint"

_MASK = (1 << 64) - 1
_K0 = 0xC3A5C85C97CB3127
_K1 = 0xB492B66FBE98F273
_K2 = 0x9AE16A3B2F90404F


def _fetch64(s: bytes, i: int) -> int:
    return int.from_bytes(s[i : i + 8], "little")


def _fetch32(s: bytes, i: int) -> int:
    return int.from_bytes(s[i : i + 4], "little")


def _rotate(value: int, shift: int) -> int:
    return value if shift == 0 else ((value >> shift) | (value << (64 - shift))) & _MASK


def _shift_mix(value: int) -> int:
    return value ^ (value >> 47)


def _hash_len16(u: int, v: int, mul: int) -> int:
    a = ((u ^ v) * mul) & _MASK
    a ^= a >> 47
    b = ((v ^ a) * mul) & _MASK
    b ^= b >> 47
    return (b * mul) & _MASK


def _hash_len0to16(s: bytes) -> int:
    n = len(s)
    if n >= 8:
        mul = _K2 + n * 2
        a = (_fetch64(s, 0) + _K2) & _MASK
        b = _fetch64(s, n - 8)
        c = (_rotate(b, 37) * mul + a) & _MASK
        d = ((_rotate(a, 25) + b) * mul) & _MASK
        return _hash_len16(c, d, mul)
    if n >= 4:
        mul = _K2 + n * 2
        a = _fetch32(s, 0)
        return _hash_len16((n + (a << 3)) & _MASK, _fetch32(s, n - 4), mul)
    if n > 0:
        y = (s[0] + (s[n >> 1] << 8)) & 0xFFFFFFFF
        z = (n + (s[n - 1] << 2)) & 0xFFFFFFFF
        return (_shift_mix(((y * _K2) ^ (z * _K0)) & _MASK) * _K2) & _MASK
    return _K2


def _hash_len17to32(s: bytes) -> int:
    n = len(s)
    mul = _K2 + n * 2
    a = (_fetch64(s, 0) * _K1) & _MASK
    b = _fetch64(s, 8)
    c = (_fetch64(s, n - 8) * mul) & _MASK
    d = (_fetch64(s, n - 16) * _K2) & _MASK
    return _hash_len16(
        (_rotate((a + b) & _MASK, 43) + _rotate(c, 30) + d) & _MASK,
        (a + _rotate((b + _K2) & _MASK, 18) + c) & _MASK,
        mul,
    )


def _hash_len33to64(s: bytes) -> int:
    n = len(s)
    mul = _K2 + n * 2
    a = (_fetch64(s, 0) * _K2) & _MASK
    b = _fetch64(s, 8)
    c = (_fetch64(s, n - 8) * mul) & _MASK
    d = (_fetch64(s, n - 16) * _K2) & _MASK
    y = (_rotate((a + b) & _MASK, 43) + _rotate(c, 30) + d) & _MASK
    z = _hash_len16(y, (a + _rotate((b + _K2) & _MASK, 18) + c) & _MASK, mul)
    e = (_fetch64(s, 16) * mul) & _MASK
    f = _fetch64(s, 24)
    g = ((y + _fetch64(s, n - 32)) * mul) & _MASK
    h = ((z + _fetch64(s, n - 24)) * mul) & _MASK
    return _hash_len16(
        (_rotate((e + f) & _MASK, 43) + _rotate(g, 30) + h) & _MASK,
        (e + _rotate((f + a) & _MASK, 18) + g) & _MASK,
        mul,
    )


def _weak_hash_len32_with_seeds(s: bytes, i: int, a: int, b: int) -> tuple[int, int]:
    w, x, y, z = _fetch64(s, i), _fetch64(s, i + 8), _fetch64(s, i + 16), _fetch64(s, i + 24)
    a = (a + w) & _MASK
    b = _rotate((b + a + z) & _MASK, 21)
    c = a
    a = (a + x + y) & _MASK
    b = (b + _rotate(a, 44)) & _MASK
    return (a + z) & _MASK, (b + c) & _MASK


def fingerprint64(s: bytes) -> int:
    """farmhashna::Hash64, unsigned."""
    n = len(s)
    if n <= 16:
        return _hash_len0to16(s)
    if n <= 32:
        return _hash_len17to32(s)
    if n <= 64:
        return _hash_len33to64(s)
    seed = 81
    x = seed
    y = (seed * _K1 + 113) & _MASK
    z = (_shift_mix((y * _K2 + 113) & _MASK) * _K2) & _MASK
    v = (0, 0)
    w = (0, 0)
    x = (x * _K2 + _fetch64(s, 0)) & _MASK
    end = ((n - 1) // 64) * 64  # after the loop, 1 to 64 bytes are left
    last64 = end + ((n - 1) & 63) - 63
    i = 0
    while True:
        x = (_rotate((x + y + v[0] + _fetch64(s, i + 8)) & _MASK, 37) * _K1) & _MASK
        y = (_rotate((y + v[1] + _fetch64(s, i + 48)) & _MASK, 42) * _K1) & _MASK
        x ^= w[1]
        y = (y + v[0] + _fetch64(s, i + 40)) & _MASK
        z = (_rotate((z + w[0]) & _MASK, 33) * _K1) & _MASK
        v = _weak_hash_len32_with_seeds(s, i, (v[1] * _K1) & _MASK, (x + w[0]) & _MASK)
        w = _weak_hash_len32_with_seeds(
            s, i + 32, (z + w[1]) & _MASK, (y + _fetch64(s, i + 16)) & _MASK
        )
        z, x = x, z
        i += 64
        if i == end:
            break
    mul = _K1 + ((z & 0xFF) << 1)
    i = last64
    w = ((w[0] + ((n - 1) & 63)) & _MASK, w[1])
    v = ((v[0] + w[0]) & _MASK, v[1])
    w = ((w[0] + v[0]) & _MASK, w[1])
    x = (_rotate((x + y + v[0] + _fetch64(s, i + 8)) & _MASK, 37) * mul) & _MASK
    y = (_rotate((y + v[1] + _fetch64(s, i + 48)) & _MASK, 42) * mul) & _MASK
    x ^= (w[1] * 9) & _MASK
    y = (y + v[0] * 9 + _fetch64(s, i + 40)) & _MASK
    z = (_rotate((z + w[0]) & _MASK, 33) * mul) & _MASK
    v = _weak_hash_len32_with_seeds(s, i, (v[1] * mul) & _MASK, (x + w[0]) & _MASK)
    w = _weak_hash_len32_with_seeds(
        s, i + 32, (z + w[1]) & _MASK, (y + _fetch64(s, i + 16)) & _MASK
    )
    z, x = x, z
    return _hash_len16(
        (_hash_len16(v[0], w[0], mul) + _shift_mix(y) * _K0 + z) & _MASK,
        (_hash_len16(v[1], w[1], mul) + x) & _MASK,
        mul,
    )


def farm_fingerprint(data: bytes | None) -> int | None:
    """The DuckDB function: Fingerprint64 as BigQuery's signed INT64."""
    if data is None:
        return None
    value = fingerprint64(data)
    return value - (1 << 64) if value >= 1 << 63 else value
