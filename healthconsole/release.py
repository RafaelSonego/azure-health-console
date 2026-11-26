"""Is a downloaded release the one the release pipeline signed? `SHA256SUMS.txt` lists each file's hash and
`SHA256SUMS.txt.sig` is its ECDSA P-256 signature, made with a Key Vault key that never leaves the vault. The public
half ships inside this package, so an upgrade is checked against the key of the version already installed."""
import hashlib
import re
from pathlib import Path

PUBLIC_KEY = Path(__file__).with_name("release-signing.pem")
SUMS, SIGNATURE = "SHA256SUMS.txt", "SHA256SUMS.txt.sig"
_LINE = re.compile(r"^([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+-]*)$")
_RELEASE_FILE = re.compile(r"\.(whl|tar\.gz)$|^requirements-release\.txt$")


class VerifyError(Exception):
    """The release isn't the signed one (or can't be checked); the message says what to do."""


def _key(pem):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    try:
        key = serialization.load_pem_public_key(pem)
    except ValueError as error:
        raise VerifyError("This version has no usable release key, so it can't check a release. Check it with openssl "
                          "as the README's *Check what you install* shows.") from error
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
        raise VerifyError("The release key in this version isn't an EC P-256 key.")
    return key


def _signature(raw):
    """DER as openssl writes it, or the 64-byte r‖s that Key Vault returns."""
    if len(raw) == 64:
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        return encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    return raw


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(folder, key_pem=None):
    """[(file, "ok" | "not downloaded")] for each file SHA256SUMS.txt lists; VerifyError on a bad signature, a file
    whose hash differs, or a release file in the folder that the list doesn't name."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    folder = Path(folder)
    try:
        sums, signature = (folder / SUMS).read_bytes(), (folder / SIGNATURE).read_bytes()
    except OSError as error:
        raise VerifyError(f"Download {SUMS} and {SIGNATURE} from the release into {folder} first.") from error
    try:
        _key(key_pem or PUBLIC_KEY.read_bytes()).verify(_signature(signature), sums, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as error:
        raise VerifyError(f"{SUMS} wasn't signed by the release pipeline: don't install anything from {folder}.") from error
    listed = {}
    for line in sums.decode("utf-8", "replace").splitlines():
        match = _LINE.match(line)
        if not match:
            raise VerifyError(f"{SUMS} has a line that isn't '<sha256>  <file name>': {line[:80]!r}")
        listed[match.group(2)] = match.group(1)
    results = []
    for name, expected in listed.items():
        path = folder / name
        if not path.is_file():
            results.append((name, "not downloaded"))
            continue
        if _sha256(path) != expected:
            raise VerifyError(f"{name} isn't the file that was released (its SHA-256 differs): delete it.")
        results.append((name, "ok"))
    strays = sorted(p.name for p in folder.iterdir() if p.is_file() and _RELEASE_FILE.search(p.name) and p.name not in listed)
    if strays:
        raise VerifyError(f"{', '.join(strays)} isn't in the signed {SUMS}: don't install it.")
    if not any(status == "ok" for _, status in results):
        raise VerifyError(f"None of the files {SUMS} lists are in {folder}.")
    return results
