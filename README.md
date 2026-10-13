# usbvault

Keeps `~/Documents` and `~/Downloads` of a Mac, an Ubuntu PC and an
EndeavourOS laptop together on one USB drive named **VAULT**, and keeps them
tidy. **It never deletes anything.** Deleting is something only you do, by hand.

```
  Mac ──sends──▶  VAULT (USB, exFAT)  ◀──two-way──▶  Ubuntu PC
                                      ◀──two-way──▶  EndeavourOS laptop
```

- **Mac:** sends its files to the USB. The first run backs up everything.
- **Ubuntu and EndeavourOS:** the first run merges each computer with the USB;
  after that, their `~/Documents` and `~/Downloads` stay copies of the USB
  with the same folder structure. Together they are the backup if the USB is
  lost.
- **Pictures** is never copied, on any OS.

## The rules (the same on every computer)

| What | Where it goes |
|---|---|
| New files and folders (decided by name + extension) | Copied, same folder structure |
| A file edited on one side | The newest version wins; the older one is **kept** in `.vault/versions/` (USB) or `~/Documents/TimeIsRelative/versions/` (computer) |
| Files not used (opened or modified) for 6 months | Moved to `~/Documents/FreezeSolo/<original path>`; the folders stay |
| Identical content (checked by SHA-256, whatever the name) | The **newest** copy stays where it is; older copies move to `~/Documents/AttackOfTheClones/<original path>`, listed in `AttackOfTheClones/duplicates.csv` |
| DICOM studies (`.dcm` or the DICOM signature inside the file) | The whole study folder moves to `~/Documents/OldWorld/` and is **never copied** |
| `.mkv .mp4 .mov .avi .webm .iso`, installers (`.dmg .pkg .mpkg .app .deb .rpm .AppImage .snap .flatpak .run .exe .msi`), archives over 1 GB | Never copied; they stay where they are |
| `~/Documents/code` and every git repository | Left alone: not copied, not moved |
| Logs, the local database and a copy of the USB database | `~/Documents/TimeIsRelative/` on every computer, never copied |

These rules have some details worth knowing:

- **"Used"** means the latest of: last opened, last modified, and on the Mac
  Finder's "Last opened" date. The program's own reading never counts.
- **Fresh arrivals aren't frozen.** A file that appears after setup gets 6
  months before it can be frozen, even if its dates are old (e.g. unzipped
  files).
- **Moves are repeated everywhere.** When one computer moves a file into
  FreezeSolo or AttackOfTheClones, the USB and the Linux computers make the
  same move, so the file doesn't come back to its old place.
- **Deleting by hand on Linux** is respected: a file you delete there is marked
  as deleted and not copied back. It stays on the USB and the other computers
  until you delete it there too.

## The daemon

Every computer runs `usbvault.py watch` in the background, every 60 seconds:

- **USB not plugged in:** it keeps logging, records new files, and does the
  FreezeSolo, AttackOfTheClones and OldWorld moves on this computer. Anything
  that also has to happen on the USB is queued.
- **VAULT plugged in:** it repeats the queued moves on the USB, copies the new
  files, updates the USB's database, and refreshes the database copy in
  TimeIsRelative.
- **Desktop notifications** appear whenever something was moved or copied.

## Install (each computer)

1. Put this repo at `~/Documents/code/back-to-the-future-unix-base/` (the same
   path on all three; it is inside `code`, so it is never copied).
2. Format the USB as **exFAT** and name it **VAULT** (Disk Utility on the Mac,
   Disks on Ubuntu). This erases the drive. EndeavourOS also needs
   `sudo pacman -S exfatprogs`.
3. Run setup. It creates the `usbvault` conda environment from
   `environment.yml` (Python 3.12, standard library only) and installs the
   daemon: launchd on macOS, a systemd user service on Ubuntu and EndeavourOS.

   ```bash
   ./setup.sh
   ```

4. **Mac only, once:** open System Settings → Privacy & Security → Full Disk
   Access and add the conda Python that setup prints. Background programs need
   it to read Documents and Downloads and to write to the USB.
5. **EndeavourOS only:** make sure the desktop auto-mounts USB drives (KDE:
   Disks & Cameras → Device Auto-Mount; XFCE: Removable Drives and Media).

## First run

The **Mac goes first**. With the environment active (`conda activate usbvault`)
and VAULT plugged in:

```bash
python usbvault.py init /Volumes/VAULT
```

```bash
python usbvault.py first-run --dry-run
```

```bash
python usbvault.py first-run
```

`--dry-run` lists everything it would move and copy, and changes nothing. The
real run asks for a "yes" before moving anything. On the Mac it goes in this
order: DICOM → OldWorld, 6-month-old files → FreezeSolo, older duplicates →
AttackOfTheClones, then the copy to the USB.

Then on **Ubuntu** and on **EndeavourOS**, with VAULT plugged in (it mounts at
`/media/$USER/VAULT` or `/run/media/$USER/VAULT`):

```bash
python usbvault.py first-run --dry-run
```

```bash
python usbvault.py first-run
```

That merges both ways: the computer receives what's on the USB, and the USB
receives what's only on the computer. The daemon does nothing on a computer
until its first run is done.

## Other commands

```bash
python usbvault.py status      # this computer, what's queued for the USB, the USB catalogue
python usbvault.py sync        # one daemon pass, by hand
python usbvault.py verify      # re-read every file on the USB and compare checksums
python usbvault.py config      # show the settings (~/.config/usbvault/config.json)
python usbvault.py install-daemon --remove   # stop the daemon (its file is kept, renamed)
```

Logs are in `~/Documents/TimeIsRelative/logs/`. The daemon's own output goes to
`~/Library/Logs/usbvault-watch.log` (Mac) or `journalctl --user -u usbvault-watch -f` (Linux).

## The databases (SQLite)

They only **add and update** rows: a trigger in each database refuses `DELETE`.
A removal is recorded as a mark (`deleted_at`, `state = 'deleted'`). Flexible
metadata (dates, size, type, permissions, which computer) is stored as JSON in
a `metadata` column, which you can query with `json_extract`.

| Database | Tables |
|---|---|
| USB: `VAULT/.vault/catalog.sqlite` | `files` (every file on the USB + SHA-256 + metadata), `sources` (each computer's files), `moves`, `duplicates`, `events` |
| Each computer: `~/Documents/TimeIsRelative/local.sqlite` | `files` (state, metadata, state at last sync), `hashes`, `moves` (queued for the USB), `applied`, `duplicates`, `events` |
| Each computer: `TimeIsRelative/vault-catalog-backup.sqlite` | Copy of the USB catalogue, refreshed at every sync |
| Each computer: `TimeIsRelative/catalog.jsonl` | The USB catalogue as JSON, one file per line |

For example, to list every duplicate found so far:

```bash
sqlite3 /Volumes/VAULT/.vault/catalog.sqlite "SELECT found, host, kept, clone FROM duplicates"
```

## Lost or stolen USB

Format a new USB as exFAT named VAULT, then on Ubuntu or EndeavourOS (they hold
everything):

```bash
python usbvault.py init /media/$USER/VAULT
```

```bash
python usbvault.py sync
```

## Good to know

- **Always eject the USB** before unplugging it. exFAT has no journal.
- **The only files the program ever removes** are its own half-written
  temporary copies (`.vaulttmp-*`). Your files are never removed.
- **It rewrites its own generated files** (`duplicates.csv`, `catalog.jsonl`,
  the database backup) with newer versions.
- **Names exFAT refuses** (`: ? * " < > |`) are stored on the USB with `_`
  instead.
- **Encryption:** an exFAT USB is readable by anyone who finds it, and your
  Documents include IDs, FIEL files and contracts. Consider a VeraCrypt
  container.

## Tests

The tests use fake computers and a fake USB in temporary folders, never your
real files.

```bash
python -m unittest discover tests -v
```
