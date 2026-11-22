# Deployment files

## systemd (Linux)

`nai@.service` is a systemd template unit (uses `%i` for the user). Adjust paths
if your checkout is not at `/home/<user>/Projects/nai`.

```bash
sudo cp deploy/nai@.service /etc/systemd/system/nai@.service
sudo systemctl daemon-reload
sudo systemctl enable --now nai@$USER
systemctl status nai@$USER
journalctl -u nai@$USER -f
```

The unit runs `python -m nai.service.daemon` in the foreground; systemd handles
process supervision, restart and logging. The supervisor still writes
`.nai/service.sock`, so `nai service status|logs|exec|jobs` works alongside it.

## macOS (launchd)

Create `~/Library/LaunchAgents/local.nai.plist` (replace paths):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.nai</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/nai/.venv/bin/python</string>
    <string>-m</string><string>nai.service.daemon</string>
    <string>--state-dir</string><string>/path/to/nai/.nai</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/nai</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/path/to/nai/.nai/logs/service.log</string>
  <key>StandardErrorPath</key><string>/path/to/nai/.nai/logs/service.log</string>
</dict>
</plist>
```

Then `launchctl load ~/Library/LaunchAgents/local.nai.plist`.

## Manual (foreground)

```bash
nai service start --foreground
```

Stop with Ctrl-C. The launcher stops all children gracefully.
