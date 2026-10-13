"""End-to-end tests with fake computers and a fake USB drive in temp folders.

Each "computer" has its own home, settings and name; the USB is a folder named
VAULT under a fake mount root, so it can be "unplugged" by renaming it.
Nothing here touches your real files or a real USB drive.

Run with:  python -m unittest discover tests -v
"""
import ast
import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "usbvault.py"
DAY = 86400


class Computer:
    def __init__(self, tmp: Path, name: str, role: str):
        self.name = name
        self.home = tmp / name
        self.docs = self.home / "Documents"
        self.downloads = self.home / "Downloads"
        for d in (self.docs, self.downloads, self.home / "Pictures"):
            d.mkdir(parents=True)
        self.config = tmp / f"{name}-config.json"
        self.config.write_text(json.dumps({
            "config_version": 3, "role": role, "settle_seconds": 0,
            "folders": {"Documents": str(self.docs), "Downloads": str(self.downloads)},
            "exclude_paths": [str(self.docs / "code")],
        }))
        self.env = dict(os.environ, HOME=str(self.home), USBVAULT_CONFIG=str(self.config),
                        USBVAULT_HOST=name, USBVAULT_MOUNT_ROOTS=str(tmp / "mnt"),
                        XDG_CONFIG_HOME=str(self.home / ".config"))

    def write(self, rel, data, age_days=None):
        p = self.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            p.write_bytes(data)
        else:
            p.write_text(data)
        if age_days is not None:
            t = time.time() - age_days * DAY
            os.utime(p, (t, t))
        return p

    def run(self, *args, mount_root=None):
        env = dict(self.env)
        if mount_root is not None:
            env["USBVAULT_MOUNT_ROOTS"] = str(mount_root)
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def ok(self, *args, **kw):
        r = self.run(*args, **kw)
        assert r.returncode == 0, f"{args} failed:\n{r.stdout}\n{r.stderr}"
        return r.stdout

    def exists(self, rel):
        return (self.home / rel).exists()

    def read(self, rel):
        return (self.home / rel).read_text()


def dicom(n: int) -> bytes:
    return b"\0" * 128 + b"DICM" + f"study image {n}".encode()


class VaultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="usbvault-test-"))
        self.mnt = self.tmp / "mnt"
        self.usb = self.mnt / "VAULT"
        self.usb.mkdir(parents=True)
        self.empty_mnt = self.tmp / "nothing-plugged-in"
        self.empty_mnt.mkdir()
        self.mac = Computer(self.tmp, "mac", "source")
        self.linux = Computer(self.tmp, "ubuntu", "replica")
        self.mac.ok("init", str(self.usb), "--yes")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def on_usb(self, rel):
        return (self.usb / rel).exists()

    def usb_csv(self):
        with open(self.usb / "Documents/AttackOfTheClones/duplicates.csv", encoding="utf-8-sig") as f:
            return list(csv.reader(f))[1:]

    def mac_files(self):
        m = self.mac
        m.write("Documents/cv.pdf", "my cv")
        m.write("Documents/Facturas/2025/enero.xml", "invoice 2025")
        m.write("Documents/Facturas/2026/enero.xml", "invoice 2026")  # same name, different content
        m.write("Documents/old/report.pdf", "old report", age_days=400)
        m.write("Documents/Contratos/contrato.pdf", "contract", age_days=20)
        time.sleep(1.1)  # so the download is clearly newer
        m.write("Downloads/contrato (1).pdf", "contract", age_days=1)
        for i in range(3):
            m.write(f"Documents/Scans/CT/series1/IM000{i}", dicom(i))
        m.write("Documents/Scans/CT/viewer.txt", "viewer notes")
        m.write("Documents/MCC/a.txt", "a")
        m.write("Documents/MCC/b.txt", "b")
        m.write("Documents/MCC/c.txt", "c")
        m.write("Documents/MCC/scans/sample.dcm", dicom(9))
        m.write("Downloads/movie.mp4", "video")
        m.write("Downloads/setup.dmg", "installer")
        m.write("Downloads/setup.deb", "installer")
        big = m.home / "Downloads/backup.zip"
        with open(big, "wb") as f:
            f.truncate(1_100_000_000)  # sparse: takes no real space
        m.write("Downloads/small.zip", "small archive")
        m.write("Downloads/file.pdf.crdownload", "half downloaded")
        m.write("Documents/code/proj/main.py", "print('code stays')")
        m.write("Documents/thesis/.git/HEAD", "ref")
        m.write("Documents/thesis/tesis.tex", "repo stays")
        m.write("Pictures/visa.jpg", "not copied")

    # ------------------------------------------------------------------ first run

    def test_first_run_preview_changes_nothing(self):
        self.mac_files()
        out = self.mac.ok("first-run", "--dry-run")
        self.assertIn("Preview only", out)
        self.assertIn("Documents/Scans", out)
        self.assertTrue(self.mac.exists("Documents/old/report.pdf"))
        self.assertTrue(self.mac.exists("Documents/Scans/CT/series1/IM0000"))
        self.assertFalse(self.on_usb("Documents/cv.pdf"))
        out = self.mac.ok("watch", "--once")
        self.assertIn("waiting", out)  # the daemon does nothing before first-run

    def test_first_run_on_the_mac(self):
        self.mac_files()
        self.mac.ok("first-run", "--yes")
        m = self.mac
        # DICOM: the whole study folder (mostly DICOM) and only the scans subfolder of MCC
        self.assertTrue(m.exists("Documents/OldWorld/Documents/Scans/CT/series1/IM0001"))
        self.assertTrue(m.exists("Documents/OldWorld/Documents/Scans/CT/viewer.txt"))
        self.assertFalse(m.exists("Documents/Scans"))
        self.assertTrue(m.exists("Documents/OldWorld/Documents/MCC/scans/sample.dcm"))
        self.assertTrue(m.exists("Documents/MCC/a.txt"))
        self.assertFalse(self.on_usb("Documents/OldWorld"))
        self.assertFalse(self.on_usb("Documents/Scans"))
        self.assertFalse(self.on_usb("Documents/MCC/scans"))
        # FreezeSolo: the file moves, its folder stays
        self.assertTrue(m.exists("Documents/FreezeSolo/Documents/old/report.pdf"))
        self.assertTrue(m.exists("Documents/old"))
        self.assertTrue(self.on_usb("Documents/FreezeSolo/Documents/old/report.pdf"))
        # duplicates: the newest copy stays, the older one moves
        self.assertTrue(m.exists("Downloads/contrato (1).pdf"))
        self.assertTrue(m.exists("Documents/AttackOfTheClones/Documents/Contratos/contrato.pdf"))
        self.assertFalse(m.exists("Documents/Contratos/contrato.pdf"))
        self.assertTrue(self.on_usb("Documents/AttackOfTheClones/Documents/Contratos/contrato.pdf"))
        self.assertEqual(len(self.usb_csv()), 1)
        self.assertTrue(m.exists("Documents/AttackOfTheClones/duplicates.csv"))
        # same name, different content: both kept in place
        self.assertTrue(self.on_usb("Documents/Facturas/2025/enero.xml"))
        self.assertTrue(self.on_usb("Documents/Facturas/2026/enero.xml"))
        # copied with the same structure
        self.assertEqual((self.usb / "Documents/cv.pdf").read_text(), "my cv")
        self.assertTrue(self.on_usb("Downloads/small.zip"))
        # never copied, left where they are
        for rel in ("Downloads/movie.mp4", "Downloads/setup.dmg", "Downloads/setup.deb", "Downloads/backup.zip"):
            self.assertFalse(self.on_usb(rel), rel)
            self.assertTrue(m.exists(rel), rel)
        self.assertFalse(self.on_usb("Downloads/file.pdf.crdownload"))
        self.assertFalse(self.on_usb("Documents/code"))
        self.assertFalse(self.on_usb("Documents/thesis"))
        self.assertTrue(m.exists("Documents/thesis/tesis.tex"))
        self.assertFalse(self.on_usb("Pictures"))
        # TimeIsRelative: logs, local database, catalogue backup and export; never copied
        t = m.home / "Documents/TimeIsRelative"
        self.assertTrue((t / "local.sqlite").exists())
        self.assertTrue((t / "vault-catalog-backup.sqlite").exists())
        self.assertTrue((t / "catalog.jsonl").exists())
        self.assertTrue(list((t / "logs").glob("*.log")))
        self.assertFalse(self.on_usb("Documents/TimeIsRelative"))
        line = json.loads((t / "catalog.jsonl").read_text().splitlines()[0])
        self.assertIn("sha256", line["metadata"])

    # ------------------------------------------------------------------- daemon

    def test_daemon_offline_then_online(self):
        self.mac.write("Documents/cv.pdf", "my cv")
        self.mac.ok("first-run", "--yes")
        # USB unplugged: the daemon logs and tidies, and remembers what to do on the USB
        self.mac.write("Downloads/cv-copy.pdf", "my cv")
        self.mac.write("Documents/new.txt", "brand new")
        out = self.mac.ok("watch", "--once", mount_root=self.empty_mnt)
        self.assertIn("not plugged in", out)
        self.assertTrue(self.mac.exists("Downloads/cv-copy.pdf"))  # the new file stays
        self.assertTrue(self.mac.exists("Documents/AttackOfTheClones/Documents/cv.pdf"))  # old copy moved
        self.assertTrue(self.on_usb("Documents/cv.pdf"))  # USB untouched while unplugged
        self.assertFalse(self.on_usb("Documents/new.txt"))
        # plugged in again
        self.mac.ok("watch", "--once")
        self.assertFalse(self.on_usb("Documents/cv.pdf"))
        self.assertTrue(self.on_usb("Documents/AttackOfTheClones/Documents/cv.pdf"))
        self.assertTrue(self.on_usb("Downloads/cv-copy.pdf"))
        self.assertTrue(self.on_usb("Documents/new.txt"))
        self.assertEqual(len(self.usb_csv()), 1)
        out = self.mac.ok("watch", "--once")
        self.assertNotIn("to the USB", out)  # nothing new: quiet

    def test_daemon_freezes_old_files_but_not_new_arrivals(self):
        self.mac.write("Documents/notes.txt", "notes")
        self.mac.ok("first-run", "--yes")
        t = time.time() - 300 * DAY
        os.utime(self.mac.home / "Documents/notes.txt", (t, t))  # not used for months
        self.mac.write("Downloads/unzipped-old.txt", "came out of an old zip", age_days=300)
        self.mac.ok("watch", "--once")
        self.assertTrue(self.mac.exists("Documents/FreezeSolo/Documents/notes.txt"))
        self.assertTrue(self.on_usb("Documents/FreezeSolo/Documents/notes.txt"))
        self.assertFalse(self.on_usb("Documents/notes.txt"))
        self.assertTrue(self.mac.exists("Downloads/unzipped-old.txt"))  # just arrived: not frozen

    def test_idle_pass_writes_nothing_to_the_usb(self):
        self.mac.write("Documents/a.txt", "a")
        self.mac.write("Documents/b.txt", "a")  # a duplicate, so the CSV exists
        self.mac.ok("first-run", "--yes")
        self.mac.ok("watch", "--once")

        def snapshot():
            return {p: p.stat().st_mtime_ns for p in self.usb.rglob("*")}
        before = snapshot()
        time.sleep(0.05)
        self.mac.ok("watch", "--once")
        after = snapshot()
        self.assertEqual({str(p.relative_to(self.usb)) for p in before if before[p] != after.get(p)}, set())

    def test_usb_found_by_its_name(self):
        self.mac.write("Documents/a.txt", "a")
        self.mac.ok("first-run", "--yes")
        out = self.mac.ok("watch", "--once")
        self.assertIn("connected", out)
        self.usb.rename(self.mnt / "OTHER")
        out = self.mac.ok("watch", "--once")
        self.assertIn("named VAULT is not plugged in", out)

    # -------------------------------------------------------------------- linux

    def test_linux_first_run_merges_both_ways(self):
        self.mac.write("Documents/cv.pdf", "my cv")
        self.mac.write("Documents/shared.txt", "mac version", age_days=10)
        self.mac.ok("first-run", "--yes")
        self.linux.write("Documents/linux-only.txt", "from linux")
        self.linux.write("Documents/shared.txt", "linux version, newer and longer", age_days=1)
        self.linux.ok("first-run", "--yes")
        self.assertEqual(self.linux.read("Documents/cv.pdf"), "my cv")
        self.assertTrue(self.on_usb("Documents/linux-only.txt"))
        self.assertEqual((self.usb / "Documents/shared.txt").read_text(), "linux version, newer and longer")
        old = list((self.usb / ".vault/versions").rglob("shared.txt"))
        self.assertEqual([p.read_text() for p in old], ["mac version"])  # kept, not overwritten

    def test_linux_deleted_file_is_not_brought_back(self):
        self.mac.write("Documents/cv.pdf", "my cv")
        self.mac.ok("first-run", "--yes")
        self.linux.ok("first-run", "--yes")
        (self.linux.home / "Documents/cv.pdf").unlink()  # you delete it by hand
        self.linux.ok("sync")
        self.assertFalse(self.linux.exists("Documents/cv.pdf"))
        self.assertTrue(self.on_usb("Documents/cv.pdf"))  # and nothing is deleted on the USB
        db = sqlite3.connect(self.linux.home / "Documents/TimeIsRelative/local.sqlite")
        state = db.execute("SELECT state, deleted_at FROM files WHERE path='Documents/cv.pdf'").fetchone()
        self.assertEqual(state[0], "deleted")
        self.assertIsNotNone(state[1])

    def test_edits_on_linux_reach_the_usb_and_keep_the_old_version(self):
        self.mac.write("Documents/plan.txt", "v1")
        self.mac.ok("first-run", "--yes")
        self.linux.ok("first-run", "--yes")
        time.sleep(1.1)
        self.linux.write("Documents/plan.txt", "v2 edited on linux")
        self.linux.ok("sync")
        self.assertEqual((self.usb / "Documents/plan.txt").read_text(), "v2 edited on linux")
        self.assertEqual([x.read_text() for x in (self.usb / ".vault/versions").rglob("plan.txt")], ["v1"])

    def test_moves_from_the_mac_are_repeated_on_linux(self):
        self.mac.write("Documents/cv.pdf", "my cv")
        self.mac.ok("first-run", "--yes")
        self.linux.ok("first-run", "--yes")
        self.mac.write("Downloads/cv (2).pdf", "my cv")
        self.mac.ok("sync")
        self.linux.ok("sync")
        self.assertFalse(self.linux.exists("Documents/cv.pdf"))
        self.assertTrue(self.linux.exists("Documents/AttackOfTheClones/Documents/cv.pdf"))
        self.assertTrue(self.linux.exists("Downloads/cv (2).pdf"))
        self.linux.ok("sync")  # stable: nothing comes back
        self.assertFalse(self.linux.exists("Documents/cv.pdf"))

    def test_linux_duplicate_of_a_usb_file(self):
        self.mac.write("Documents/manual.pdf", "the manual", age_days=30)
        self.mac.ok("first-run", "--yes")
        self.linux.ok("first-run", "--yes")
        self.linux.write("Downloads/manual-again.pdf", "the manual")
        self.linux.ok("sync")
        self.assertTrue(self.linux.exists("Downloads/manual-again.pdf"))
        self.assertTrue(self.linux.exists("Documents/AttackOfTheClones/Documents/manual.pdf"))
        self.assertTrue(self.on_usb("Documents/AttackOfTheClones/Documents/manual.pdf"))
        self.assertFalse(self.on_usb("Documents/manual.pdf"))

    # --------------------------------------------------------------- no deletes

    def test_the_program_never_deletes_files(self):
        tree = ast.parse(SCRIPT.read_text())
        forbidden = {"unlink", "rmtree", "rmdir", "removedirs"}  # on any object (os, shutil, Path)
        offenders = []
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef):
                for node in ast.walk(fn):
                    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                        continue
                    name = node.func.attr
                    owner = node.func.value.id if isinstance(node.func.value, ast.Name) else ""
                    if (name in forbidden or (name == "remove" and owner in ("os", "shutil"))) \
                            and fn.name != "discard_temp":
                        offenders.append(f"{fn.name} calls {name}")
        self.assertEqual(offenders, [])

    def test_databases_refuse_deletes(self):
        self.mac.write("Documents/a.txt", "a")
        self.mac.ok("first-run", "--yes")
        for path in (self.usb / ".vault/catalog.sqlite", self.mac.home / "Documents/TimeIsRelative/local.sqlite"):
            db = sqlite3.connect(path)
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("DELETE FROM files")
            db.close()

    def test_every_original_still_exists_somewhere(self):
        self.mac_files()
        before = {p.read_bytes() for p in self.mac.home.rglob("*")
                  if p.is_file() and p.stat().st_size < 10_000_000 and "TimeIsRelative" not in str(p)}
        self.mac.ok("first-run", "--yes")
        after = {p.read_bytes() for p in self.mac.home.rglob("*")
                 if p.is_file() and p.stat().st_size < 10_000_000}
        self.assertTrue(before <= after)

    # ----------------------------------------------------------------- the rest

    def test_verify_detects_corruption(self):
        self.mac.write("Documents/a.txt", "abcdef")
        self.mac.ok("first-run", "--yes")
        self.mac.ok("verify")
        p = self.usb / "Documents/a.txt"
        st = p.stat()
        p.write_text("abcdeX")
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
        r = self.mac.run("verify")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Documents/a.txt", r.stdout)

    def test_status(self):
        self.mac.write("Documents/a.txt", "a")
        self.mac.ok("first-run", "--yes")
        out = self.mac.ok("status")
        self.assertIn("role source", out)
        self.assertIn("catalogue: 1 files", out)

    def test_install_daemon_dry_run(self):
        out = self.mac.ok("install-daemon", "--dry-run", "--python", sys.executable)
        self.assertIn(str(SCRIPT), out)
        self.assertIn("watch", out)

    def test_install_daemon_refuses_the_usb_copy(self):
        r = subprocess.run([sys.executable, str(self.usb / "usbvault.py"), "install-daemon", "--dry-run",
                            "--python", sys.executable], env=self.mac.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("unplugged", r.stderr)


if __name__ == "__main__":
    unittest.main()
