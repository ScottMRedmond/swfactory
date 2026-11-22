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
5. Optional: install Tailscale to reach it away from home.

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

Copy the App's private key to the Mac mini, then delete the downloaded copy from your laptop. The key must exist **only** here.

```sh
scp app.private-key.pem SWFactory@macmini:.factory/app-key.pem
ssh SWFactory@macmini chmod 600 .factory/app-key.pem
```

Note the App's id (on the App's settings page). It goes in the plist.

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

If the plist itself changed, reinstall it (step 6, with `bootout` first):

```sh
sudo launchctl bootout system/local.factory.orchestrator
# copy it again as in step 6, then:
sudo launchctl bootstrap system /Library/LaunchDaemons/local.factory.orchestrator.plist
```

## Rolling back

- **Code:** `git checkout v<N-1>`, reinstall, kickstart.
- **A bad migration:** before migrating, the orchestrator copies the database to `factory.db.before-v<N>`. Stop the job, put the copy back, and check out the previous tag:

  ```sh
  sudo launchctl bootout system/local.factory.orchestrator
  cp ~/.factory/factory.db.before-v<N> ~/.factory/factory.db
  git -C ~/swfactory checkout v<N-1>
  sudo launchctl bootstrap system /Library/LaunchDaemons/local.factory.orchestrator.plist
  ```

  Runs recorded after the migration are lost from history. Their work isn't: it's on GitHub.

## Stopping it

- To stop new work but keep collecting what's in flight: `factory pause`, then `factory resume`.
- To stop the process: `sudo launchctl bootout system/local.factory.orchestrator`. Runs in flight keep going on Actions, and their files wait there (for 7 days) until it's back.

## Backups

Back up `/Users/SWFactory/.factory/` with Time Machine, or copy the database nightly with SQLite's backup command, which is safe while it runs:

```sh
sqlite3 ~/.factory/factory.db ".backup '/path/to/backups/factory-$(date +%F).db'"
```

Losing the database loses history and attempt counts, not work (see [State](orchestrator.md#state)). The App key can be regenerated from the App's settings page if it's lost; revoke the old one there too.

## Moving to another machine

Stop the job, then copy `factory.db` and `app-key.pem` to the new machine and follow the one-time setup there. Nothing else is stateful: `git/` is rebuilt as needed and logs are just history.
