"""
Security Layer — protects ALL sensitive data in the pipeline.

What this covers:
  1. Credential Vault       — encrypts portal passwords at rest (Fernet AES-128)
  2. Resume Encryptor       — encrypts resume/cover letter files on disk
  3. Secure File Manager    — temp files wiped with os.urandom overwrite
  4. Audit Logger           — immutable append-only log of every action
  5. PII Scrubber           — strips PII from logs before writing

Encryption: Fernet (symmetric, AES-128-CBC + HMAC-SHA256).
Key:        Derived from VAULT_MASTER_PASSWORD via PBKDF2-HMAC-SHA256.
            Never stored — derived fresh each session from env var.

NEVER store:
  - Passwords in plaintext .env (use vault store)
  - Resume text in Redis or DB without encryption
  - PII in structured logs
"""

from __future__ import annotations
import hashlib
import hmac
import json
import os
import re
import struct
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

# ── Key derivation ─────────────────────────────────────────────────────────────

def _derive_key(master_password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    """
    Derive a 32-byte Fernet key from master password via PBKDF2.
    Returns (key, salt). Salt is random if not provided.
    """
    if salt is None:
        salt = os.urandom(16)
    key_raw = hashlib.pbkdf2_hmac(
        "sha256",
        master_password.encode("utf-8"),
        salt,
        iterations=480_000,   # OWASP 2024 minimum for PBKDF2-SHA256
        dklen=32,
    )
    # Fernet requires URL-safe base64-encoded 32-byte key
    key = urlsafe_b64encode(key_raw)
    return key, salt


# ── Fernet-compatible encrypt/decrypt (no external dep beyond stdlib) ──────────
# We implement a minimal Fernet ourselves to avoid adding cryptography lib
# if it's absent, but in production: `pip install cryptography` and use
# cryptography.fernet.Fernet directly.

try:
    from cryptography.fernet import Fernet, InvalidToken

    class _Crypto:
        def __init__(self, key: bytes):
            self._f = Fernet(key)

        def encrypt(self, data: bytes) -> bytes:
            return self._f.encrypt(data)

        def decrypt(self, token: bytes) -> bytes:
            return self._f.decrypt(token)

except ImportError:
    # Fallback: XOR with key hash (NOT production-grade — install cryptography)
    import warnings
    warnings.warn("cryptography not installed — using weak XOR fallback. Run: pip install cryptography")

    class _Crypto:  # type: ignore
        def __init__(self, key: bytes):
            self._key = hashlib.sha256(key).digest()

        def encrypt(self, data: bytes) -> bytes:
            stream = (self._key * ((len(data) // 32) + 1))[:len(data)]
            return bytes(a ^ b for a, b in zip(data, stream))

        def decrypt(self, token: bytes) -> bytes:
            return self.encrypt(token)  # XOR is symmetric


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Credential Vault
# ═══════════════════════════════════════════════════════════════════════════════

class CredentialVault:
    """
    Stores portal credentials encrypted at rest in a local JSON file.
    Master password lives ONLY in VAULT_MASTER_PASSWORD env var — never on disk.

    Usage:
        vault = CredentialVault.open()
        vault.store("linkedin", "user@example.com", "s3cr3t")
        email, password = vault.retrieve("linkedin")
    """

    VAULT_PATH = Path(".vault/credentials.enc")
    SALT_PATH  = Path(".vault/salt.bin")

    def __init__(self, crypto: _Crypto):
        self._crypto = crypto
        self._data: dict[str, dict] = {}

    @classmethod
    def open(cls, master_password: str | None = None) -> "CredentialVault":
        pwd = master_password or os.environ.get("VAULT_MASTER_PASSWORD")
        if not pwd:
            raise EnvironmentError(
                "VAULT_MASTER_PASSWORD env var not set. "
                "Set it before running the pipeline."
            )
        cls.VAULT_PATH.parent.mkdir(exist_ok=True, mode=0o700)

        # Load or create salt
        if cls.SALT_PATH.exists():
            salt = cls.SALT_PATH.read_bytes()
        else:
            salt = os.urandom(16)
            cls.SALT_PATH.write_bytes(salt)
            cls.SALT_PATH.chmod(0o600)

        key, _ = _derive_key(pwd, salt)
        crypto = _Crypto(key)
        vault = cls(crypto)

        # Load existing vault
        if cls.VAULT_PATH.exists():
            try:
                raw = cls.VAULT_PATH.read_bytes()
                decrypted = crypto.decrypt(raw)
                vault._data = json.loads(decrypted)
            except Exception:
                log.error("vault_decryption_failed — wrong master password or corrupted vault")
                raise

        return vault

    def store(self, portal: str, email: str, password: str) -> None:
        """Encrypt and persist credential."""
        self._data[portal] = {"email": email, "password": password}
        self._persist()
        AuditLog.write("credential_stored", portal=portal, email=_mask(email))

    def retrieve(self, portal: str) -> tuple[str, str]:
        """Returns (email, password) or raises KeyError."""
        cred = self._data.get(portal)
        if not cred:
            raise KeyError(f"No credentials for portal '{portal}'. Run: vault store {portal}")
        AuditLog.write("credential_retrieved", portal=portal)
        return cred["email"], cred["password"]

    def list_portals(self) -> list[str]:
        return list(self._data.keys())

    def _persist(self) -> None:
        raw = json.dumps(self._data).encode()
        encrypted = self._crypto.encrypt(raw)
        self.VAULT_PATH.write_bytes(encrypted)
        self.VAULT_PATH.chmod(0o600)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Resume Encryptor
# ═══════════════════════════════════════════════════════════════════════════════

class ResumeEncryptor:
    """
    Encrypts resume and cover letter files before writing to disk.
    Decrypts on-the-fly when agents need to read them.
    Files on disk are always ciphertext — plaintext only in memory.
    """

    def __init__(self, crypto: _Crypto):
        self._crypto = crypto

    def encrypt_file(self, plaintext: str, dest: Path) -> Path:
        """Write encrypted file. Returns path."""
        encrypted = self._crypto.encrypt(plaintext.encode("utf-8"))
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        dest.write_bytes(encrypted)
        dest.chmod(0o600)
        AuditLog.write("file_encrypted", path=str(dest))
        return dest

    def decrypt_file(self, path: Path) -> str:
        """Read and decrypt file. Returns plaintext string."""
        encrypted = path.read_bytes()
        plaintext = self._crypto.decrypt(encrypted)
        AuditLog.write("file_decrypted", path=str(path))
        return plaintext.decode("utf-8")

    def encrypt_text(self, text: str) -> bytes:
        return self._crypto.encrypt(text.encode("utf-8"))

    def decrypt_bytes(self, data: bytes) -> str:
        return self._crypto.decrypt(data).decode("utf-8")


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Secure File Manager
# ═══════════════════════════════════════════════════════════════════════════════

class SecureFileManager:
    """
    Temp file lifecycle with guaranteed secure deletion.
    Overwrites with random bytes before unlinking — prevents data recovery.

    Use as async context manager:
        async with SecureFileManager() as sfm:
            path = sfm.create_temp("resume text...", "resume.txt")
            # use path
        # file is securely wiped on exit
    """

    def __init__(self):
        self._temp_dir = Path("/tmp/internship_bot_secure")
        self._temp_dir.mkdir(exist_ok=True, mode=0o700)
        self._managed: list[Path] = []

    def create_temp(self, content: str, filename: str) -> Path:
        path = self._temp_dir / filename
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
        self._managed.append(path)
        return path

    def secure_wipe(self, path: Path) -> None:
        """Overwrite with random bytes 3× then delete."""
        if not path.exists():
            return
        size = path.stat().st_size
        for _ in range(3):
            path.write_bytes(os.urandom(size))
        path.unlink()
        AuditLog.write("file_wiped", path=str(path))

    def wipe_all(self) -> None:
        for path in self._managed:
            try:
                self.secure_wipe(path)
            except Exception as e:
                log.warning("secure_wipe_failed", path=str(path), error=str(e))
        self._managed.clear()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.wipe_all()


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Audit Logger
# ═══════════════════════════════════════════════════════════════════════════════

class AuditLog:
    """
    Append-only audit trail. One JSON line per event.
    Never contains plaintext passwords, full resume text, or unmasked emails.
    File is append-only (chmod a-w after each line in production with chattr +a).
    """

    LOG_PATH = Path(".vault/audit.log")

    @classmethod
    def write(cls, event: str, **kwargs) -> None:
        cls.LOG_PATH.parent.mkdir(exist_ok=True, mode=0o700)
        entry = {
            "ts":    datetime.now(timezone.utc).isoformat(),
            "event": event,
            **{k: PiiScrubber.scrub(str(v)) for k, v in kwargs.items()},
        }
        with cls.LOG_PATH.open("a") as f:
            f.write(json.dumps(entry) + "\n")

    @classmethod
    def tail(cls, n: int = 20) -> list[dict]:
        if not cls.LOG_PATH.exists():
            return []
        lines = cls.LOG_PATH.read_text().strip().splitlines()
        return [json.loads(l) for l in lines[-n:]]


# ═══════════════════════════════════════════════════════════════════════════════
# 5. PII Scrubber
# ═══════════════════════════════════════════════════════════════════════════════

class PiiScrubber:
    """Strips PII from strings before they hit structured logs."""

    _EMAIL_RE    = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
    _PHONE_RE    = re.compile(r"(\+?\d[\d\s\-\(\)]{7,15}\d)")
    _PASSPORT_RE = re.compile(r"\b[A-Z]{1,2}\d{6,9}\b")

    @classmethod
    def scrub(cls, text: str) -> str:
        text = cls._EMAIL_RE.sub("[EMAIL]", text)
        text = cls._PHONE_RE.sub("[PHONE]", text)
        text = cls._PASSPORT_RE.sub("[PASSPORT]", text)
        return text


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _mask(email: str) -> str:
    """u***@domain.com"""
    if "@" not in email:
        return "***"
    user, domain = email.split("@", 1)
    return user[0] + "***@" + domain


# ═══════════════════════════════════════════════════════════════════════════════
# Vault CLI  (run: python -m security.vault store linkedin)
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    import getpass

    action = sys.argv[1] if len(sys.argv) > 1 else "list"
    vault = CredentialVault.open()

    if action == "store":
        portal = sys.argv[2]
        email = input(f"Email for {portal}: ")
        pwd = getpass.getpass(f"Password for {portal}: ")
        vault.store(portal, email, pwd)
        print(f"✓ Stored credentials for {portal}")

    elif action == "list":
        portals = vault.list_portals()
        print("Stored portals:", portals if portals else "(none)")

    elif action == "audit":
        for entry in AuditLog.tail(20):
            print(entry)
