# Keeps Titan running on a laptop that sleeps.
#
# The problem this solves, measured four times between 4 and 16 September 2026:
# Docker Desktop's own autostart is a Run key under HKCU, and a Run key fires on
# *login*. Waking from sleep is not a login -- the session resumes, the desktop
# looks entirely normal, and the one process the whole estate depends on is
# simply not running. Titan was down for 2 days 22 hours over 14-16 September
# for exactly this, costing three consecutive working days of sending, and
# nothing noticed because everything that could notice runs inside Docker.
#
# The obvious fix is a Scheduled Task with a resume-from-sleep trigger. That
# needs administrator rights on this machine -- both Register-ScheduledTask and
# schtasks.exe return "Access is denied" unelevated -- so this does the same job
# from the Startup folder, which needs no privilege at all.
#
# It is also strictly better than the task in one respect: a task triggered on
# wake only covers waking. This notices Docker being down for any reason,
# including Docker Desktop crashing or being quit by hand.
#
# What it deliberately does NOT do: wake the machine. That is genuinely
# privileged, and nothing running as a normal user can do it. While the laptop
# is asleep Titan is stopped, and the only fixes for that are not sleeping it or
# moving Titan to a host that never does.

$ErrorActionPreference = "Stop"

$DockerExe   = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
$LogPath     = Join-Path $env:LOCALAPPDATA "Titan\keep-titan-up.log"
$PollSeconds = 120

# After launching Docker, give the engine time to come up before judging it
# again. Docker Desktop takes well over a minute on a cold start, and a shorter
# grace period would see "not ready yet", launch it a second time, and end up
# with two copies fighting over the same VM.
$StartGraceSeconds = 300

# How long Docker Desktop may sit running with a dead engine before this stops
# waiting and resets the VM underneath it.
#
# Found by testing rather than reasoning. The first version of this script only
# knew how to start Docker Desktop, which is the right repair when the app is
# not running and the wrong one when it is. Killing Docker's processes outright
# -- a deliberately harsher test than a wake from sleep -- left Docker Desktop
# quite happy to relaunch and the WSL VM behind it wedged: eleven Docker
# processes alive, `docker ps` failing, and this script logging "waiting" every
# three minutes for eighteen minutes with no path to ever fixing it.
#
# `wsl --shutdown` is what a person does at that point, and it worked in twenty
# seconds. Ten minutes before reaching for it, because it is the blunter
# instrument: it stops every WSL distribution on the machine, not only Docker's.
$WedgedAfterSeconds = 600

New-Item -ItemType Directory -Force -Path (Split-Path $LogPath) | Out-Null

function Write-Log([string]$Message) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $LogPath -Value $line -Encoding utf8
    # Keep the log from growing without bound. Trimmed rather than rotated: this
    # is a breadcrumb trail for "why did Titan restart at 3am", not an audit log.
    if ((Get-Item $LogPath).Length -gt 1MB) {
        $keep = Get-Content $LogPath -Tail 2000
        Set-Content -Path $LogPath -Value $keep -Encoding utf8
    }
}

function Test-DockerEngine {
    # `docker ps` rather than checking for the process: Docker Desktop's process
    # can be alive while the Linux VM behind it is not, and the VM is the thing
    # the containers actually need. This asks the engine a question only a
    # working engine can answer.
    try {
        $null = & docker ps --quiet 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Reset-DockerVm {
    # The repair of last resort: stop Docker, reset WSL underneath it, start
    # Docker again. Only reached after $WedgedAfterSeconds of the app being up
    # with a dead engine, because it stops every WSL distribution on the
    # machine and not only Docker's.
    Write-Log "engine wedged -- stopping Docker and resetting WSL"
    Get-Process | Where-Object { $_.ProcessName -like "*Docker*" } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 5
    try {
        & wsl.exe --shutdown 2>&1 | Out-Null
    } catch {
        Write-Log ("wsl --shutdown failed: " + $_.Exception.Message)
    }
    Start-Sleep -Seconds 8
    if (Test-Path $DockerExe) { Start-Process -FilePath $DockerExe | Out-Null }
    Write-Log "Docker relaunched after WSL reset"
}

Write-Log "watcher started (poll ${PollSeconds}s)"

# When the engine first went down while Docker Desktop was up. Null whenever the
# engine is healthy, so an unrelated blip does not accumulate towards a reset.
$engineDownSince = $null

while ($true) {
    try {
        if (Test-DockerEngine) {
            $engineDownSince = $null
            Start-Sleep -Seconds $PollSeconds
            continue
        }

        if (-not (Test-Path $DockerExe)) {
            Write-Log "Docker Desktop not found at $DockerExe; nothing to start"
            Start-Sleep -Seconds $PollSeconds
            continue
        }

        # Docker Desktop is up but the engine is not. Usually it is still
        # starting, so the first answer is to wait -- launching a second copy
        # while the first brings the VM up is how you get a wedge that needs a
        # reboot. But waiting forever is what the first version of this script
        # did, and "forever" is not a repair.
        if (Get-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue) {
            if ($null -eq $engineDownSince) { $engineDownSince = Get-Date }
            $downFor = [int]((Get-Date) - $engineDownSince).TotalSeconds

            if ($downFor -ge $WedgedAfterSeconds) {
                Reset-DockerVm
                $engineDownSince = $null
                Start-Sleep -Seconds $StartGraceSeconds
                continue
            }

            Write-Log "engine not ready but Docker Desktop is running; waiting (${downFor}s)"
            Start-Sleep -Seconds $PollSeconds
            continue
        }

        Write-Log "engine down and Docker Desktop not running -- starting it"
        $engineDownSince = Get-Date
        Start-Process -FilePath $DockerExe | Out-Null
        Start-Sleep -Seconds $StartGraceSeconds

        if (Test-DockerEngine) {
            Write-Log "engine is up; containers restart themselves (restart: unless-stopped)"
        } else {
            Write-Log "engine still not up after ${StartGraceSeconds}s; will retry"
        }
    } catch {
        # Never exit. A watcher that dies on one bad poll is a watcher that was
        # not there the night it mattered.
        Write-Log ("poll failed: " + $_.Exception.Message)
        Start-Sleep -Seconds $PollSeconds
    }
}
