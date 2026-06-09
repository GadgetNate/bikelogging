# Mac mini + VS Code + GitHub + Raspberry Pi setup

## 1. Mac mini

Install these:

- Visual Studio Code
- Git
- GitHub CLI, optional but useful

Check:

```bash
git --version
code --version
```

Unzip the project and open it:

```bash
cd ~/Projects
unzip ~/Downloads/bikelogger_vscode_git_pi4.zip
cd bikelogger
code .
```

Initialize git:

```bash
git init
git add .
git commit -m "Initial BikeLogger repo"
```

Create a GitHub repo and push:

```bash
git branch -M main
git remote add origin git@github.com:YOUR_GITHUB_USER/bikelogger.git
git push -u origin main
```

## 2. Give Codex access

Use the GitHub repository as the shared workspace. Give Codex access to the repo rather than direct access to the Raspberry Pi.

Good rule: Codex can propose and edit code, but the Pi only runs code you have committed and deployed intentionally.

## 3. Raspberry Pi first clone

On the Pi:

```bash
sudo apt update
sudo apt install -y git
cd ~
git clone git@github.com:YOUR_GITHUB_USER/bikelogger.git
cd bikelogger
sudo ./install.sh
sudo reboot
```

If SSH keys are annoying at first, clone over HTTPS instead:

```bash
git clone https://github.com/YOUR_GITHUB_USER/bikelogger.git
```

## 4. Normal update cycle

On Mac:

```bash
git checkout -b improve/bluetooth-logging
# edit files
git add .
git commit -m "Improve Bluetooth logging"
git push -u origin improve/bluetooth-logging
```

Merge or switch the Pi to that branch.

On Pi:

```bash
cd ~/bikelogger
git checkout main
git pull
sudo ./install.sh
```

After the first install, you can just run:

```bash
sudo bikelogger-update
```

## 5. Validation after every update

```bash
sudo bikelogger-test
sudo bikelogger-report
journalctl -u bikelogger -n 80 --no-pager
```

Open:

```text
http://<raspberry-pi-ip>:8080/
```
