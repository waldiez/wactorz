"""The private CA and broker certificate an install issues for brokers it provides.

A node that connects over TLS has to trust the broker's certificate, and asking
whoever installs Wactorz to run a certificate authority would stop most of them at
the first step. So an install mints its own CA, once, and issues the broker's
certificate from it. Nodes are given the CA, not the broker certificate, so the
broker certificate can be issued again -- it nears expiry, or the broker gains an
address -- without redeploying a node.

That CA signs nothing but this install's broker, which is why a client trusting it
skips the hostname check (see :mod:`.mqtt_tls`). Its key never leaves the state
directory.

The CA is never replaced without being asked: every deployed node holds it, and a
new one would be refused by all of them. A broker certificate that is missing,
unreadable, near expiry, or does not name an address the broker is reached by is
simply issued again.
"""

from __future__ import annotations

import datetime
import ipaddress
import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .mqtt_tls import CA_FILE, TLS_DIRNAME
from .paths import ensure_state_dir

logger = logging.getLogger(__name__)

#: The CA's private key, beside its certificate.
CA_KEY_FILE = "ca.key"

#: The broker's certificate and key, as a broker loads them.
BROKER_CERT_FILE = "broker.crt"
BROKER_KEY_FILE = "broker.key"

#: How long the CA is valid. Long, because replacing it means redeploying every node.
CA_LIFETIME = datetime.timedelta(days=3650)

#: How long a broker certificate is valid. Reissuing one costs nothing.
BROKER_LIFETIME = datetime.timedelta(days=825)

#: How close to expiry a broker certificate is issued again.
RENEW_BEFORE = datetime.timedelta(days=30)

#: Names every broker certificate carries: loopback, and the host names a broker
#: has inside docker compose and Home Assistant.
STANDARD_NAMES = ("localhost", "127.0.0.1", "::1", "mosquitto", "core-mosquitto")

#: What a DNS name in a certificate may be made of. Anything else is skipped.
_DNS_NAME = re.compile(r"^[A-Za-z0-9_*.-]+$")

_CA_NAME = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Wactorz MQTT CA")])
_BROKER_NAME = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "wactorz-mqtt")])


class UnreadableCAError(ValueError):
    """The CA in a directory is incomplete or cannot be read."""

    def __init__(self, directory: Path) -> None:
        super().__init__(
            f"The MQTT CA in {directory} is incomplete or unreadable. Restore it from a "
            f"backup, or delete {CA_FILE} and {CA_KEY_FILE} there and deploy every node "
            "again: a new CA is trusted by no node deployed with the old one."
        )


@dataclass(frozen=True)
class BrokerFiles:
    """Where the CA and the broker's certificate and key are, and whether it was just issued."""

    ca: Path
    cert: Path
    key: Path
    issued: bool


def default_directory() -> Path:
    """This install's TLS directory, created if it is not there."""
    return Path(ensure_state_dir()) / TLS_DIRNAME


def ensure(
    names: Iterable[str] = (),
    directory: Path | None = None,
    now: datetime.datetime | None = None,
) -> BrokerFiles:
    """Make sure a CA and a current broker certificate naming ``names`` exist.

    Mints the CA the first time. Issues the broker certificate when there is none,
    or the one there will not do; otherwise leaves it as it is.
    """
    target = directory if directory is not None else default_directory()
    target.mkdir(parents=True, exist_ok=True)
    moment = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
    ca_cert, ca_key = _ca(target, moment)
    wanted = _wanted_names(names)
    cert_path, key_path = target / BROKER_CERT_FILE, target / BROKER_KEY_FILE
    reason = _why_issue(cert_path, key_path, ca_cert, wanted, moment)
    if reason:
        logger.info("[mqtt-tls] Issuing the broker certificate: %s", reason)
        # Keeping the names it already carries, so a certificate stays good for
        # whoever issued it last. Two things issue it -- this server, and the
        # one-shot step inside the compose stack -- and each knows its own
        # addresses, so replacing the names would have them reissue in turn for
        # ever, restarting the broker each time.
        _issue(cert_path, key_path, ca_cert, ca_key, _with_existing(cert_path, wanted), moment)
    return BrokerFiles(ca=target / CA_FILE, cert=cert_path, key=key_path, issued=bool(reason))


def export(
    files: BrokerFiles, directory: Path, *, cert_name: str, key_name: str, ca_name: str = ""
) -> None:
    """Copy the broker's files where a broker reads them.

    The certificate is written with the CA after it, as a chain, which is the form
    a broker asking for "a certificate, including its chain" expects. The key is
    written readable by its owner only. ``ca_name`` also writes the CA on its own.
    """
    directory.mkdir(parents=True, exist_ok=True)
    _write_public(directory / cert_name, files.cert.read_bytes() + files.ca.read_bytes())
    _write_private(directory / key_name, files.key.read_bytes())
    if ca_name:
        _write_public(directory / ca_name, files.ca.read_bytes())


def _ca(
    directory: Path, now: datetime.datetime
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    cert_path, key_path = directory / CA_FILE, directory / CA_KEY_FILE
    if not cert_path.exists() and not key_path.exists():
        return _mint_ca(cert_path, key_path, now)
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    except (OSError, ValueError) as exc:
        raise UnreadableCAError(directory) from exc
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise UnreadableCAError(directory)
    return cert, key


def _mint_ca(
    cert_path: Path, key_path: Path, now: datetime.datetime
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(_CA_NAME)
        .issuer_name(_CA_NAME)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + CA_LIFETIME)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_private(key_path, _private_pem(key))
    _write_public(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    logger.info("[mqtt-tls] Minted this install's MQTT CA in %s", cert_path.parent)
    return cert, key


def _why_issue(
    cert_path: Path,
    key_path: Path,
    ca_cert: x509.Certificate,
    wanted: list[str],
    now: datetime.datetime,
) -> str:
    """Why the broker certificate has to be issued, or ``""`` when it is fine as it is."""
    if not cert_path.exists() or not key_path.exists():
        return "there is none"
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (OSError, ValueError):
        return "the one there is unreadable"
    if not _signed_by(cert, ca_cert):
        return "the one there was not issued by this install's CA"
    if cert.not_valid_after_utc - RENEW_BEFORE <= now:
        return "the one there expires soon"
    missing = sorted(set(wanted) - _names_in(cert))
    if missing:
        return f"the one there does not name {', '.join(missing)}"
    return ""


def _issue(
    cert_path: Path,
    key_path: Path,
    ca_cert: x509.Certificate,
    ca_key: ec.EllipticCurvePrivateKey,
    names: list[str],
    now: datetime.datetime,
) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    expires = min(now + BROKER_LIFETIME, ca_cert.not_valid_after_utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_BROKER_NAME)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(expires)
        .add_extension(
            x509.SubjectAlternativeName([_general_name(n) for n in names]), critical=False
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    _write_private(key_path, _private_pem(key))
    _write_public(cert_path, cert.public_bytes(serialization.Encoding.PEM))


def _with_existing(cert_path: Path, wanted: list[str]) -> list[str]:
    """``wanted`` plus every name the certificate at ``cert_path`` already names."""
    try:
        current = _names_in(x509.load_pem_x509_certificate(cert_path.read_bytes()))
    except (OSError, ValueError):
        return wanted
    return _wanted_names([*wanted, *current])


def _signed_by(cert: x509.Certificate, ca_cert: x509.Certificate) -> bool:
    public = ca_cert.public_key()
    algorithm = cert.signature_hash_algorithm
    if cert.issuer != ca_cert.subject or not isinstance(public, ec.EllipticCurvePublicKey):
        return False
    if algorithm is None:
        return False
    try:
        public.verify(cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(algorithm))
    except InvalidSignature:
        return False
    return True


def _names_in(cert: x509.Certificate) -> set[str]:
    try:
        extension = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return set()
    dns = extension.value.get_values_for_type(x509.DNSName)
    ips = extension.value.get_values_for_type(x509.IPAddress)
    return {name.lower() for name in dns} | {str(ip) for ip in ips}


def _wanted_names(names: Iterable[str]) -> list[str]:
    """Every name the certificate should carry, normalised, with unusable ones skipped."""
    wanted: set[str] = set()
    for raw in (*STANDARD_NAMES, *names):
        name = (raw or "").strip()
        if not name:
            continue
        try:
            wanted.add(str(ipaddress.ip_address(name)))
            continue
        except ValueError:
            pass
        if _DNS_NAME.match(name):
            wanted.add(name.lower())
        else:
            logger.warning(
                "[mqtt-tls] %r is not a host name; leaving it out of the certificate", name
            )
    return sorted(wanted)


def _general_name(name: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ipaddress.ip_address(name))
    except ValueError:
        return x509.DNSName(name)


def _private_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _write_private(path: Path, data: bytes) -> None:
    """Write ``data`` readable by its owner only, replacing ``path`` in one step."""
    staging = path.with_name(f".{path.name}.tmp")
    staging.unlink(missing_ok=True)
    fd = os.open(staging, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    staging.replace(path)


def _write_public(path: Path, data: bytes) -> None:
    staging = path.with_name(f".{path.name}.tmp")
    staging.write_bytes(data)
    staging.replace(path)
