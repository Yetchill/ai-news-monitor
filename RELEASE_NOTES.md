## Windows installer

Download `AI-Intelligence-Monitor-Setup-<version>-x64.exe` and the accompanying
`SHA256SUMS.txt`. In PowerShell, compare `Get-FileHash <installer> -Algorithm SHA256`
with the checksum file before installing.

The installer is per-user and does not require administrator permission. It installs the
`AI 情报助手` shortcut in the Start menu and on the desktop. Uninstalling preserves local
data under `%LOCALAPPDATA%\AIIntelligenceMonitor`.

This installer is currently **not code signed**. Windows may show an “unknown publisher”
or SmartScreen warning; verify the release source and SHA-256 before choosing to run it.
