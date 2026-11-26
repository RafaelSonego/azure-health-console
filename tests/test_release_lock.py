import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements-release.txt"


def locked():
    """{name: (version, hashes)} from the release lock."""
    found = {}
    for entry in re.split(r"\n(?=\S)", LOCK.read_text()):
        pin = re.match(r"([A-Za-z0-9._-]+)==([^\s;\\]+)", entry)
        if pin:
            found[re.sub(r"[-_.]+", "-", pin.group(1).lower())] = (pin.group(2), re.findall(r"--hash=sha256:[0-9a-f]{64}", entry))
    return found


def test_every_dependency_is_bounded_and_locked_with_hashes_inside_its_bounds():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    pins = locked()
    for spec in project["project"]["dependencies"] + project["build-system"]["requires"]:
        requirement = Requirement(spec)
        assert any(s.operator in ("<", "<=", "==", "~=") for s in requirement.specifier), f"{spec} has no upper bound"
        if spec in project["build-system"]["requires"]:
            continue
        version, hashes = pins[re.sub(r"[-_.]+", "-", requirement.name.lower())]
        assert hashes and requirement.specifier.contains(version), f"{spec}: lock has {version}"
    assert all(hashes for _, hashes in pins.values())  # --require-hashes refuses the whole file otherwise


def test_the_lock_ships_in_the_sdist_and_homebrew_installs_from_it_by_hash():
    assert "include requirements-release.txt" in (ROOT / "MANIFEST.in").read_text()
    formula = (ROOT / "packaging" / "homebrew" / "azure-health-console.rb.tmpl").read_text()
    assert '"--require-hashes", "-r", buildpath/"requirements-release.txt"' in formula
    assert '"--no-deps", buildpath' in formula
