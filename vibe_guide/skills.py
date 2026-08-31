from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile


_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_GITHUB_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
_SCP_SOURCE = re.compile(
    r"^(?P<userinfo>[^@/\s]+)@(?P<host>[^:/\s]+):(?P<path>[^?#]+)$"
)


class _InstallError(Exception):
    pass


@dataclass
class SkillSpec:
    name: str
    source: str
    commit: str


@dataclass
class SkillInstallResult:
    status: str
    installed: bool
    source: str
    commit: str


def _github_path(path):
    clean = path.strip('/')
    if clean.endswith('.git'):
        clean = clean[:-4]
    parts = clean.split('/')
    if (
        len(parts) != 2
        or any(part in ('', '.', '..') for part in parts)
        or not all(_GITHUB_PART.fullmatch(part) for part in parts)
    ):
        raise ValueError('invalid GitHub repository path')
    return parts


def normalize_github_source(source, reject_credentials=True):
    """Return one credential-free identity for supported GitHub Git URLs."""
    value = source.strip() if isinstance(source, str) else ''
    scp = _SCP_SOURCE.fullmatch(value)
    if scp:
        if scp.group('host').lower() != 'github.com':
            raise ValueError('unsupported Git host')
        if reject_credentials and scp.group('userinfo') != 'git':
            raise ValueError('credential-bearing Git URL')
        owner, repository = _github_path(scp.group('path'))
        return 'https://github.com/{}/{}'.format(owner, repository)

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError('invalid Git URL')
    if parsed.scheme.lower() not in ('https', 'ssh'):
        raise ValueError('unsupported Git URL scheme')
    if (parsed.hostname or '').lower() != 'github.com':
        raise ValueError('unsupported Git host')
    if parsed.query or parsed.fragment or port is not None:
        raise ValueError('unsupported Git URL component')
    if reject_credentials:
        if parsed.password is not None:
            raise ValueError('credential-bearing Git URL')
        if parsed.scheme.lower() == 'https' and parsed.username is not None:
            raise ValueError('credential-bearing Git URL')
        if parsed.scheme.lower() == 'ssh' and parsed.username not in (None, 'git'):
            raise ValueError('credential-bearing Git URL')
    owner, repository = _github_path(parsed.path)
    return 'https://github.com/{}/{}'.format(owner, repository)


def sanitize_git_url_for_display(source):
    """Remove userinfo/query material before a Git source crosses a report boundary."""
    try:
        return normalize_github_source(source)
    except (AttributeError, ValueError):
        pass
    try:
        return normalize_github_source(source, reject_credentials=False)
    except (AttributeError, ValueError):
        pass

    value = source.strip() if isinstance(source, str) else ''
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        parsed = None
        port = None
    if parsed and parsed.scheme and parsed.hostname:
        host = parsed.hostname.lower()
        if port is not None:
            host += ':{}'.format(port)
        path = parsed.path or ''
        return '{}://{}{}'.format(parsed.scheme.lower(), host, path)

    scp = _SCP_SOURCE.fullmatch(value)
    if scp:
        return '{}:{}'.format(scp.group('host').lower(), scp.group('path'))
    if value.startswith(('/', './', '../', '~')):
        return '<local-path>'
    return '<invalid-source>'


def _git(repo, *args):
    environment = os.environ.copy()
    environment['GIT_TERMINAL_PROMPT'] = '0'
    try:
        completed = subprocess.run(
            ['git', '-C', str(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        raise _InstallError()
    if completed.returncode != 0:
        raise _InstallError()
    return completed.stdout.strip()


def _clone_vendor(source, vendor, requested_sha):
    vendor.parent.mkdir(parents=True, exist_ok=True)
    holder = Path(tempfile.mkdtemp(prefix='.clone-', dir=str(vendor.parent)))
    checkout = holder / 'repository'
    environment = os.environ.copy()
    environment['GIT_TERMINAL_PROMPT'] = '0'
    try:
        completed = subprocess.run(
            [
                'git', 'clone', '--no-checkout', '--origin', 'origin',
                source, str(checkout),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env=environment,
        )
        if completed.returncode != 0:
            raise _InstallError()
        _git(checkout, 'fetch', '--no-tags', 'origin', requested_sha)
        os.replace(str(checkout), str(vendor))
    except (OSError, subprocess.SubprocessError):
        raise _InstallError()
    finally:
        shutil.rmtree(holder, ignore_errors=True)


def _verify_vendor(vendor, source, requested_sha, fetch):
    if vendor.is_symlink() or not vendor.is_dir():
        raise _InstallError()
    try:
        origin = normalize_github_source(
            _git(vendor, 'config', '--get', 'remote.origin.url')
        )
    except ValueError:
        raise _InstallError()
    if origin != source:
        raise _InstallError()
    if fetch:
        _git(vendor, 'fetch', '--no-tags', 'origin', requested_sha)
    actual = _git(vendor, 'rev-parse', '--verify', requested_sha + '^{commit}')
    if actual.lower() != requested_sha:
        raise _InstallError()
    tree = _git(vendor, 'rev-parse', '--verify', requested_sha + '^{tree}')
    if not _FULL_SHA.fullmatch(tree):
        raise _InstallError()
    return actual.lower(), tree.lower()


def _safe_archive_members(archive):
    members = archive.getmembers()
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or '..' in path.parts:
            raise _InstallError()
        if not member.isdir() and not member.isreg():
            raise _InstallError()
    return members


def _materialize_commit(vendor, commit, stage):
    environment = os.environ.copy()
    environment['GIT_TERMINAL_PROMPT'] = '0'
    try:
        completed = subprocess.run(
            ['git', '-C', str(vendor), 'archive', '--format=tar', commit],
            check=False,
            capture_output=True,
            timeout=60,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        raise _InstallError()
    if completed.returncode != 0:
        raise _InstallError()
    try:
        with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode='r:') as archive:
            members = _safe_archive_members(archive)
            for member in members:
                destination = stage.joinpath(*PurePosixPath(member.name).parts)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                source_file = archive.extractfile(member)
                if source_file is None:
                    raise _InstallError()
                with source_file, destination.open('xb') as output:
                    shutil.copyfileobj(source_file, output)
                destination.chmod(0o755 if member.mode & 0o111 else 0o644)
    except (OSError, tarfile.TarError):
        raise _InstallError()
    manifest = stage / 'SKILL.md'
    if manifest.is_symlink() or not manifest.is_file():
        raise _InstallError()


def _materialized_tree_sha256(root):
    digest = hashlib.sha256()
    for path in sorted(
        root.rglob('*'), key=lambda item: item.relative_to(root).as_posix()
    ):
        relative = path.relative_to(root).as_posix().encode('utf-8')
        if path.is_dir():
            digest.update(b'D\0' + relative + b'\0')
        elif path.is_file() and not path.is_symlink():
            digest.update(b'F\0' + relative + b'\0')
            with path.open('rb') as handle:
                for chunk in iter(lambda: handle.read(64 * 1024), b''):
                    digest.update(chunk)
        else:
            raise _InstallError()
    return digest.hexdigest()


def _lexists(path):
    return os.path.lexists(str(path))


def _write_record_atomic(path, record):
    descriptor, temporary = tempfile.mkstemp(
        prefix='.' + path.name + '-', dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(record, handle, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, str(path))
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            Path(temporary).unlink()
        except OSError:
            pass
        raise


def _remove_published_path(path):
    if not _lexists(path):
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def install_skill(spec, vibe_home, fetch=False):
    safe_source = sanitize_git_url_for_display(spec.source)
    safe_commit = spec.commit.lower() if _FULL_SHA.fullmatch(spec.commit or '') else ''
    record_created = False
    publish_attempted = False
    stage = None
    target = None
    record_path = None
    try:
        if not _NAME.fullmatch(spec.name or '') or spec.name in ('.', '..'):
            raise _InstallError()
        source = normalize_github_source(spec.source)
        if not _FULL_SHA.fullmatch(spec.commit or ''):
            raise _InstallError()
        requested_sha = spec.commit.lower()

        home = Path(vibe_home).resolve()
        vendor = home / 'vendor' / spec.name
        skills_root = home / 'skills'
        target = skills_root / spec.name
        record_path = skills_root / (spec.name + '.json')
        if skills_root.exists() and (
            skills_root.is_symlink() or not skills_root.is_dir()
        ):
            raise _InstallError()
        skills_root.mkdir(parents=True, exist_ok=True)
        if _lexists(target) or _lexists(record_path):
            raise _InstallError()

        if not _lexists(vendor):
            if not fetch:
                raise _InstallError()
            _clone_vendor(source, vendor, requested_sha)
        actual, tree = _verify_vendor(vendor, source, requested_sha, fetch)

        stage = Path(
            tempfile.mkdtemp(prefix='.' + spec.name + '-', dir=str(skills_root))
        )
        _materialize_commit(vendor, actual, stage)
        installed_tree = _materialized_tree_sha256(stage)
        record = {
            'source': source,
            'sha': actual,
            'tree': tree,
            'installed_tree_sha256': installed_tree,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'validation': 'verified',
        }
        _write_record_atomic(record_path, record)
        record_created = True
        publish_attempted = True
        os.replace(str(stage), str(target))
        stage = None
        return SkillInstallResult('installed', True, source, actual)
    except (
        _InstallError,
        OSError,
        ValueError,
        subprocess.SubprocessError,
        tarfile.TarError,
    ):
        if publish_attempted and target is not None:
            try:
                _remove_published_path(target)
            except OSError:
                pass
        if record_created and record_path is not None:
            try:
                record_path.unlink()
            except OSError:
                pass
        return SkillInstallResult('pending', False, safe_source, safe_commit)
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
