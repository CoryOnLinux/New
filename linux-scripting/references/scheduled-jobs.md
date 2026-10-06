# Scheduled and unattended jobs

Read for anything run by a timer, cron, udev, a hook or a service: no terminal, nobody watching, and a different environment from the shell where it was tested.

## What's different when nobody is watching

- **PATH is short.** The systemd user manager doesn't include `~/.local/bin`; cron gives `/usr/bin:/bin`. Use absolute paths for your own scripts, or set PATH at the top.
- **No terminal**: no prompts, no `read`, no colours (check `[[ -t 2 ]]`), no `sudo` password. Anything that needs root belongs in a system unit, not in `sudo` inside a user job.
- **No desktop session variables** (`DISPLAY`, `DBUS_SESSION_BUS_ADDRESS`) in cron. systemd user units have them only after the session has imported them.
- **Output**: under systemd, stderr goes to the journal with timestamps (`journalctl --user -u name`). Don't build your own log file unless asked. Under cron, output is mailed or lost: redirect it.
- **Exit status is the report.** Non-zero marks the unit failed (`systemctl --user --failed`). Exit 0 only when the job did its work or was correctly skipped.
- **Runs overlap or get missed.** A laptop is off at 03:00, a job runs longer than its interval. `Persistent=true` catches up on missed runs. systemd won't start a oneshot that is still running, but use `flock` anyway for manual runs.

## systemd user timer (the default on Arch/CachyOS)

```ini
# ~/.config/systemd/user/backup.service
[Unit]
Description=Snapshot Documents and Projects to the Backup drive
# Not mounted: the run is skipped, which isn't a failure.
ConditionPathIsMountPoint=/run/media/%u/Backup

[Service]
Type=oneshot
ExecStart=%h/.local/bin/backup
Nice=10
IOSchedulingClass=idle
```
```ini
# ~/.config/systemd/user/backup.timer
[Unit]
Description=Nightly backup

[Timer]
OnCalendar=*-*-* 02:30
Persistent=true
RandomizedDelaySec=10min

[Install]
WantedBy=timers.target
```
Run (fish):
```fish
systemctl --user daemon-reload
systemctl --user enable --now backup.timer
systemctl --user list-timers backup.timer
systemctl --user start backup.service; journalctl --user -u backup -e   # test run now
loginctl enable-linger $USER   # only if it must run while logged out
```
- User timers stop when the user's last session ends, unless lingering is enabled. Mention it whenever the job must run overnight.
- Check calendar expressions with `systemd-analyze calendar '*-*-* 02:30'` and unit files with `systemd-analyze verify`.
- `%h` is the home directory and `%u` the user name. Don't write `/home/name` into units.
- Root jobs: system units in `/etc/systemd/system`, enabled without `--user`.

## Removable and network targets

- udisks mounts drives at `/run/media/$USER/LABEL`. When the drive is unplugged, that path is gone or is an empty folder on the root filesystem.
- **Never `mkdir -p` the destination of a removable drive.** The job would then fill the root filesystem, and rotation would prune the wrong place. Check first, then write:
  ```bash
  mountpoint -q -- "$dest_root" || { log "backup drive not mounted, skipping"; exit 0; }
  ```
  Keep this even with `ConditionPathIsMountPoint=`: the script can also be run by hand.
- For NAS shares, also check that the share answers (`timeout 10 stat -- "$dest_root/.marker"`), so a hung mount fails fast.

## Snapshot backups

- Use `rsync -a --delete --link-dest="$prev" "$src/" "$dest/.incomplete-$stamp/"`, then `mv` it to `$stamp` only when rsync exits 0. Unchanged files become hard links: each snapshot looks complete but only changes take space.
- rsync exit 24 ("some files vanished") is normal for live folders. Treat it as success with a warning. Treat other non-zero codes as failure.
- Prune only after a successful snapshot. Only delete names that match your own stamp pattern, keep the newest N, and never prune when there is nothing new.
- Use a stamp that sorts and doesn't collide: `date +%Y-%m-%d_%H%M%S`. Several runs on one day must not overwrite each other.
- Track the newest snapshot by listing the names, or with a `latest` symlink updated after the `mv`.
- `-a` copies symlinks as symlinks rather than following them. Add `-HAX` when hard links, ACLs and xattrs matter (the target filesystem has to support them).
- exFAT and FAT targets can't hold hard links, symlinks, permissions or `:` in names. Detect the filesystem type (`findmnt -no FSTYPE -- "$dest_root"`) and refuse or warn.

## cron (if the user really wants it)

- On Arch: `sudo pacman -S --needed cronie; sudo systemctl enable --now cronie`.
- `crontab -e` lines: `30 2 * * * /home/me/.local/bin/backup`. Escape `%` as `\%`. Set `PATH=` and `MAILTO=` at the top.
- There is no catch-up for missed runs (use anacron or a systemd timer for that).
