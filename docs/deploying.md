# Deploying the orchestrator

How to set up the orchestrator on the factory PC, release a new version, and recover. The design is in [orchestrator.md](orchestrator.md) (where it runs and [Deploying](orchestrator.md#deploying)), and why it's on this PC is in [ADR 0002](adr/0002-orchestrator-on-a-linux-mini-pc.md).

**Not yet tested on the PC.** This runbook replaces the Mac mini one, which was tested on 2026-11-22. Steps marked *untested* are the ones most likely to need fixing on the first real deploy.

The orchestrator is one process (`factory serve`) and one SQLite file. systemd runs it as the `swfactory` user, from `orchestrator/deploy/factory-orchestrator.service`. The same PC is the development machine, under your own account; [step 2](#2-your-own-account) keeps the two apart.

| What | Where |
|---|---|
| Code | `/home/swfactory/swfactory`, checked out at a release tag |
| `factory` command | `/home/swfactory/swfactory/orchestrator/.venv/bin/factory` |
| Database | `/home/swfactory/.factory/factory.db` |
| Bare repos for checking and pushing bundles | `/home/swfactory/.factory/git/` |
| Orchestrator log (JSON lines, rotated, 30 days) | `/home/swfactory/.factory/logs/` |
| stdout and stderr (crashes before logging starts) | the journal: `sudo journalctl -u factory-orchestrator` |
| Nightly database backups (the newest 14) | `/home/swfactory/.factory/backups/`, and off the machine with restic |
| The App's id and key path | `/home/swfactory/.factory/orchestrator.env` |
| GitHub App private key | `/home/swfactory/.factory/app-key.pem` |

## One-time setup

### 0. The PC

1. In the BIOS (F1 at power-on on a ThinkCentre): set **After Power Loss** to **Power On**, so it comes back after a power cut.
2. Install **Ubuntu Server LTS** (24.04 or newer). Call the machine `factory-pc`, make your own account in the installer, and tick **Install OpenSSH server**. Ubuntu Server doesn't sleep and installs security updates by itself (`unattended-upgrades`).
3. Reserve its IP address in the router's DHCP settings, and give the laptop the name in `~/.ssh/config`:

   ```
   Host factory-pc
       HostName <IP address>
   ```

### 1. The `swfactory` user and SSH

As your own account on the PC:

1. Add the user. It has no password, so it can only be reached with an SSH key:

   ```sh
   sudo adduser --disabled-password --gecos "" swfactory
   sudo chmod 700 /home/*
   ```

   Ubuntu makes new homes readable by their own group only, but `700` makes sure. Run the `chmod` again for any user you add later.
2. From the laptop, put your public key on both accounts and check they get in:

   ```sh
   ssh-copy-id factory-pc                     # your own account, with its password
   ssh -t factory-pc 'sudo install -d -m 700 -o swfactory -g swfactory /home/swfactory/.ssh &&
     sudo install -m 600 -o swfactory -g swfactory ~/.ssh/authorized_keys /home/swfactory/.ssh/'
   ssh swfactory@factory-pc true
   ```

3. Turn off password login for SSH. Add to `/etc/ssh/sshd_config.d/100-factory.conf`, then `sudo systemctl restart ssh`:

   ```
   PasswordAuthentication no
   KbdInteractiveAuthentication no
   ```

4. Check from the laptop that the key gets in, a password is refused, and the homes are closed:

   ```sh
   ssh swfactory@factory-pc true                          # succeeds
   ssh -o PubkeyAuthentication=no swfactory@factory-pc    # must fail with "Permission denied (publickey)"
   ssh factory-pc ls /home/swfactory                      # must fail with "Permission denied"
   ```

SSH works on the home network only. There's no access from away: Tailscale is left for later, if it's ever needed.

### 2. Your own account

You develop on this PC, often with an agent working as you. Two things would let anything running as you read the App key, so neither is allowed:

- **Passwordless sudo.** The installer's account needs its password for `sudo`; keep it that way. Check:

  ```sh
  sudo -k; sudo -n true                                   # must fail with "a password is required"
  sudo grep -rs NOPASSWD /etc/sudoers /etc/sudoers.d      # only the swfactory rule from step 8, once it exists
  ```

- **The `docker` group, or any root Docker daemon.** A container started through the system daemon can mount `/home/swfactory`. Use **rootless Docker** instead, which runs a daemon as you, with only your rights. *Untested here.* Install Docker Engine from Docker's apt repository (docs.docker.com → Install → Ubuntu), including `docker-ce-rootless-extras`, plus `uidmap`. Then:

  ```sh
  sudo systemctl disable --now docker.service docker.socket   # no root daemon
  dockerd-rootless-setuptool.sh install                       # as you, not with sudo
  systemctl --user enable --now docker
  sudo loginctl enable-linger "$USER"                         # keep it running when you're logged out
  docker context use rootless
  ```

  Check:

  ```sh
  getent group docker                                          # no members after the last ":"
  docker run --rm -v /home/swfactory:/h alpine ls /h           # must fail with "Permission denied"
  ```

  VS Code Dev Containers over Remote-SSH uses the `docker` command and its current context, so it picks up the rootless daemon.

Never put the App key, the production database or `swfactory`'s Claude login in your own account. To try the orchestrator while developing, use a separate test App and database.

### 3. Tools

As your own account: `sudo apt install git python3 python3-venv restic`. Python 3.12 or newer is required: check `python3 --version`.

### 4. The code

As `swfactory`:

```sh
git clone https://github.com/ScottMRedmond/swfactory.git ~/swfactory
cd ~/swfactory
git checkout <release tag>
python3 -m venv orchestrator/.venv
orchestrator/.venv/bin/pip install ./orchestrator
install -d -m 700 ~/.factory
```

Put `~/swfactory/orchestrator/.venv/bin` and `~/.local/bin` on `swfactory`'s `PATH` at the **top** of `~/.bashrc`, so `ssh swfactory@factory-pc factory status` works. It has to go at the top: bash reads `~/.bashrc` for `ssh` with a command, but Ubuntu's `~/.bashrc` stops early for a non-interactive shell.

```sh
sed -i '1i export PATH="$HOME/swfactory/orchestrator/.venv/bin:$HOME/.local/bin:$PATH"' ~/.bashrc
```

### 5. The GitHub App key

Give the PC a key of its own. On the App's settings page, **Generate a private key**: a GitHub App can have several, so this one is only ever used here, and can be revoked on its own if the PC is lost, without touching any other key. Copy it to the PC, then delete the downloaded copy from your laptop. The key must exist **only** here.

```sh
scp app.private-key.pem swfactory@factory-pc:.factory/app-key.pem
ssh swfactory@factory-pc chmod 600 .factory/app-key.pem
```

Then, as `swfactory`, write the App's id (on the App's settings page) and the key's path where both the service and `factory doctor` read them. Neither is secret:

```sh
printf 'FACTORY_APP_ID=<app id>\nFACTORY_APP_KEY_FILE=/home/swfactory/.factory/app-key.pem\n' > ~/.factory/orchestrator.env
chmod 600 ~/.factory/orchestrator.env
sed -i '2i set -a; . ~/.factory/orchestrator.env; set +a' ~/.bashrc
```

`~/.factory` is mode 700, so only `swfactory` (and root) can read the key, the database and its backups. Check as your own account:

```sh
ls /home/swfactory/.factory    # must fail with "Permission denied"
```

### 6. Claude Code, for reading usage

The orchestrator runs `claude -p /usage` before dispatching, to apply the [usage pause](orchestrator.md#limits-and-brakes). It never runs a model on the PC. As `swfactory`:

1. Install Claude Code with the native installer, which puts `claude` in `~/.local/bin`. The unit's `PATH` includes it.
2. Run a full `claude auth login`. Over SSH it prints a URL: open it on the laptop and paste the code back. A `setup-token` token isn't enough: `/usage` then returns no usage report, and the factory never dispatches.
3. Check: `claude -p "/usage" --output-format stream-json --verbose` prints a `usage_report` line.

**From systemd: *untested*.** On the Mac mini, a launchd job got a full `usage_report` from a login made over SSH. On Linux, Claude Code keeps its login in a file under `~/.claude` instead of a keychain, so a service should see it too. After step 7, `factory status` should show a fresh usage reading.

**The login expires.** The usage result carries `credential_expires_at`, about 30 days after `claude auth login`. Whether Claude Code renews it by itself without anyone running it interactively hasn't been checked. If it lapses, usage becomes unreadable, the factory stops dispatching, and after 15 minutes it opens a [factory problem](orchestrator.md#observability). The fix is to run `claude auth login` again as `swfactory`.

### 7. The systemd service

As your own account. The unit file needs no editing: it reads the App's id and key path from `orchestrator.env`.

```sh
sudo cp /home/swfactory/swfactory/orchestrator/deploy/factory-orchestrator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now factory-orchestrator
```

`enable` starts it at boot, and `Restart=always` restarts it 30 seconds after it exits.

Check it's running:

```sh
systemctl status factory-orchestrator     # active (running), and its pid
factory status                            # as swfactory: last poll per repo, usage reading
factory logs --follow
```

If it won't stay up, look in `sudo journalctl -u factory-orchestrator -n 50` first.

Check it comes back by itself, once after a crash and once after a reboot:

```sh
sudo kill <pid>                                        # the pid from systemctl status
systemctl status factory-orchestrator                  # a new pid within about 30 seconds
sudo reboot                                            # then, without logging in:
ssh swfactory@factory-pc factory status
```

### 8. Let `swfactory` restart its own service

`swfactory` isn't an admin. One sudo rule lets it restart the orchestrator, and nothing else, so releases don't need your account. As your own account:

```sh
sudo visudo -f /etc/sudoers.d/factory
```

Add this one line and save (`visudo` refuses a rule with a syntax error):

```
swfactory ALL=(root) NOPASSWD: /usr/bin/systemctl restart factory-orchestrator.service
```

Check from the laptop. It prints the command back if it's allowed, without running it:

```sh
ssh swfactory@factory-pc 'sudo -n -l /usr/bin/systemctl restart factory-orchestrator.service'
```

sudo matches the arguments exactly, so the restart must be written exactly as in the rule, `.service` included. Anything else asks for a password and fails.

### 9. Enrol a repo

For each repo: install the GitHub App on it, add its `.factory/` enrolment and caller workflow on its default branch (see [Enrolling a repo](software-factory.md#enrolling-a-repo)), add the `CLAUDE_CODE_OAUTH_TOKEN` secret, then:

```sh
factory doctor <owner>/<repo>
```

It checks what the orchestrator can see and dispatches a `smoke` run, which uses no Claude usage.

## Releasing a new version

Deploy by hand, from a tag, never automatically from `main`.

1. Merge to `main`. If the change touched `contracts/`, the order matters: **merge first, then redeploy** (see [The runner contract](software-factory.md#the-runner-contract)). Callers already use `run.yml@main`, which accepts the current and the previous `contract_version`.
2. Tag it: `git tag v<N> && git push origin v<N>`.
3. On the PC, as `swfactory`:

   ```sh
   cd ~/swfactory
   git fetch --tags
   git checkout v<N>
   orchestrator/.venv/bin/pip install ./orchestrator   # picks up new dependencies
   sudo /usr/bin/systemctl restart factory-orchestrator.service
   ```

   The [sudo rule](#8-let-swfactory-restart-its-own-service) allows this one command without a password. Installing, enabling or stopping the service still needs your account.
4. Check `factory status` and `factory logs` for the startup lines and the first poll.

Runs in flight keep going on Actions during the restart. The orchestrator collects them when it comes back.

If a unit file itself changed, copy it again as in step 7 (or [Backups](#backups) for the backup units), then, as your own account:

```sh
sudo systemctl daemon-reload
sudo systemctl restart factory-orchestrator
```

## Rolling back

- **Code:** `git checkout v<N-1>`, reinstall, restart.
- **A bad migration:** before migrating, the orchestrator copies the database to `factory.db.before-v<N>`. Stop the service, put the copy back, and check out the previous tag. Stopping and starting the service needs your account; the copy must be made as `swfactory`, so the file stays its own:

  ```sh
  # as your own account
  sudo systemctl stop factory-orchestrator
  # as swfactory
  cp ~/.factory/factory.db.before-v<N> ~/.factory/factory.db
  rm -f ~/.factory/factory.db-wal ~/.factory/factory.db-shm
  cd ~/swfactory && git checkout v<N-1> && orchestrator/.venv/bin/pip install ./orchestrator
  # as your own account
  sudo systemctl start factory-orchestrator
  ```

  Runs recorded after the migration are lost from history. Their work isn't: it's on GitHub.

## Stopping it

- To stop new work but keep collecting what's in flight: `factory pause`, then `factory resume`.
- To stop the process: `sudo systemctl stop factory-orchestrator`, and `sudo systemctl disable factory-orchestrator` too if it should stay stopped after a reboot. Runs in flight keep going on Actions, and their files wait there (for 7 days) until it's back.

## Backups

`factory backup` copies the database to `backups/factory-<date>.db` beside it (`~/.factory/backups/`) with SQLite's online backup, which is safe while the orchestrator runs, and deletes all but the newest 14 copies. Then restic copies that folder, encrypted, off the machine, keeping 30 daily snapshots. Both run in `factory-backup.service`, started nightly at 03:30 by `factory-backup.timer`. If the PC was off at 03:30, the timer runs it as soon as the PC is back.

*Untested.* First choose where the off-machine copies go: any restic repository works, such as a NAS over SFTP or a cloud bucket (Backblaze B2, S3). As `swfactory`, write its settings and a password, then create the repository:

```sh
openssl rand -base64 32 > ~/.factory/restic-password
cat > ~/.factory/restic.env <<'EOF'
RESTIC_REPOSITORY=<repository, e.g. s3:https://s3.us-west-000.backblazeb2.com/<bucket>/factory>
RESTIC_PASSWORD_FILE=/home/swfactory/.factory/restic-password
# The storage's credentials, e.g. AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY
EOF
chmod 600 ~/.factory/restic-password ~/.factory/restic.env
(set -a; . ~/.factory/restic.env; restic init)
```

**Keep a copy of the restic password and the storage's credentials off the PC too**, in a password manager. Without them the off-machine copies can't be read, and the PC dying is the case they're for.

Then install the units, as your own account:

```sh
sudo cp /home/swfactory/swfactory/orchestrator/deploy/factory-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now factory-backup.timer
sudo systemctl start factory-backup.service     # one backup now, to check it
sudo ls /home/swfactory/.factory/backups/        # today's copy
systemctl list-timers factory-backup.timer      # the next run
```

and as `swfactory`: `(set -a; . ~/.factory/restic.env; restic snapshots)` shows the first off-machine snapshot.

If a night's backup is missing, `systemctl status factory-backup` shows whether it failed, and `sudo journalctl -u factory-backup` shows why. A missing or unreadable database fails the backup and leaves the old copies alone.

To restore one, stop the service, put it back, and start it again. As in [Rolling back](#rolling-back), the copy is made as `swfactory`:

```sh
# as your own account
sudo systemctl stop factory-orchestrator
# as swfactory, from the local copies:
cp ~/.factory/backups/factory-<date>.db ~/.factory/factory.db
# or from the off-machine copies:
(set -a; . ~/.factory/restic.env; restic restore latest --target /tmp/restore)
cp /tmp/restore/home/swfactory/.factory/backups/factory-<date>.db ~/.factory/factory.db
rm -rf /tmp/restore
# then, either way:
rm -f ~/.factory/factory.db-wal ~/.factory/factory.db-shm
# as your own account
sudo systemctl start factory-orchestrator
```

If `factory.db` is lost while the service runs, the orchestrator starts a new, empty one, and each night's copy of it pushes out a good one. Restore within two weeks from the local copies, or within 30 days from restic.

Losing the database loses history and attempt counts, not work (see [State](orchestrator.md#state)). The App key can be regenerated from the App's settings page if it's lost; revoke the old one there too.

## Moving to another machine

Stop the service, then copy `factory.db` (or last night's backup) to the new machine and follow the one-time setup there, generating the new machine its own App key (step 5). Revoke the old machine's key on the App's settings page once the new one is running. Nothing else is stateful: `git/` is rebuilt as needed and logs are just history.

**Never run two orchestrators at once.** Both would act on the same labels, and push, comment and dispatch twice. Stop the old one before starting the new one.

### Moving off the Mac mini (once)

The orchestrator first ran on the family Mac mini, under launchd, as `SWFactory`. To move it to the PC:

1. Do steps 0–6 on the PC. Don't start the service yet.
2. Stop the Mac mini's jobs and take a consistent copy of the database. As an admin on the Mac mini:

   ```sh
   sudo launchctl bootout system/local.factory.orchestrator
   sudo launchctl bootout system/local.factory.backup
   ```

   Then, as `SWFactory`: `factory backup`. Its newest `~/.factory/backups/factory-<date>.db` is the copy to move.
3. From the laptop, move it across, then delete the laptop's copy:

   ```sh
   scp SWFactory@macmini:.factory/backups/factory-<date>.db /tmp/factory.db
   scp /tmp/factory.db swfactory@factory-pc:.factory/factory.db
   rm /tmp/factory.db
   ```

4. Do steps 7–8 on the PC, then [Backups](#backups). Check `factory status` shows the history and a fresh poll.
5. Revoke the Mac mini's App key on the App's settings page.
6. Clean up the Mac mini, as an admin:

   ```sh
   sudo rm /Library/LaunchDaemons/local.factory.orchestrator.plist /Library/LaunchDaemons/local.factory.backup.plist
   sudo rm /etc/sudoers.d/factory
   ```

   Then System Settings → Users & Groups: delete `SWFactory` and its home folder, which removes its key, database, backups and Claude login. Turn off **Remote Login** in Sharing if nothing else uses it, and remove `/etc/ssh/sshd_config.d/100-factory.conf` if you want password login back.
