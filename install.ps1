# Installs or upgrades azure-health-console (Windows PowerShell 5.1 or PowerShell 7): irm <this file's raw URL> | iex
# A version other than the newest: $env:AHC_VERSION = "1.2.0" first.
& {
    $ErrorActionPreference = "Continue"   # Stop would turn a native command's stderr into an error on PowerShell 5.1
    $ProgressPreference = "SilentlyContinue"
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $Repo = "RafaelSonego/azure-health-console"
    $Package = "azure-health-console"
    $KeyX = "FP5CYATsAM/QPbKnW7oTIxXRdV0Ft3kUA3vYU1L4FJc="   # the release key (healthconsole/release-signing.pem)
    $KeyY = "npABtxTRRDwmjX5jc+BhSzia9WQnJ9J+dCj/66VulWI="

    function Has($Name) { [bool](Get-Command $Name -ErrorAction SilentlyContinue) }

    function Find-Python {
        $tries = @()
        if (Has "py") { foreach ($v in "3.14", "3.13", "3.12", "3.11") { $tries += , @("py", "-$v") } }
        $tries += , @("python")
        foreach ($try in $tries) {
            if (-not (Has $try[0])) { continue }
            $rest = @($try | Select-Object -Skip 1)
            $found = & $try[0] @rest -c "import sys; print(sys.executable) if sys.version_info >= (3, 11) else sys.exit(1)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $found) { return ([string]($found | Select-Object -Last 1)).Trim() }
        }
        return $null
    }

    function Invoke-Pipx {
        $all = @($PipxCommand | Select-Object -Skip 1) + $args
        & $PipxCommand[0] @all
    }

    function ConvertTo-P1363([byte[]]$Der) {
        # openssl's DER signature, SEQUENCE { INTEGER r, INTEGER s }, as the r||s .NET verifies
        if ($Der.Length -eq 64) { return , $Der }
        if ($Der[0] -ne 0x30) { throw "SHA256SUMS.txt.sig isn't a signature." }
        $i = 2
        if ($Der[1] -band 0x80) { $i = 2 + ($Der[1] -band 0x7f) }
        $out = New-Object byte[] 64
        foreach ($half in 0, 1) {
            if ($Der[$i] -ne 0x02) { throw "SHA256SUMS.txt.sig isn't a signature." }
            $length = $Der[$i + 1]
            $start = $i + 2
            [byte[]]$int = $Der[$start..($start + $length - 1)]
            while ($int.Length -gt 32 -and $int[0] -eq 0) { [byte[]]$int = $int[1..($int.Length - 1)] }
            if ($int.Length -gt 32) { throw "SHA256SUMS.txt.sig isn't a P-256 signature." }
            [Array]::Copy($int, 0, $out, $half * 32 + (32 - $int.Length), $int.Length)
            $i = $start + $length
        }
        return , $out
    }

    function Test-Release($Folder) {
        $point = New-Object System.Security.Cryptography.ECPoint
        $point.X = [Convert]::FromBase64String($KeyX)
        $point.Y = [Convert]::FromBase64String($KeyY)
        $parameters = New-Object System.Security.Cryptography.ECParameters
        $parameters.Curve = [System.Security.Cryptography.ECCurve+NamedCurves]::nistP256
        $parameters.Q = $point
        $key = [System.Security.Cryptography.ECDsa]::Create($parameters)
        $sums = [IO.File]::ReadAllBytes((Join-Path $Folder "SHA256SUMS.txt"))
        $signature = ConvertTo-P1363 ([IO.File]::ReadAllBytes((Join-Path $Folder "SHA256SUMS.txt.sig")))
        if (-not $key.VerifyData($sums, $signature, [System.Security.Cryptography.HashAlgorithmName]::SHA256)) {
            throw "SHA256SUMS.txt isn't signed by the release pipeline: nothing was installed."
        }
        $line = Select-String -Path (Join-Path $Folder "SHA256SUMS.txt") -Pattern '^([0-9a-f]{64})  constraints\.txt$'
        $actual = (Get-FileHash -Algorithm SHA256 (Join-Path $Folder "constraints.txt")).Hash.ToLower()
        if (-not $line -or $line.Matches[0].Groups[1].Value -ne $actual) {
            throw "constraints.txt isn't the released file: nothing was installed."
        }
    }

    function Show-Finish($Version) {
        Write-Host ""
        Write-Host "azure-health-console $Version is installed." -ForegroundColor Green
        if (-not (Has "az")) {
            Write-Host "It signs in through the Azure CLI, which isn't installed:  winget install --exact --id Microsoft.AzureCLI"
        }
        if (Has $Package) { Write-Host "Start it with:  $Package" }
        else { Write-Host "Open a new PowerShell window (so it's on your PATH), then start it with:  $Package" }
        Write-Host "Upgrade later by running this line again."
    }

    $work = Join-Path ([IO.Path]::GetTempPath()) ("$Package-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
    try {
        $python = Find-Python
        if (-not $python) {
            throw "Python 3.11 or later is needed. Install it with:  winget install --exact --id Python.Python.3.13`nthen open a new PowerShell window and run this again."
        }
        if (Has "pipx") { $PipxCommand = @("pipx") }
        else {
            $PipxCommand = @($python, "-m", "pipx")
            & $python -m pipx --version *> $null
            if ($LASTEXITCODE -ne 0) {
                Write-Host "Installing pipx (for your user only)..."
                & $python -m pip install --user --quiet pipx
                if ($LASTEXITCODE -ne 0) { throw "pipx couldn't be installed: the output above says why." }
            }
        }
        $running = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
                $_.ProcessName -eq $Package -or ($_.Path -and $_.Path -like "*\venvs\$Package\*") })
        if ($running.Count) {
            throw "The console is running, and Windows can't replace a running program's files. Stop it (Ctrl+C in its window), then run this again."
        }

        $release = Join-Path $work "release"
        New-Item -ItemType Directory -Force $release | Out-Null
        if ($env:AHC_VERSION) { $base = "https://github.com/$Repo/releases/download/v$($env:AHC_VERSION)" }
        else { $base = "https://github.com/$Repo/releases/latest/download" }
        Write-Host "Downloading the $(if ($env:AHC_VERSION) { $env:AHC_VERSION } else { 'newest' }) release's signed file list..."
        foreach ($file in "SHA256SUMS.txt", "SHA256SUMS.txt.sig", "constraints.txt") {
            try { Invoke-WebRequest -UseBasicParsing -Uri "$base/$file" -OutFile (Join-Path $release $file) -ErrorAction Stop }
            catch { throw "Couldn't download $base/$file ($($_.Exception.Message)). Releases: https://github.com/$Repo/releases" }
        }

        $verifyHelp = $false
        if (Has $Package) { & $Package verify --help *> $null; $verifyHelp = $LASTEXITCODE -eq 0 }
        if ($verifyHelp) {
            Write-Host "Checking the release with the key inside your installed $(& $Package --version)..."
            & $Package verify $release
            if ($LASTEXITCODE -ne 0) { throw "Not verified: nothing was installed. Please open an issue: https://github.com/$Repo/issues" }
        }
        else {
            Write-Host "Checking the release's signature..."
            Test-Release $release
        }
        $pin = Select-String -Path (Join-Path $release "constraints.txt") -Pattern "^$Package==([0-9][0-9A-Za-z.+-]*)"
        if (-not $pin) { throw "constraints.txt doesn't name a version of ${Package}: nothing was installed." }
        $version = $pin.Matches[0].Groups[1].Value
        Write-Host "Verified: $version is the signed release."

        $installed = ""
        if (Has $Package) { $installed = ([string](& $Package --version 2>$null)).Split(" ")[-1] }
        if ($installed -eq $version) {
            Write-Host "$version is already installed."
            Show-Finish $version
            return
        }
        Write-Host "Installing $version with pipx (pip checks every file against the signed list)..."
        Push-Location $release
        try {
            Invoke-Pipx upgrade-shared *> $null
            Invoke-Pipx install --force --python $python "$Package==$version" "--pip-args=--require-hashes -c constraints.txt"
            if ($LASTEXITCODE -ne 0) { throw "pipx couldn't install it: the output above says why." }
        }
        finally { Pop-Location }
        if (-not (Has $Package)) { Invoke-Pipx ensurepath *> $null }
        Show-Finish $version
    }
    catch {
        Write-Host ""
        Write-Host $_.Exception.Message -ForegroundColor Red
    }
    finally {
        Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
    }
}
