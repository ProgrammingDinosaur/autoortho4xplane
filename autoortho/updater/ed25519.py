"""Ed25519 signatures (RFC 8032), in pure Python.

Adapted from the reference implementation in RFC 8032 section 6. It only
handles one small manifest per update check, so speed does not matter, and
it avoids adding a compiled crypto dependency to the frozen app. Not
constant-time: fine for verifying public data, and the signing side runs in
CI only.
"""

import hashlib

_P = 2**255 - 19
_Q = 2**252 + 27742317777372353535851937790883648493


def _inv(x):
    return pow(x, _P - 2, _P)


_D = -121665 * _inv(121666) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _sha512(data):
    return hashlib.sha512(data).digest()


def _sha512_mod_q(data):
    return int.from_bytes(_sha512(data), "little") % _Q


def _add(a, b):
    # Points are extended homogeneous coordinates (X, Y, Z, T).
    pa = (a[1] - a[0]) * (b[1] - b[0]) % _P
    pb = (a[1] + a[0]) * (b[1] + b[0]) % _P
    pc = 2 * a[3] * b[3] * _D % _P
    pd = 2 * a[2] * b[2] % _P
    e, f, g, h = pb - pa, pd - pc, pd + pc, pb + pa
    return (e * f, g * h, f * g, e * h)


def _mul(scalar, point):
    result = (0, 1, 1, 0)
    while scalar > 0:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _equal(a, b):
    if (a[0] * b[2] - b[0] * a[2]) % _P != 0:
        return False
    return (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _recover_x(y, sign):
    if y >= _P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * _inv(5) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _compress(point):
    zinv = _inv(point[2])
    x = point[0] * zinv % _P
    y = point[1] * zinv % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(data):
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _expand(secret):
    if len(secret) != 32:
        raise ValueError("An Ed25519 private key is 32 bytes.")
    digest = _sha512(secret)
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    return scalar, digest[32:]


def public_key(secret):
    """The 32-byte public key for a 32-byte private key (seed)."""
    scalar, _prefix = _expand(secret)
    return _compress(_mul(scalar, _G))


def sign(secret, message):
    """A 64-byte signature of ``message``."""
    scalar, prefix = _expand(secret)
    encoded_public = _compress(_mul(scalar, _G))
    r = _sha512_mod_q(prefix + message)
    encoded_r = _compress(_mul(r, _G))
    h = _sha512_mod_q(encoded_r + encoded_public + message)
    s = (r + h * scalar) % _Q
    return encoded_r + int.to_bytes(s, 32, "little")


def verify(public, message, signature):
    """True only for a valid signature of ``message`` by ``public``."""
    if len(public) != 32 or len(signature) != 64:
        return False
    point_a = _decompress(public)
    if point_a is None:
        return False
    encoded_r = signature[:32]
    point_r = _decompress(encoded_r)
    if point_r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _Q:
        return False
    h = _sha512_mod_q(encoded_r + public + message)
    return _equal(_mul(s, _G), _add(point_r, _mul(h, point_a)))
