# Codex / AI coding rules for BikeLogger

This project runs on a Raspberry Pi attached to real hardware on a bicycle. Prefer reliability over cleverness.

## Safety rules

- Do not delete ride data under `/var/lib/bikelogger/rides`.
- Do not change GPIO pins unless explicitly requested.
- Do not change database table/column names without a migration plan.
- Keep the web UI local-only unless explicitly requested.
- Do not hard-code private WiFi passwords, home IPs, tokens, or GitHub credentials.
- Preserve plain-text SSID logging, but do not log WiFi passwords.
- Treat Bluetooth MAC addresses and ride GPS tracks as private data.

## Hardware assumptions

- Start button: GPIO27, active-low.
- Stop button: GPIO17, active-low.
- GPS: `/dev/serial0`, 38400 baud.
- BME280/BMP280: I2C bus 1, `0x76` or `0x77`.
- Camera: Raspberry Pi Camera Module 3 using Picamera2/libcamera.
- Bluetooth: built-in `hci0`, scanned with `btmgmt find`.

## Coding style

- Keep dependencies minimal.
- Use SQLite for ride data.
- Handle missing hardware gracefully.
- Any sensor failure should degrade, not crash the service.
- Add or update tests when changing parsers.
- Keep CPU usage reasonable; avoid busy loops.

## Deployment model

The Raspberry Pi installs from this git checkout using:

```bash
sudo ./install.sh
```

Routine updates happen with:

```bash
sudo bikelogger-update
```

That command pulls from git, reinstalls code into `/opt/bikelogger`, and restarts `bikelogger.service`.
