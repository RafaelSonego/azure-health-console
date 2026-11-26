#!/bin/sh
# Installs or upgrades azure-health-console (macOS, Linux): curl -fsSL <this file's raw URL> | sh
# A version other than the newest: AHC_VERSION=1.2.0 sh install.sh
set -eu

REPO="RafaelSonego/azure-health-console"
PACKAGE="azure-health-console"
RELEASE_KEY='-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEFP5CYATsAM/QPbKnW7oTIxXRdV0F
t3kUA3vYU1L4FJeekAG3FNFEPCaNfmNz4GFLOJr1ZCcn0n50KP/rpW6VYg==
-----END PUBLIC KEY-----'

say() { printf '%s\n' "$*"; }
stop() { printf '\n%s\n' "$*" >&2; exit 1; }
has() { command -v "$1" >/dev/null 2>&1; }

fetch() {
    if has curl; then curl -fsSL -o "$2" "$1"
    elif has wget; then wget -q -O "$2" "$1"
    else stop "Neither curl nor wget is installed: install one, then run this again."
    fi
}

sha256() { openssl dgst -sha256 -r "$1" | cut -d' ' -f1; }

find_python() {
    for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
        if has "$candidate" && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

how_to_get() {  # $1: python or pipx
    if [ "$(uname -s)" = Darwin ]; then say "brew install $1   (no Homebrew? https://brew.sh)"
    elif has apt-get; then [ "$1" = python ] && say "sudo apt install python3.11 python3.11-venv" || say "sudo apt install pipx"
    elif has dnf; then [ "$1" = python ] && say "sudo dnf install python3.11" || say "sudo dnf install pipx"
    else say "see https://pipx.pypa.io/stable/installation/"
    fi
}

verify() {  # $1: folder with the release files
    if has "$PACKAGE" && "$PACKAGE" verify --help >/dev/null 2>&1; then
        say "Checking the release with the key inside your installed $("$PACKAGE" --version 2>/dev/null)…"
        "$PACKAGE" verify "$1" || stop "Not verified: nothing was installed. Please open an issue: https://github.com/$REPO/issues"
        return 0
    fi
    say "Checking the release's signature…"
    printf '%s\n' "$RELEASE_KEY" > "$1/../release-signing.pem"
    openssl dgst -sha256 -verify "$1/../release-signing.pem" -signature "$1/SHA256SUMS.txt.sig" "$1/SHA256SUMS.txt" >/dev/null \
        || stop "SHA256SUMS.txt isn't signed by the release pipeline: nothing was installed."
    listed=$(sed -n 's/^\([0-9a-f]\{64\}\)  constraints\.txt$/\1/p' "$1/SHA256SUMS.txt")
    [ -n "$listed" ] && [ "$listed" = "$(sha256 "$1/constraints.txt")" ] \
        || stop "constraints.txt isn't the released file: nothing was installed."
}

main() {
    if [ "$(uname -s)" = Darwin ] && has brew; then
        if brew list --formula "$PACKAGE" >/dev/null 2>&1; then
            say "Upgrading with Homebrew…"; brew upgrade "$PACKAGE"
        else
            say "Installing with Homebrew…"; brew install "rafaelsonego/tools/$PACKAGE"
        fi
        finish "$(brew list --versions "$PACKAGE" | cut -d' ' -f2)"
        return 0
    fi

    has openssl || stop "openssl is needed to check the release. Install it, then run this again."
    python=$(find_python) || stop "Python 3.11 or later is needed. Install it with:  $(how_to_get python)"
    if has pipx; then pipx=pipx
    elif "$python" -m pipx --version >/dev/null 2>&1; then pipx="$python -m pipx"
    else stop "pipx is needed. Install it with:  $(how_to_get pipx)"
    fi

    work=$(mktemp -d "${TMPDIR:-/tmp}/$PACKAGE.XXXXXX")
    trap 'rm -rf "$work"' EXIT
    mkdir "$work/release"
    if [ -n "${AHC_VERSION:-}" ]; then base="https://github.com/$REPO/releases/download/v$AHC_VERSION"
    else base="https://github.com/$REPO/releases/latest/download"
    fi
    say "Downloading ${AHC_VERSION:-the newest} release's signed file list…"
    for file in SHA256SUMS.txt SHA256SUMS.txt.sig constraints.txt; do
        fetch "$base/$file" "$work/release/$file" || stop "Couldn't download $base/$file."
    done
    verify "$work/release"
    version=$(sed -n "s/^$PACKAGE==\([0-9][0-9A-Za-z.+-]*\).*/\1/p" "$work/release/constraints.txt")
    [ -n "$version" ] || stop "constraints.txt doesn't name a version of $PACKAGE: nothing was installed."
    say "Verified: $version is the signed release."

    installed=$("$PACKAGE" --version 2>/dev/null | cut -d' ' -f2 || true)
    if [ "$installed" = "$version" ]; then
        say "$version is already installed."
        finish "$version"
        return 0
    fi
    say "Installing $version with pipx (pip checks every file against the signed list)…"
    cd "$work/release"
    $pipx upgrade-shared >/dev/null 2>&1 || true
    $pipx install --force --python "$python" "$PACKAGE==$version" --pip-args="--require-hashes -c constraints.txt"
    cd - >/dev/null
    has "$PACKAGE" || $pipx ensurepath >/dev/null 2>&1 || true
    finish "$version"
}

finish() {
    say ""
    say "azure-health-console $1 is installed."
    has az || say "It signs in through the Azure CLI, which isn't installed: https://learn.microsoft.com/cli/azure/install-azure-cli"
    if has "$PACKAGE"; then say "Start it with:  $PACKAGE"
    else say "Open a new terminal (so it's on your PATH), then start it with:  $PACKAGE"
    fi
    if "$PACKAGE" upgrade --help >/dev/null 2>&1; then say "Upgrade later with:  $PACKAGE upgrade   (or run this line again)"
    else say "Upgrade later by running this line again."
    fi
}

main "$@"
