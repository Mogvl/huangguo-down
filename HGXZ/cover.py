from __future__ import annotations

from pathlib import Path
import os
import subprocess
import tempfile

# Values encoded by the site's public browser crypto worker.
_MEDIA_KEY = bytes(int(x) for x in "102_53_100_57_54_53_100_102_55_53_51_51_54_50_55_48".split('_'))
_MEDIA_IV = bytes(int(x) for x in "57_55_98_54_48_51_57_52_97_98_99_50_102_98_101_49".split('_'))


def _strip_no_padding_fill(data: bytes) -> bytes:
    # The site's CryptoJS NoPadding payloads are block-aligned with repeated
    # LF/NUL bytes after the real image end. Cut at the format's true terminator.
    if data.startswith(b'\xff\xd8\xff'):
        end = data.rfind(b'\xff\xd9')
        return data[:end + 2] if end >= 0 else data
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        end = data.rfind(b'IEND\xaeB`\x82')
        return data[:end + 8] if end >= 0 else data
    return data.rstrip(b'\x00\n\r')


def image_kind(data: bytes) -> str:
    data = _strip_no_padding_fill(data)
    if data.startswith(b'\xff\xd8\xff') and data.endswith(b'\xff\xd9'):
        return 'jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n') and data.endswith(b'IEND\xaeB`\x82'):
        return 'png'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'webp'
    return ''


def decrypt_cover(ciphertext: bytes) -> bytes:
    if not ciphertext or len(ciphertext) % 16:
        raise ValueError('encrypted cover is empty or not AES block aligned')
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / 'cipher.bin'
        dst = Path(td) / 'plain.bin'
        src.write_bytes(ciphertext)
        proc = subprocess.run([
            'openssl', 'enc', '-d', '-aes-128-cbc', '-nopad',
            '-K', _MEDIA_KEY.hex(), '-iv', _MEDIA_IV.hex(),
            '-in', str(src), '-out', str(dst),
        ], capture_output=True, text=True)
        if proc.returncode:
            raise RuntimeError('cover AES decryption failed')
        plain = _strip_no_padding_fill(dst.read_bytes())
    if not image_kind(plain):
        raise ValueError('decrypted cover is not a supported image')
    return plain


def write_decrypted_cover(ciphertext: bytes, destination: Path) -> str:
    plain = decrypt_cover(ciphertext)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + '.part')
    partial.write_bytes(plain)
    os.replace(partial, destination)
    return image_kind(plain)


def write_cover(data: bytes, destination: Path) -> str:
    # Cover sources differ: the /api/media/covers payload is AES encrypted,
    # while the og:image fallback is usually a plain image.
    if image_kind(data):
        plain = data
    else:
        plain = decrypt_cover(data)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + '.part')
    partial.write_bytes(plain)
    os.replace(partial, destination)
    return image_kind(plain)


def valid_cover(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 4096:
        return False
    try:
        return bool(image_kind(path.read_bytes()))
    except OSError:
        return False
