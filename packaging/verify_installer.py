"""Local install/upgrade/start/uninstall smoke test; uses a dedicated temp folder.
Does not delete application data. Creates per-user installer registration briefly.
"""
from pathlib import Path
import hashlib
import shutil
import subprocess
import sys
import tempfile
import winreg

# Never overwrite an existing installation's registration during a smoke test.
key_name = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\{CC940730-F218-4698-B2E9-C22034804794}_is1'
for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | view):
                raise RuntimeError('Existing yhShell installation found; do not run installer smoke test on this profile.')
        except FileNotFoundError:
            pass

root = Path(__file__).resolve().parent.parent
stage = Path(tempfile.mkdtemp(prefix='yhShell-installer-test-'))
sys.path.insert(0, str(root))
from yhshell import __version__
source = root / 'dist' / f'yhShell-Setup-{__version__}-windows-x64.exe'
setup = stage / source.name
shutil.copyfile(source, setup)
target = stage / 'app'

def run(args):
    print('RUN', args, flush=True)
    result = subprocess.run(args, timeout=90)
    if result.returncode:
        raise RuntimeError(f'Exit {result.returncode}: {args}')

run([str(setup), '/SP-', '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
     '/NOICONS', '/NOCLOSEAPPLICATIONS', '/DIR=' + str(target), '/LOG=' + str(stage/'install.log')])
exe = target / 'yhShell.exe'
assert exe.is_file()
assert hashlib.sha256(exe.read_bytes()).digest() == hashlib.sha256((root/'dist/yhShell/yhShell.exe').read_bytes()).digest()
run([sys.executable, str(root/'packaging/smoke.py'), str(exe)])
# Install the identical version again to exercise replacement/upgrade behavior.
run([str(setup), '/SP-', '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
     '/NOICONS', '/NOCLOSEAPPLICATIONS', '/DIR=' + str(target), '/LOG=' + str(stage/'upgrade.log')])
run([str(target/'unins000.exe'), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
     '/LOG=' + str(stage/'uninstall.log')])
assert not exe.exists(), 'uninstall left application executable'
print('PASS install, native startup, same-version replacement, uninstall', flush=True)
print('Logs retained:', stage, flush=True)
