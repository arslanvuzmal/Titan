' Launches keep-titan-up.ps1 with no visible window.
'
' A .cmd or a bare PowerShell shortcut in the Startup folder flashes a console
' window at every login and leaves one sitting in the taskbar for as long as the
' watcher runs -- which is forever. Nobody tolerates that, and a watcher people
' close is a watcher that is not running the night it matters.
'
' WScript.Shell's Run with intWindowStyle 0 and bWaitOnReturn False starts it
' hidden and detached, which is the whole reason this file exists.

Dim shell, here, script
Set shell = CreateObject("WScript.Shell")

here = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
script = here & "keep-titan-up.ps1"

shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & script & """", 0, False
