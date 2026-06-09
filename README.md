# BikeLogger

BikeLogger is the Raspberry Pi bike data logger and local RideHub web UI.

Current target hardware:

- Raspberry Pi 4 Model B
- Raspberry Pi Camera Module 3 / IMX708 using Picamera2/libcamera
- Serial GPS on `/dev/serial0` at 38400 baud
- BME280/BMP280 on I2C bus 1, address `0x76` or `0x77`
- Start button on GPIO27
- Stop button on GPIO17
- Built-in WiFi scanning
- Built-in Bluetooth LE scanning with RSSI

Ride data is stored in per-ride SQLite databases under:

```text
/var/lib/bikelogger/rides/<ride_id>/ride.sqlite
```

Photos are stored under:

```text
/var/lib/bikelogger/rides/<ride_id>/photos/
```

## Recommended workflow

Use GitHub as the source of truth.

1. Edit on the Mac mini in VS Code.
2. Commit small changes to git.
3. Push to GitHub.
4. SSH into the Raspberry Pi.
5. Run one command:

```bash
sudo bikelogger-update
```

The Pi pulls the latest code from the current git branch and reinstalls/restarts the service.

## First-time setup on the Mac mini

Unzip this project, then:

```bash
cd bikelogger
code .
git init
git add .
git commit -m "Initial BikeLogger repo"
```

Create a new GitHub repository, then connect and push:

```bash
git branch -M main
git remote add origin git@github.com:YOUR_GITHUB_USER/bikelogger.git
git push -u origin main
```

Use a private repo if you want to keep home network names, routes, and device data private.

## Let Codex help safely

Open this repository in VS Code and let Codex work inside this folder. The file `AGENTS.md` gives Codex project rules.

Good requests for Codex:

- "Add a test for NMEA parsing."
- "Refactor Bluetooth parsing without changing database columns."
- "Add an Export CSV button to the web UI."
- "Improve error handling for WiFi scan timeouts."

Do not give Codex shell access to the Raspberry Pi until changes are committed and reviewed. Treat the Pi as the deployment target, not the coding workspace.

## First-time setup on the Raspberry Pi

Install git and clone your repo:

```bash
sudo apt update
sudo apt install -y git
cd ~
git clone git@github.com:YOUR_GITHUB_USER/bikelogger.git
cd bikelogger
sudo ./install.sh
sudo reboot
```

After reboot:

```bash
sudo bikelogger-test
sudo bikelogger-report
```

## Normal Pi update command

After you push changes from your Mac:

```bash
sudo bikelogger-update
```

## Useful commands

Check service status:

```bash
systemctl status bikelogger --no-pager -l
```

Watch logs:

```bash
journalctl -u bikelogger -f
```

Restart manually:

```bash
sudo systemctl restart bikelogger
```

Open the web UI:

```text
http://<raspberry-pi-ip>:8080/
```

## Hardware checks

I2C:

```bash
ls -l /dev/i2c*
i2cdetect -y 1
```

Expected BME280/BMP280 address: `0x76` or `0x77`.

Serial GPS:

```bash
ls -l /dev/serial0
sudo timeout 5 cat /dev/serial0
```

Bluetooth:

```bash
rfkill list bluetooth
hciconfig -a
sudo timeout 12 btmgmt find
```

Camera:

```bash
rpicam-hello --list-cameras
```

On some newer Raspberry Pi OS installs, the old command `libcamera-hello` is replaced by `rpicam-hello`.

## Version control method

Use small branches:

```bash
git checkout -b fix/i2c-report
git add .
git commit -m "Improve I2C reporting"
git push -u origin fix/i2c-report
```

Then merge to `main` after review.

## Export latest ride to CSV

```bash
sudo bikelogger-export-latest
```

This creates CSV files inside the latest ride folder under `exports/`.
