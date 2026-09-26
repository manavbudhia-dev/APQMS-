"""Password-protect database backups using only the Python standard library
(no extra packages to install on the PC or the hosting account).

File layout of an encrypted backup (".db.enc"):

    MAGIC (9 bytes)  b'APQMSENC1'
    iterations       4 bytes, big-endian  (PBKDF2 work factor used for this file)
    salt             16 random bytes      (fresh per file)
    nonce            16 random bytes      (fresh per file)
    ciphertext       same length as the original database
    tag              32 bytes             (HMAC-SHA256 over everything above)

Construction (encrypt-then-MAC):
  * PBKDF2-HMAC-SHA256 turns the password + salt into 64 bytes, split into
    an encryption key and a separate MAC key.
  * The database is XOR-ed with a SHAKE-256 keystream derived from
    (encryption key + nonce) — a standard stream-cipher construction.
  * HMAC-SHA256 over header + ciphertext is checked BEFORE anything is
    decrypted, so a wrong password or a damaged/tampered file is rejected
    cleanly instead of producing garbage.

If the `cryptography` package is ever added to this project, swapping this
for AES-GCM is a drop-in change confined to this one file.
"""
import hashlib
import hmac
import os
import unicodedata

MAGIC = b'APQMSENC1'
ITERATIONS = 600_000            # OWASP-recommended range for PBKDF2-SHA256
MIN_ITERATIONS = 100_000        # sanity bounds when reading a file's header,
MAX_ITERATIONS = 5_000_000      # so a crafted file can't stall the server
SALT_LEN = 16
NONCE_LEN = 16
TAG_LEN = 32
MIN_PASSWORD_LEN = 8
_HEADER_LEN = len(MAGIC) + 4 + SALT_LEN + NONCE_LEN


def is_encrypted(data):
    """True if `data` looks like a backup produced by encrypt_bytes()."""
    return data[:len(MAGIC)] == MAGIC


def _derive_keys(password, salt, iterations):
    # NFKC-normalise so the same password typed on a different keyboard /
    # input method still matches.
    pw = unicodedata.normalize('NFKC', password).encode('utf-8')
    material = hashlib.pbkdf2_hmac('sha256', pw, salt, iterations, dklen=64)
    return material[:32], material[32:]


_XOR_CHUNK_SIZE = 1024 * 1024  # 1 MiB


def _xor_keystream(enc_key, nonce, data):
    """XOR `data` against a SHAKE-256 keystream derived from (enc_key +
    nonce), one chunk at a time. int.from_bytes(data, 'big') on the whole
    buffer (the previous implementation) turns the *entire* file into one
    giant Python integer — and does the same again for the keystream and
    the XOR result — which for a large database means several times its
    size resident in RAM simultaneously. Converting only one 1 MiB slice
    to an int at a time bounds that overhead to a constant, regardless of
    file size, while producing the exact same keystream/ciphertext as
    before (so existing .db.enc backups keep decrypting correctly)."""
    n = len(data)
    if not n:
        return b''
    stream = hashlib.shake_256(enc_key + nonce).digest(n)
    out = bytearray(n)
    mv_data, mv_stream = memoryview(data), memoryview(stream)
    for start in range(0, n, _XOR_CHUNK_SIZE):
        end = min(start + _XOR_CHUNK_SIZE, n)
        chunk_len = end - start
        d = int.from_bytes(mv_data[start:end], 'big')
        s = int.from_bytes(mv_stream[start:end], 'big')
        out[start:end] = (d ^ s).to_bytes(chunk_len, 'big')
    return bytes(out)


def encrypt_bytes(data, password):
    """Return the password-protected form of `data`."""
    if not password:
        raise ValueError('A password is required.')
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f'Password must be at least {MIN_PASSWORD_LEN} characters long.')
    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    enc_key, mac_key = _derive_keys(password, salt, ITERATIONS)
    header = MAGIC + ITERATIONS.to_bytes(4, 'big') + salt + nonce
    body = _xor_keystream(enc_key, nonce, data)
    tag = hmac.new(mac_key, header + body, hashlib.sha256).digest()
    return header + body + tag


def decrypt_bytes(blob, password):
    """Reverse encrypt_bytes(). Raises ValueError (with a message safe to
    show the user) if the file is damaged, or the password is wrong."""
    if not is_encrypted(blob) or len(blob) < _HEADER_LEN + TAG_LEN:
        raise ValueError('This is not a valid encrypted backup file.')
    if not isinstance(password, str) or not password:
        # Guard against None/non-str reaching unicodedata.normalize()
        # below, which raises TypeError rather than the ValueError this
        # function documents (and promises callers) it raises.
        raise ValueError('A password is required.')
    pos = len(MAGIC)
    iterations = int.from_bytes(blob[pos:pos + 4], 'big')
    pos += 4
    salt = blob[pos:pos + SALT_LEN]
    pos += SALT_LEN
    nonce = blob[pos:pos + NONCE_LEN]
    pos += NONCE_LEN
    body, tag = blob[pos:-TAG_LEN], blob[-TAG_LEN:]
    if not (MIN_ITERATIONS <= iterations <= MAX_ITERATIONS):
        raise ValueError('This is not a valid encrypted backup file.')

    enc_key, mac_key = _derive_keys(password, salt, iterations)
    expected = hmac.new(mac_key, blob[:pos] + body, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, tag):
        raise ValueError('Wrong password, or the backup file is damaged.')
    return _xor_keystream(enc_key, nonce, body)
