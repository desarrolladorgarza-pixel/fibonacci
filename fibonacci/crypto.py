"""
FIBONACCI — Cifrado.

La v0.4.0 usaba PBKDF2 + keystream SHA-256 + HMAC, y lo documenté como
"ofuscación honesta, no AES". Esto lo corrige: AES-256-GCM real cuando hay
`cryptography` instalado, con degradación explícita cuando no.

## La decisión de diseño

Fibonacci no tiene dependencias. Añadir `cryptography` como requisito
obligatorio rompería la instalación en Termux, en aarch64 sin compilador y en
cualquier entorno mínimo — que son exactamente los sitios donde corre un agente
personal soberano.

La salida: **AES-GCM si está disponible, y si no, el modo anterior pero
diciéndolo en voz alta**. El formato del archivo declara qué se usó, y
`fib sync export` avisa cuando cae al modo débil. Lo que no hago es llamar
"cifrado" a las dos cosas por igual.

    pip install fibonacci-agent[crypto]

## Sobre el modo de respaldo

PBKDF2 con 600k iteraciones + keystream SHA-256 en modo contador + HMAC-SHA256.
No es un cifrado auditado, pero tampoco es trivial: la clave no se puede
derivar del texto cifrado y el HMAC detecta manipulación. Sirve para mover un
archivo por un canal semi-confiable. Para un canal hostil, instala el extra o
cifra con `age`/`gpg` por fuera.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets

log = logging.getLogger("fibonacci.crypto")

PBKDF2_ROUNDS = 600_000          # OWASP 2023 para SHA-256
FORMAT_AES = "fib-aes-gcm-1"
FORMAT_FALLBACK = "fib-hmac-ctr-2"


def aes_available() -> bool:
    """
    ¿Hay AES-256-GCM utilizable?

    No basta con capturar `ImportError`. `cryptography` trae bindings en Rust,
    y una instalación rota —lo típico al mezclar el paquete del sistema con
    otra versión de Python— no falla con `ImportError` sino con un pánico de
    pyo3, que hereda de `BaseException` y por tanto atraviesa cualquier
    `except Exception`. El resultado era que `fib doctor` moría con una traza
    de Rust en la cara del usuario, y `doctor` es literalmente el primer
    comando que el README manda ejecutar.

    Es una dependencia **opcional**: que esté rota debe degradar al cifrado de
    respaldo, exactamente igual que si no estuviera.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        return True
    except ImportError:
        return False
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001
        log.warning("'cryptography' está instalado pero no se puede usar (%s: %s). "
                    "Se usará el cifrado de respaldo.", type(exc).__name__, exc)
        return False


def _derive(passphrase: str, salt: bytes, length: int = 32) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt,
                               PBKDF2_ROUNDS, dklen=length)


# ---------------------------------------------------------------------------

def encrypt(plaintext: str, passphrase: str, *,
            force_fallback: bool = False) -> str:
    """Cifra a un JSON autodescriptivo. Declara siempre qué algoritmo usó."""
    if not passphrase:
        raise ValueError("se requiere una contraseña")

    salt = secrets.token_bytes(16)
    key = _derive(passphrase, salt)
    data = plaintext.encode("utf-8")

    if aes_available() and not force_fallback:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = secrets.token_bytes(12)
        blob = AESGCM(key).encrypt(nonce, data, None)
        return json.dumps({
            "enc": FORMAT_AES,
            "kdf": f"pbkdf2-sha256-{PBKDF2_ROUNDS}",
            "salt": base64.b64encode(salt).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "data": base64.b64encode(blob).decode(),
        })

    log.warning("cryptography no está disponible: usando cifrado de respaldo. "
                "Instala fibonacci-agent[crypto] para AES-256-GCM.")
    cipher = _xor_stream(data, key)
    mac = hmac.new(key, cipher, hashlib.sha256).digest()
    return json.dumps({
        "enc": FORMAT_FALLBACK,
        "kdf": f"pbkdf2-sha256-{PBKDF2_ROUNDS}",
        "salt": base64.b64encode(salt).decode(),
        "mac": base64.b64encode(mac).decode(),
        "data": base64.b64encode(cipher).decode(),
        "_aviso": ("cifrado de respaldo, no AES. Instala "
                   "fibonacci-agent[crypto] para cifrado auditado."),
    })


def decrypt(blob: str, passphrase: str) -> str:
    d = json.loads(blob)
    fmt = d.get("enc", "")
    salt = base64.b64decode(d["salt"])
    key = _derive(passphrase, salt)
    data = base64.b64decode(d["data"])

    if fmt == FORMAT_AES:
        if not aes_available():
            raise ValueError(
                "este paquete usa AES-GCM y falta `cryptography`. "
                "Instala: pip install fibonacci-agent[crypto]")
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        try:
            plain = AESGCM(key).decrypt(base64.b64decode(d["nonce"]), data, None)
        except InvalidTag:
            raise ValueError("contraseña incorrecta o archivo manipulado")
        return plain.decode("utf-8")

    if fmt in (FORMAT_FALLBACK, "fib-xor-1"):
        expected = base64.b64decode(d["mac"])
        actual = hmac.new(key, data, hashlib.sha256).digest()
        if fmt == "fib-xor-1":       # formato 0.4.0: MAC truncado a 16 bytes
            actual = hashlib.sha256(key + data).digest()[:16]
        if not hmac.compare_digest(actual, expected):
            raise ValueError("contraseña incorrecta o archivo manipulado")
        return _xor_stream(data, key).decode("utf-8")

    raise ValueError(f"formato de cifrado desconocido: {fmt}")


def _xor_stream(data: bytes, key: bytes) -> bytes:
    """Keystream SHA-256 en modo contador. Simétrico: cifra y descifra igual."""
    out = bytearray()
    counter = 0
    while len(out) < len(data):
        out += hashlib.sha256(key + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(a ^ b for a, b in zip(data, out))


def describe() -> str:
    return ("AES-256-GCM" if aes_available()
            else "respaldo HMAC-CTR (instala [crypto] para AES)")


def is_encrypted(text: str) -> bool:
    try:
        return json.loads(text.lstrip()).get("enc", "").startswith("fib-")
    except (json.JSONDecodeError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Cifrado en reposo para los snapshots
# ---------------------------------------------------------------------------

def encrypt_file(path, passphrase: str) -> bool:
    """Los snapshots del journal pueden contener datos sensibles y viven 14
    días en disco. Esto los cifra en reposo si el usuario lo pide."""
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return False
    blob = encrypt(base64.b64encode(p.read_bytes()).decode(), passphrase)
    p.with_suffix(p.suffix + ".enc").write_text(blob, encoding="utf-8")
    p.unlink()
    return True


def decrypt_file(path, passphrase: str) -> bool:
    from pathlib import Path

    p = Path(path)
    if not p.exists() or not str(p).endswith(".enc"):
        return False
    raw = base64.b64decode(decrypt(p.read_text(encoding="utf-8"), passphrase))
    original = Path(str(p)[:-4])
    original.write_bytes(raw)
    p.unlink()
    return True
