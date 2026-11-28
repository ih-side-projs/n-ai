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

## Remote worker (macOS)

A worker runs named recipes for a Linux orchestrator over the LAN. On the
orchestrator:

```bash
nai worker add macbook --capabilities macos,python   # prints the token once
nai remote serve --host 0.0.0.0 --port 8765 --insecure-lan   # trusted LAN only
```

On the Mac:

```bash
nai-worker init --name macbook --server http://<orchestrator>:8765 --token <token>
nai-worker doctor
nai-worker run            # or install the launchd agent below
```

`nai-worker.plist` is a launchd user agent; copy it to
`~/Library/LaunchAgents/local.nai.worker.plist`, edit the paths, and
`launchctl load -w` it. `worker.toml` holds the token, so keep it at mode 0600
(`nai-worker init` does that). Prefer TLS (`--certfile/--keyfile`) whenever the
listener is reachable beyond a trusted LAN; `--insecure-lan` is an explicit
opt-in, never a default.

## Manual (foreground)

```bash
nai service start --foreground
```

Stop with Ctrl-C. The launcher stops all children gracefully.
