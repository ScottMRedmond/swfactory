# The orchestrator runs on a dedicated Linux mini PC, not the family Mac mini

The orchestrator moves off the family Mac mini to a small Linux PC of its own: a refurbished Lenovo ThinkCentre M80q Tiny (i5-10500T, 32 GB, 512 GB NVMe, with room for a 2.5" SATA SSD later), running Ubuntu Server LTS. The Mac mini runs the family's photo and video system, which works and must not be put at risk, and the factory's GitHub App key doesn't belong on a machine the whole family shares. The PC is also the remote development machine, reached from the laptop with VS Code Remote-SSH, so it's sized for development (Docker, devcontainers, image builds) rather than for the orchestrator, which needs very little; being x86-64, it builds the same `linux/amd64` images that Actions runs. Runners stay on GitHub Actions: no factory run happens on this PC.

## Considered options

- **Stay on the family Mac mini.** Rejected: a shared family machine, with a photo and video system on it that the factory mustn't disturb. The separate `SWFactory` user kept the key from other accounts, but every admin task (users, launchd, sudo, Remote Login) touched the family's machine.
- **The development laptop (M1 MacBook Air, 8 GB).** Rejected: it sleeps whenever the lid is shut, so labels added while away wait until the laptop is open, and 8 GB is already tight for Docker. Still a fine stopgap if the PC is down.
- **A newer mini PC (Ryzen 7000/8000-series).** Not needed: about twice the cost for faster image builds, and nothing the orchestrator needs.

## Consequences

- **Development and the factory share a machine, so they're kept apart by Linux users.** The factory runs as a `swfactory` user whose home is mode 700. The developer's account has no sudo at all, not even with a password, since an agent working as the developer (Claude Code in auto mode, for example) could catch the password by editing the developer's shell startup files. Admin work is done as a separate `admin` account, reached only by SSH from the laptop, so the agent can't become root or `swfactory`.
- **Docker for development is rootless.** Membership of the `docker` group is as good as root: any container can mount `/home/swfactory` and read the App key. So the developer uses rootless Docker, the system Docker daemon is off, and nobody is in the `docker` group.
- **Production and development never share state.** Production runs from `swfactory`'s own checkout at a release tag, with its own database and the only copy of the App key. A development checkout never points at either.
- **Still no runners on this PC.** Agents next to the App key and a developer's workspace are the same risk the Mac mini had with family data. If the factory ever gets a runner on its own hardware (see "Hosted minutes or our own hardware" in `docs/software-factory.md`), it's a separate machine.
- **launchd becomes systemd**, Time Machine becomes an encrypted off-machine copy with restic, and `docs/deploying.md` is rewritten for Linux. The launchd plists are removed.
- **The PC is always on**, so a down orchestrator is now a fault, not a sleep. A dead-man's switch is possible now, though v1 still has none.
