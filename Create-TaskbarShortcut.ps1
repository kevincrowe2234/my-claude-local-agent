# Creates/updates "My Claude Local Agent.lnk" in this folder, pointing at
# pythonw.exe (no console window) running my_claude_agent_app.py.
#
# After running this, right-click the .lnk file and choose "Pin to taskbar".
#
# NOTE: Windows copies pinned shortcuts into
#   %APPDATA%\Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar\
# at the moment you pin them. If you ever change the target again after
# pinning, re-run this script AND then re-run it a second time targeting
# that pinned copy directly (or simply unpin and re-pin) - editing this
# .lnk file alone will not update an already-pinned icon.

$appDir = $PSScriptRoot
$scriptName = "my_claude_agent_app.py"

$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    # Fall back to common install locations if pythonw isn't on PATH.
    $candidates = @(
        "C:\Program Files\Python39\pythonw.exe",
        "C:\Program Files\Python312\pythonw.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python39\pythonw.exe"
    )
    $pythonw = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}

if (-not $pythonw) {
    Write-Error "Could not find pythonw.exe. Install Python (with 'Add to PATH' checked) first."
    exit 1
}

$lnkPath = Join-Path $appDir "My Claude Local Agent.lnk"
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath = $pythonw
$lnk.Arguments = "`"$scriptName`""
$lnk.WorkingDirectory = $appDir
$lnk.WindowStyle = 1
$lnk.IconLocation = "C:\Windows\System32\shell32.dll,236"
$lnk.Save()

Write-Output "Shortcut created/updated: $lnkPath"
Write-Output "  Target: $pythonw"
Write-Output "  Arguments: `"$scriptName`""
Write-Output "  WorkingDirectory: $appDir"
Write-Output ""
Write-Output "Next: right-click the .lnk file in File Explorer and choose 'Pin to taskbar'."
