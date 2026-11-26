import base64
import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parents[1]
SH = (ROOT / "install.sh").read_text()
PS1 = (ROOT / "install.ps1").read_text()
KEY = serialization.load_pem_public_key((ROOT / "healthconsole" / "release-signing.pem").read_bytes())


def test_both_scripts_carry_the_package_s_release_key():
    embedded = re.search(r"RELEASE_KEY='(-----BEGIN PUBLIC KEY-----.*?-----END PUBLIC KEY-----)'", SH, re.S).group(1)
    assert serialization.load_pem_public_key(embedded.encode()).public_numbers() == KEY.public_numbers()
    x = re.search(r'\$KeyX = "([^"]+)"', PS1).group(1)
    y = re.search(r'\$KeyY = "([^"]+)"', PS1).group(1)
    numbers = KEY.public_numbers()
    assert (base64.b64decode(x), base64.b64decode(y)) == (numbers.x.to_bytes(32, "big"), numbers.y.to_bytes(32, "big"))


def test_both_scripts_install_only_by_hash_from_the_checked_constraints():
    for script in (SH, PS1):
        installs = [line for line in script.splitlines() if re.search(r"pipx\b.*install --force", line, re.I)]
        assert len(installs) == 1 and "--pip-args=--require-hashes -c constraints.txt" in installs[0].replace('"', "")
    assert SH.index('verify "$work/release"') < SH.index("install --force")
    assert PS1.index("Test-Release $release") < PS1.index("install --force")


def test_a_cut_off_download_runs_nothing():
    """curl | sh and irm | iex run what arrived: the work happens only in the last line (sh) or one block (ps1)."""
    assert SH.rstrip().splitlines()[-1] == 'main "$@"'
    code = [line for line in PS1.splitlines() if line.strip() and not line.startswith("#")]
    assert code[0] == "& {" and code[-1] == "}"
    assert not re.search(r"^\s*exit\b", PS1, re.M)  # exit inside iex would close the user's PowerShell window


def test_install_sh_keeps_lf_and_install_ps1_is_ascii_for_windows_powershell_5():
    assert "install.sh text eol=lf" in (ROOT / ".gitattributes").read_text()
    assert PS1.isascii()  # 5.1 reads a saved script without a BOM as the ANSI code page


def sign_release(folder, constraints=b"azure-health-console==9.9.9 \\\n    --hash=sha256:" + b"1" * 64 + b"\n"):
    key = ec.generate_private_key(ec.SECP256R1())
    folder.mkdir()
    (folder / "constraints.txt").write_bytes(constraints)
    sums = f"{hashlib.sha256(constraints).hexdigest()}  constraints.txt\n".encode()
    (folder / "SHA256SUMS.txt").write_bytes(sums)
    (folder / "SHA256SUMS.txt.sig").write_bytes(key.sign(sums, ec.ECDSA(hashes.SHA256())))
    return key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def sh_verify(tmp_path, folder, pem):
    library = tmp_path / "lib.sh"
    library.write_text(SH.rstrip().rsplit("\n", 1)[0] + "\n")  # everything but the main call
    script = f'. "{library}"; PACKAGE=ahc-not-installed; RELEASE_KEY="$(cat "{tmp_path / "key.pem"}")"; verify "{folder}"'
    (tmp_path / "key.pem").write_bytes(pem)
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True)


@pytest.mark.skipif(not (shutil.which("sh") and shutil.which("openssl")), reason="needs sh and openssl")
def test_install_sh_accepts_the_signed_release_and_refuses_a_changed_one(tmp_path):
    folder = tmp_path / "release"
    pem = sign_release(folder)
    assert sh_verify(tmp_path, folder, pem).returncode == 0
    (folder / "constraints.txt").write_bytes(b"evil")
    assert "constraints.txt isn't the released file" in sh_verify(tmp_path, folder, pem).stderr
    other = tmp_path / "other"
    sign_release(other)
    assert "isn't signed by the release pipeline" in sh_verify(tmp_path, other, pem).stderr
