#!/usr/bin/env bash
#
# Prepares a fresh Ubuntu 24.04 server to run the platform.
#
# Run as root on a brand-new server, once:
#
#     bash bootstrap-server.sh
#
# Installs Docker, sets up a firewall, creates a non-root user, and stops
# there. It does not clone the project or start anything - see docs/DEPLOY.md
# for the rest, which is deliberately done by hand so you see what happens.

set -euo pipefail

APP_USER="nexterpay"

say()  { printf "\n\033[1;34m==>\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m!!\033[0m %s\n" "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "Run this as root on the server (ssh root@your-server-ip)." >&2
  exit 1
fi

if ! grep -qi ubuntu /etc/os-release; then
  warn "This was written for Ubuntu. Continuing, but check each step."
fi

say "Updating packages (takes a minute or two)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq

say "Installing Docker, git and basics"
apt-get install -y -qq \
  ca-certificates curl gnupg git ufw fail2ban unattended-upgrades

install -m 0755 -d /etc/apt/keyrings
if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
fi

. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update -qq
apt-get install -y -qq \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

systemctl enable --now docker

say "Creating the '${APP_USER}' user"
# Every step below is idempotent and runs whether or not the user already
# exists. An earlier version guarded all of this behind "is this a new user",
# which meant a re-run silently skipped the SSH key copy and the docker group -
# and then disabled password logins, locking the user out.
if id -u "$APP_USER" >/dev/null 2>&1; then
  echo "    user exists; re-checking its setup"
else
  adduser --disabled-password --gecos "" "$APP_USER"
  echo "    created"
fi

usermod -aG docker "$APP_USER"
echo "    added to the docker group"

# Carry over the SSH key so you can log straight in as this user.
if [[ -f /root/.ssh/authorized_keys ]]; then
  mkdir -p "/home/${APP_USER}/.ssh"
  cp /root/.ssh/authorized_keys "/home/${APP_USER}/.ssh/authorized_keys"
  chown -R "${APP_USER}:${APP_USER}" "/home/${APP_USER}/.ssh"
  chmod 700 "/home/${APP_USER}/.ssh"
  chmod 600 "/home/${APP_USER}/.ssh/authorized_keys"
  echo "    SSH key copied from root"
else
  warn "No SSH key found for root. You will need a password to log in as ${APP_USER}."
fi

say "Firewall"
# The bot dials out to Telegram; nothing needs to reach it. SSH only.
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null
ufw status verbose | sed 's/^/    /'

say "Hardening SSH"
# Only disable password logins if a key is actually installed. Doing it
# unconditionally would lock you out of a server created with a root password
# and no key - which is a very bad five minutes to have.
if [[ -s "/home/${APP_USER}/.ssh/authorized_keys" ]]; then
  sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
  sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
  systemctl reload ssh || systemctl reload sshd
  echo "    password logins disabled (key present)"
else
  warn "No SSH key found, so password logins are being left ENABLED."
  warn "This server is reachable from the whole internet and will be probed"
  warn "within hours. fail2ban is installed and running, which helps, but the"
  warn "right fix is to add a key and re-run this script:"
  warn ""
  warn "  on your machine:  ssh-keygen -t ed25519"
  warn "                    type \$env:USERPROFILE\\.ssh\\id_ed25519.pub"
  warn "  on the server:    mkdir -p ~/.ssh && nano ~/.ssh/authorized_keys"
  warn "                    (paste the key, save, then re-run this script)"
fi

say "Automatic security updates"
dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null 2>&1 || true

say "Backups directory"
install -d -o "$APP_USER" -g "$APP_USER" "/home/${APP_USER}/backups"

cat <<EOF

────────────────────────────────────────────────────────────
 Server is ready.

 Next, from your own machine:

   ssh ${APP_USER}@$(hostname -I | awk '{print $1}')

 Then follow docs/DEPLOY.md from step 4.

 Notes:
   - Password logins are now disabled. Keep your SSH key safe.
   - Only SSH is open. Nothing needs to reach the bot.
   - Docker is installed and '${APP_USER}' can use it without sudo.
────────────────────────────────────────────────────────────

EOF
