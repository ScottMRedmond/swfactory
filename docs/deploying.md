# Deploying the orchestrator

How to set up the orchestrator on the family Mac mini, release a new version, and recover. The design is in [orchestrator.md](orchestrator.md) (where it runs and [Deploying](orchestrator.md#deploying)).

The orchestrator is one process (`factory serve`) and one SQLite file. launchd runs it as the `SWFactory` user, from `orchestrator/deploy/local.factory.orchestrator.plist`.

| What | Where |
|---|---|
| Code | `/Users/SWFactory/swfactory`, checked out at a release tag |
| `factory` command | `/Users/SWFactory/swfactory/orchestrator/.venv/bin/factory` |
| Database | `/Users/SWFactory/.factory/factory.db` |
| Bare repos for checking and pushing bundles | `/Users/SWFactory/.factory/git/` |
| Orchestrator log (JSON lines, rotated, 30 days) | `/Users/SWFactory/.factory/logs/` |
| launchd's stdout and stderr (crashes before logging starts) | `/Users/SWFactory/.factory/launchd.log` |
| Nightly database backups (the newest 14) | `/Users/SWFactory/.factory/backups/` |
| GitHub App private key | `/Users/SWFactory/.factory/app-key.pem` |

## One-time setup

### 1. The `SWFactory` user and SSH

1. System Settings → Users & Groups: add a **Standard** user named `SWFactory`. Other family accounts must be non-admin, so they can't read its files.
2. System Settings → General → Sharing: turn on **Remote Login**, allowed for `SWFactory` (and your own admin account).
3. Turn off password login for SSH. Add to `/etc/ssh/sshd_config.d/100-factory.conf`:

   ```
   PasswordAuthentication no
   KbdInteractiveAuthentication no
   ```

4. Put your laptop's public key in `/Users/SWFactory/.ssh/authorized_keys` (mode 600, owned by `SWFactory`), and check `ssh SWFactory@macmini` works before closing your existing session.
5. Install Tailscale on the Mac mini and the laptop, signed in to the same tailnet. In the Tailscale admin console, turn off key expiry for the Mac mini, so it doesn't drop off the tailnet while you're away. Use Tailscale's own sign-in, not Tailscale SSH: sshd and its key-only rule stay the only way in.

Check both ways in from the laptop, and that a password is refused:

```sh
ssh SWFactory@macmini true                          # at home
ssh SWFactory@<tailnet name of the mac mini> true   # with the laptop off the home network, e.g. a phone hotspot
ssh -o PubkeyAuthentication=no SWFactory@macmini    # must fail with "Permission denied (publickey)"
```

### 2. Tools

As an admin: install Homebrew, then `brew install python@3.12 git`. Python 3.12 or newer is required.

### 3. The code

As `SWFactory`:

```sh
git clone https://github.com/ScottMRedmond/swfactory.git ~/swfactory
cd ~/swfactory
git checkout <release tag>
python3 -m venv orchestrator/.venv
orchestrator/.venv/bin/pip install ./orchestrator
mkdir -p ~/.factory && chmod 700 ~/.factory
```

Put `orchestrator/.venv/bin` on `SWFactory`'s `PATH` (in `~/.zprofile`), so `ssh SWFactory@macmini factory status` works.

### 4. The GitHub App key

Give the Mac mini a key of its own. On the App's settings page, **Generate a private key**: a GitHub App can have several, so this one is only ever used here, and can be revoked on its own if the Mac mini is lost, without touching any other key. Copy it to the Mac mini, then delete the downloaded copy from your laptop. The key must exist **only** here.

```sh
scp app.private-key.pem SWFactory@macmini:.factory/app-key.pem
ssh SWFactory@macmini chmod 600 .factory/app-key.pem
```

Note the App's id (on the App's settings page). It goes in the plist.

`~/.factory` is mode 700, so only `SWFactory` can read the key, the database and its backups. A new macOS home folder is readable by the `staff` group, which every family account is in, so this matters. Check it from another family account (one that isn't an admin):

```sh
ls /Users/SWFactory/.factory    # must fail with "Permission denied"
```

### 5. Claude Code, for reading usage

The orchestrator runs `claude -p /usage` before dispatching, to apply the [usage pause](orchestrator.md#limits-and-brakes). It never runs a model on the Mac mini. As `SWFactory`:

1. Install Claude Code (the native installer puts `claude` in `~/.local/bin`, which the plist's `PATH` includes).
2. Run a full `claude auth login`. A `setup-token` token isn't enough: `/usage` then returns no usage report, and the factory never dispatches.
3. Check: `claude -p "/usage" --output-format stream-json --verbose` prints a `usage_report` line.

**The login works from launchd.** Tested on 2026-11-22 with Claude Code 2.1.330: a system launchd job running as `SWFactory`, with nobody logged in as `SWFactory`, got a full `usage_report` from a login made over SSH. After step 6, `factory status` should show a fresh usage reading.

**The login expires.** The usage result carries `credential_expires_at`, about 30 days after `claude auth login`. Whether Claude Code renews it by itself without anyone running it interactively hasn't been checked. If it lapses, usage becomes unreadable, the factory stops dispatching, and after 15 minutes it opens a [factory problem](orchestrator.md#observability). The fix is to run `claude auth login` again as `SWFactory`.

### 6. The launchd job

```sh
cd /Users/SWFactory/swfactory
sed 's/REPLACE_WITH_APP_ID/<app id>/' orchestrator/deploy/local.factory.orchestrator.plist \
  | sudo tee /Library/LaunchDaemons/local.factory.orchestrator.plist > /dev/null
sudo chown root:wheel /Library/LaunchDaemons/local.factory.orchestrator.plist
sudo chmod 644 /Library/LaunchDaemons/local.factory.orchestrator.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/local.factory.orchestrator.plist
```

It's a daemon (in `/Library/LaunchDaemons`, with `UserName` set to `SWFactory`), so it starts at boot without anyone logging in, and `KeepAlive` restarts it when it exits. The plist holds the App id, which isn't secret, and the path to the key, not the key itself.

Check it's running:

```sh
sudo launchctl print system/local.factory.orchestrator | grep -E 'state|pid|last exit'
factory status        # as SWFactory: last poll per repo, usage reading
factory logs --follow
```

If it won't stay up, look in `~/.factory/launchd.log` first.

### 7. Let `SWFactory` restart its own job

`SWFactory` isn't an admin. One sudo rule lets it restart the orchestrator, and nothing else, so releases don't need your admin account. As an admin:

```sh
sudo visudo -f /etc/sudoers.d/factory
```

Add this one line and save (`visudo` refuses a rule with a syntax error):

```
SWFactory ALL=(root) NOPASSWD: /bin/launchctl kickstart -k system/local.factory.orchestrator
```

Check from the laptop. It prints the command back if it's allowed, without running it:

```sh
ssh SWFactory@macmini 'sudo -n -l /bin/launchctl kickstart -k system/local.factory.orchestrator'
```

Tested on 2026-11-22. sudo matches the arguments exactly, so the restart must be `kickstart -k` with this label. Anything else, such as leaving out `-k`, asks for a password and fails.

### 8. Enrol a repo

For each repo: install the GitHub App on it, add its `.factory/` enrolment and caller workflow on its default branch (see [Enrolling a repo](software-factory.md#enrolling-a-repo)), add the `CLAUDE_CODE_OAUTH_TOKEN` secret, then:

```sh
factory doctor <owner>/<repo>
```

It checks what the orchestrator can see and dispatches a `smoke` run, which uses no Claude usage.

## Releasing a new version

Deploy by hand, from a tag, never automatically from `main`.

1. Merge to `main`. If the change touched `contracts/`, the order matters: **merge first, then redeploy** (see [The runner contract](software-factory.md#the-runner-contract)). Callers already use `run.yml@main`, which accepts the current and the previous `contract_version`.
2. Tag it: `git tag v<N> && git push origin v<N>`.
3. On the Mac mini, as `SWFactory`:

   ```sh
   cd ~/swfactory
   git fetch --tags
   git checkout v<N>
   orchestrator/.venv/bin/pip install ./orchestrator   # picks up new dependencies
   sudo /bin/launchctl kickstart -k system/local.factory.orchestrator
   ```

   The [sudo rule](#7-let-swfactory-restart-its-own-job) allows this one command without a password. Installing or removing the job (`bootstrap`, `bootout`) still needs your admin account.
4. Check `factory status` and `factory logs` for the startup lines and the first poll.

Runs in flight keep going on Actions during the restart. The orchestrator collects them when it comes back.

If a plist itself changed, reinstall it (step 6 for the orchestrator, [Backups](#backups) for `local.factory.backup`, with `bootout` first). The orchestrator's:

```sh
sudo launchctl bootout system/local.factory.orchestrator
# copy it again as in step 6, then:
sudo launchctl bootstrap system /Library/LaunchDaemons/local.factory.orchestrator.plist
```

## Rolling back

- **Code:** `git checkout v<N-1>`, reinstall, kickstart.
- **A bad migration:** before migrating, the orchestrator copies the database to `factory.db.before-v<N>`. Stop the job, put the copy back, and check out the previous tag. Stopping and starting the job needs your admin account; the copy must be made as `SWFactory`, so the file stays its own:

  ```sh
  # as an admin
  sudo launchctl bootout system/local.factory.orchestrator
  # as SWFactory
  cp ~/.factory/factory.db.before-v<N> ~/.factory/factory.db
  rm -f ~/.factory/factory.db-wal ~/.factory/factory.db-shm
  git -C ~/swfactory checkout v<N-1>
  # as an admin
  sudo launchctl bootstrap system /Library/LaunchDaemons/local.factory.orchestrator.plist
  ```

  Runs recorded after the migration are lost from history. Their work isn't: it's on GitHub.

## Stopping it

- To stop new work but keep collecting what's in flight: `factory pause`, then `factory resume`.
- To stop the process: `sudo launchctl bootout system/local.factory.orchestrator`. Runs in flight keep going on Actions, and their files wait there (for 7 days) until it's back.

## Backups

`factory backup` copies the database to `backups/factory-<date>.db` beside it (`~/.factory/backups/`) with SQLite's online backup, which is safe while the orchestrator runs, and deletes all but the newest 14 copies. A second launchd job, `orchestrator/deploy/local.factory.backup.plist`, runs it nightly at 03:30 as `SWFactory`. If the Mac mini is asleep then, launchd runs it when it wakes. Install it once, as an admin:

```sh
cd /Users/SWFactory/swfactory
sudo cp orchestrator/deploy/local.factory.backup.plist /Library/LaunchDaemons/
sudo chown root:wheel /Library/LaunchDaemons/local.factory.backup.plist
sudo chmod 644 /Library/LaunchDaemons/local.factory.backup.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/local.factory.backup.plist
sudo launchctl kickstart system/local.factory.backup   # one backup now, to check it
ls ~SWFactory/.factory/backups/                         # today's copy
```

If a night's copy is missing, look in `~/.factory/backup.log`. A missing or unreadable database fails the backup and leaves the old copies alone.

These copies are on the same disk as the database. Turn on Time Machine too (for the whole disk, or at least `/Users/SWFactory/.factory/backups/`), so a copy also lives off the machine. Time Machine copying the live `factory.db` mid-write can catch it inconsistent; the dated copies are always whole.

To restore one, stop the job, put it back, and start it again. As in [Rolling back](#rolling-back), the copy is made as `SWFactory`:

```sh
# as an admin
sudo launchctl bootout system/local.factory.orchestrator
# as SWFactory
cp ~/.factory/backups/factory-<date>.db ~/.factory/factory.db
rm -f ~/.factory/factory.db-wal ~/.factory/factory.db-shm
# as an admin
sudo launchctl bootstrap system /Library/LaunchDaemons/local.factory.orchestrator.plist
```

If `factory.db` is lost while the job runs, the orchestrator starts a new, empty one, and each night's copy of it pushes out a good one. Restore within two weeks, or take the copies from Time Machine.

Losing the database loses history and attempt counts, not work (see [State](orchestrator.md#state)). The App key can be regenerated from the App's settings page if it's lost; revoke the old one there too.

## Moving to another machine

Stop the job, then copy `factory.db` (or last night's backup) to the new machine and follow the one-time setup there, generating the new machine its own App key (step 4). Revoke the old machine's key on the App's settings page once the new one is running. Nothing else is stateful: `git/` is rebuilt as needed and logs are just history.
