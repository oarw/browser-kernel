"""Preflight and durable evidence for the experimental Windows kernel build."""
import ctypes
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone

GIB = 1024 ** 3
MSVC = '14.44.35207'
SDK = '10.0.26100.0'


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path, value):
    """Publish complete JSON only; preserve the previous report on write failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf8', newline='\n') as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate_work_dir(path, repository):
    root = Path(path).resolve()
    repository = Path(repository).resolve()
    if root == Path(root.anchor) or root == repository or root in repository.parents or repository in root.parents:
        raise ValueError('Use a dedicated directory outside the repository, not a drive root or repository ancestor')
    # The locked upstream recipe uses batch scripts and has long generated paths.
    if len(str(root)) > 60 or any(c in str(root) for c in ' &|<>^%!"\r\n'):
        raise ValueError('Use a short work directory without spaces or shell metacharacters')
    return root


def resource_requirements(phase, jobs, prepared=False):
    if phase not in ('prepare', 'build') or not 1 <= jobs <= 8:
        raise ValueError('Expected phase prepare/build and jobs between 1 and 8')
    return {
        # Fresh trees follow Chromium's documented 100 GB minimum; incremental
        # builds retain 40 GiB headroom. Neither is a promise of peak usage.
        'freeDiskBytes': 40 * GIB if prepared else 100_000_000_000,
        'availableMemoryBytes': (4 if phase == 'prepare' else 6 + 2 * (jobs - 1)) * GIB,
        'totalMemoryBytes': 15 * GIB,
    }


def resource_issues(machine, requirements):
    return [f'{field}: have {machine.get(field, 0) / GIB:.1f} GiB, require {minimum / GIB:.1f} GiB'
            for field, minimum in requirements.items() if machine.get(field, 0) < minimum]


def windows_memory():
    from ctypes import wintypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [('length', wintypes.DWORD), ('load', wintypes.DWORD)] + [
            (name, ctypes.c_ulonglong) for name in
            ('totalPhysical', 'availablePhysical', 'totalPage', 'availablePage', 'totalVirtual', 'availableVirtual', 'extended')]

    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError('GlobalMemoryStatusEx failed')
    return {'totalMemoryBytes': status.totalPhysical, 'availableMemoryBytes': status.availablePhysical}


def windows_toolchain():
    import winreg
    vswhere = Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'Microsoft Visual Studio/Installer/vswhere.exe'
    result = subprocess.run([str(vswhere), '-latest', '-products', '*', '-requires',
                             'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-property', 'installationPath'],
                            check=True, capture_output=True, text=True, encoding='utf8', timeout=30)
    if not result.stdout.strip():
        raise RuntimeError('Visual Studio C++ installation was not found')
    vs = Path(result.stdout.strip())
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows Kits\Installed Roots',
                        0, winreg.KEY_READ | winreg.KEY_WOW64_32KEY) as key:
        sdk = Path(winreg.QueryValueEx(key, 'KitsRoot10')[0])
    required = [vs / 'VC/Auxiliary/Build/vcvars64.bat', vs / f'VC/Tools/MSVC/{MSVC}/bin/Hostx64/x64/cl.exe',
                sdk / f'Include/{SDK}/um/Windows.h', sdk / f'Lib/{SDK}/um/x64/kernel32.lib',
                sdk / f'bin/{SDK}/x64/rc.exe']
    for path in required:
        if not path.is_file():
            raise RuntimeError(f'Missing locked toolchain file: {path}')
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\FileSystem') as key:
        long_paths = winreg.QueryValueEx(key, 'LongPathsEnabled')[0] == 1
    if not long_paths:
        raise RuntimeError('Windows LongPathsEnabled must be enabled for Chromium sources')
    return {'visualStudio': str(vs), 'vcvars': str(required[0]), 'msvc': MSVC, 'sdk': SDK,
            'sdkRoot': str(sdk), 'longPathsEnabled': long_paths}


def preflight(root, phase, jobs, prepared=False):
    """Read-only inspection; does not clone, download or install anything."""
    requirements = resource_requirements(phase, jobs, prepared)
    report = {'schemaVersion': 1, 'checkedAt': now(), 'phase': phase, 'jobs': jobs,
              'workDirectory': str(root), 'requirements': requirements, 'issues': [],
              'machine': {'platform': platform.platform(), 'architecture': platform.machine(), 'cpus': os.cpu_count()},
              'python': {'executable': sys.executable, 'version': platform.python_version()}, 'tools': {}}
    if sys.platform != 'win32' or platform.machine().lower() not in ('amd64', 'x86_64') or sys.maxsize <= 2 ** 32:
        report['issues'].append('Windows x64 with a 64-bit Python interpreter is required')
    if sys.version_info < (3, 12):
        report['issues'].append('Python 3.12 or newer is required')
    for command in ('git', '7z'):
        report['tools'][command] = shutil.which(command)
        if not report['tools'][command]:
            report['issues'].append(f'Missing command: {command}')
    for package, version in [('httplib2', '0.22.0'), ('pyparsing', '3.2.3')]:
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            actual = None
        report['python'][package] = actual
        if actual != version:
            report['issues'].append(f'{package}=={version} is required; installed: {actual}')
    existing = root
    while not existing.exists():
        existing = existing.parent
    report['machine']['freeDiskBytes'] = shutil.disk_usage(existing).free
    if sys.platform == 'win32':
        try:
            report['machine'].update(windows_memory())
            report['toolchain'] = windows_toolchain()
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            report['issues'].append(str(error))
    report['issues'].extend(resource_issues(report['machine'], requirements))
    report['ready'] = not report['issues']
    return report


def validate_prepared(marker, identity, source, files):
    """A marker alone cannot certify a partial or modified build tree."""
    if marker.get('inputs') != identity:
        raise RuntimeError('Prepared tree uses different inputs; use a new work directory')
    recorded = marker.get('preparedFiles', {})
    if set(recorded) != set(files):
        raise RuntimeError('Prepared file manifest is missing or incompatible; use a new work directory')
    for name in files:
        path = source / name
        if not path.is_file() or digest(path) != recorded[name]:
            raise RuntimeError(f'Prepared build file changed or disappeared: {name}')


@contextmanager
def workspace_lock(root):
    path = root / 'build.lock'
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError as error:
        raise RuntimeError(f'Workspace already locked: {path}; inspect its PID before removing a stale lock') from error
    try:
        with os.fdopen(fd, 'w', encoding='utf8') as stream:
            json.dump({'pid': os.getpid(), 'startedAt': now()}, stream)
        yield
    finally:
        path.unlink(missing_ok=True)
