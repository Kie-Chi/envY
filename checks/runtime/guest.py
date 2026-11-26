#!/usr/bin/env python3
"""Closed disposable-guest lifecycle program; importing this module performs no IO.

The host runner is deliberately absent. Only seed_data renders executable guest
text. This file is also the standalone guest payload, with data constants
prepended by guest_program. Never execute it against a host installation.
"""
import base64
import codecs
from contextlib import ExitStack
import errno
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import pty
import pwd
import re
import select
import signal
import stat
import subprocess
import sys
import tarfile
import time
from urllib.parse import quote

HOME = Path('/home/envy-runtime')
REPO = HOME / '.envy'
SEED = Path('/run/envy-runtime-install-seed')
MARKER = Path('/run/envy-runtime-guest-nonce')
SCRIPT = '/usr/local/lib/envy-runtime-guest.py'
CONTEXT = Path('/run/envy-runtime-context.json')
RESULT = HOME / '.local/state/envy-runtime-result.json'
OBSERVATIONS = HOME / '.local/state/envy-runtime-observations.json'
NIX = '/nix/var/nix/profiles/default/bin/nix'
STORE = '/nix/var/nix/profiles/default/bin/nix-store'
PREFIX = 'ENVY_RUNTIME_ACCEPTANCE_RECEIPT '
INSTALL_PREFIX = 'ENVY_RUNTIME_INSTALL_RECEIPT '
ABORT_PREFIX = 'ENVY_RUNTIME_GUEST_ABORT '
OBSERVATIONS_PREFIX = 'ENVY_RUNTIME_GUEST_OBSERVATIONS '
DIAGNOSTIC_PREFIX = 'ENVY_RUNTIME_GUEST_DIAGNOSTIC '
TLS_PREFIX = 'ENVY_RUNTIME_GUEST_PUBLIC_TLS '
NATIVE_TLS_PREFIX = 'ENVY_RUNTIME_GUEST_NATIVE_TLS '
PREFETCH_PREFIX = 'ENVY_RUNTIME_GUEST_INPUT_PREFETCH '
SERIAL_PREFIXES = (INSTALL_PREFIX, OBSERVATIONS_PREFIX, PREFIX, DIAGNOSTIC_PREFIX, TLS_PREFIX, NATIVE_TLS_PREFIX,
                   PREFETCH_PREFIX)
SYSTEM_CA_BUNDLE = '/etc/ssl/certs/ca-certificates.crt'
PUBLIC_TLS_ENDPOINTS = (
    ('nix-cache', 'https://cache.nixos.org/nix-cache-info'),
    ('github-api', 'https://api.github.com/'),
    ('github-codeload', 'https://codeload.github.com/'),
)
MAX_SERIAL = 512 * 1024
ROOT_PHASE = 'root-stages'
ROOT_PHASES = frozenset(('root-stages', 'seed-payload', 'official-install', 'payload-import',
                         'daemon-ca', 'native-tls', 'source-add', 'source-info', 'input-prefetch', 'flake-archive', 'setup-build',
                         'locked-tools', 'user-session', 'user-result'))
NIX_FAILURE_PATTERNS = (
    ('nix-certificate', (rb'certificate (?:verify|verification) failed', rb'self[- ]signed certificate',
                         rb'unable to get (?:local )?issuer certificate', rb'ssl peer certificate')),
    ('nix-dns', (rb'could not resolve (?:host|hostname|proxy)', rb'couldn.t resolve (?:host|hostname)',
                 rb'temporary failure in name resolution', rb'name or service not known')),
    ('nix-github-rate-limit', (rb'api rate limit exceeded', rb'github[^\r\n]{0,120}rate limit')),
    ('nix-http-403', (rb'http(?:/[0-9.]+)?(?: error| status(?: code)?)?[: ]+403\b',)),
    ('nix-http-401', (rb'http(?:/[0-9.]+)?(?: error| status(?: code)?)?[: ]+401\b',)),
    ('nix-http-404', (rb'http(?:/[0-9.]+)?(?: error| status(?: code)?)?[: ]+404\b',)),
    ('nix-connection', (rb'connection (?:refused|reset|timed out)', rb'failed to connect',
                        rb'(?:operation|connection) timeout', rb'timeout was reached', rb'operation timed out')),
    ('nix-hash-mismatch', (rb'hash mismatch', rb'nar hash mismatch')),
    ('nix-unsupported-option', (rb'(?:unknown|unrecogni[sz]ed) (?:flag|option)',)),
    ('nix-missing-git', (rb"(?:executing|execute|start|run)[^\r\n]{0,80}['\"]git['\"][^\r\n]{0,80}(?:no such file|not found)",
                         rb'git: (?:command )?not found')),
    ('nix-evaluation', (rb'syntax error', rb"attribute ['\"][^'\"\r\n]{1,160}['\"] missing")),
)
NIX_FAILURE_CODES = frozenset(code for code, _ in NIX_FAILURE_PATTERNS)
NIX_FAILURE_PHASES = frozenset(('input-prefetch', 'flake-archive', 'setup-build', 'locked-tools'))
MAX_NIX_STDERR = 8192
LAST_NIX_FAILURE = 'unknown'
DIAGNOSTIC_CODES = NIX_FAILURE_CODES | frozenset((
    'unknown', 'cancelled', 'timeout', 'native-source-path', 'native-source-nar',
    'native-info', 'archive-source', 'archive-unapproved-source', 'lock-drift',
    'command-failed', 'command-timeout', 'command-output-limit', 'duplicate-json-key',
    'nonfinite-json', 'closed-data-schema', 'store-path', 'fresh-guest-required',
    'official-install-receipt', 'official-install-incomplete', 'bootstrap-transport',
    'bootstrap-build-outputs', 'bootstrap-not-approved', 'bootstrap-not-realized',
    'bootstrap-not-executable', 'native-object-mismatch', 'import-inventory-mismatch',
    'user-result-binding', 'user-result-transport', 'observations-binding',
    'file-type-or-size', 'file-owner', 'file-mode', 'file-changed', 'payload-digest',
    'serial-prefix', 'serial-uid', 'serial-budget', 'serial-device', 'serial-timeout',
    'serial-write', 'daemon-inactive', 'input-prefetch-lock', 'input-prefetch-source',
    'input-prefetch-result', 'input-prefetch-hash',
    'prefetch-timeout-active-download', 'prefetch-timeout-active-unpack',
    'prefetch-timeout-active-store-add', 'prefetch-timeout-overlap',
    'prefetch-timeout-exit-observed-open-stream', 'prefetch-timeout-unknown',
    'cache-copy-timeout-exit-observed-open-stream', 'cache-copy-timeout-unknown',
    'cache-copy-timeout-stop-observed', 'cache-copy-timeout-unclosed-reported-total',
    'cache-copy-timeout-unclosed-progress', 'cache-copy-timeout-unclosed-no-progress',
))
# Only reviewed static codes may leave the entrypoint or stop the host runner.
ABORT_CODES = frozenset((
    'unknown', 'closed-data-schema', 'config-transport', 'invalid-closed-parameter',
    'closed-entrypoint', 'guest-platform', 'guest-uid', 'file-type-or-size',
    'file-owner', 'file-mode', 'file-changed', 'guest-nonce', 'guest-root-mount',
    'guest-host-mount', 'seed-not-readonly-iso', 'guest-network', 'guest-network-mac',
    'guest-network-timeout', 'guest-identity', 'guest-login-runtime', 'guest-dbus', 'timeout',
))
STAGES = ('setup', 'firstApply', 'repeatApply', 'faultApply', 'recoveryApply')
SETUP_ERROR_CODES = frozenset((
    'unknown', 'cancelled', 'timeout', 'command-timeout', 'command-output-limit',
    'pty-output-limit', 'pty-stage-order', 'unknown-pty-prompt', 'setup-not-saved',
    'setup-incomplete', 'setup-unexpected-commit', 'setup-lost-proof', 'lock-drift',
    'file-type-or-size', 'file-owner', 'file-mode', 'file-changed',
))
# Frozen Linux/common validation log IDs; secrets use their YAML schema IDs.
MAX_SETUP_DIAGNOSTIC = 8192
SETUP_FIELD_IDS = (
    'envy.storage.mode', 'envy.storage.maintenance.go-modcache',
    'envy.storage.archive.repository', 'envy.storage.archive.mountSource',
    'envy.user.name', 'envy.user.home', 'envy.git.name', 'envy.git.email',
    'envy.habits.terminalScratchpad.gesture', 'envy.habits.globalLauncher.gesture',
    'envy.repository.path', 'envy.vscode.mode', 'envy.mirrors.mode',
    'envy.amux.skipPermissions', 'storage/archive/password', 'home/passwd',
    'llm/steps/apikey', 'llm/deepseek/apikey', 'envy.linux.desktop', 'envy.linux.option',
)
SETUP_VALIDATION_MESSAGES = (
    'required field is empty', 'value is not one of the allowed choices',
    'Value cannot be empty', 'Please enter a valid email address',
    'Terminal scratchpad gesture must be one of F2 through F10, or F12',
    'Global launcher gesture must use Option+<key>, for example Option+Space',
)
SETUP_CHILD_CLASSES = (
    'NameError', 'UnboundLocalError', 'AttributeError', 'KeyError', 'IndexError',
    'TypeError', 'ValueError', 'RuntimeError', 'OSError', 'FileNotFoundError',
    'PermissionError', 'CalledProcessError', 'TimeoutExpired', 'EOFError',
    'ImportError', 'ModuleNotFoundError', 'AssertionError', 'NotImplementedError',
)
SETUP_CHILD_FRAMES = (
    '_run_setup', '_edit', 'menuconfig_loop', 'show_changes',
    '_save_needed_without_edits', 'validate_required_fields', '_validate_field',
    'software_changes', 'read_sops_yaml_keys', 'get_current_device_public_key',
    'current_device_public_key', '_prepare_key_and_secrets', 'initial_values',
    'manifest_settings', 'machine_manifest', 'save_all',
)
SECRET_NAMES = ('home-passwd', 'llm-credential-stepfun', 'llm-credential-deepseek')
SIGNATURE_SCOPE = 'public-copy-require-sigs; local-content-verify-no-trust; export-is-not-narinfo'
TRANSPORTS = ('nix-export', 'guest-fetch')
NETWORK_MAC = '52:54:00:45:4e:59'
MAX_SOURCE = 128 * 1024**2
MAX_METADATA = 16 * 1024**2
MAX_OUTPUT = 8 * 1024**2
PROOF = 'envy-runtime-home-manager-proof\n'
PROOF_REL = '.config/envy-runtime/proof'
BASELINE = '''{ lib, ... }: {
  imports = [ (import ../../checks/runtime/fixture.nix @ARGS@) ];
  home.file.".config/envy-runtime/proof".text = "envy-runtime-home-manager-proof\\n";
}
'''
FAULT = '''{ lib, ... }: {
  imports = [ ./runtime-original.nix ];
  home.activation.envyRuntimeFixedFault = lib.hm.dag.entryAfter [ "linkGeneration" ] ''
    printf 'ENVY_RUNTIME_FIXED_FAULT_97\\n' >&2
    exit 97
  '';
}
'''
PATH_RE = r'/nix/store/[0-9abcdfghijklmnpqrsvwxyz]{32}-[A-Za-z0-9+._?=-]+'
DEADLINE = None
COMMAND_FACTS = []


class GuestFailure(Exception):
    """Only fixed error codes may cross the serial boundary."""


def require(condition, code):
    if not condition:
        raise GuestFailure(code)


def emit_serial(prefix, row):
    """Root-only receipt boundary, called only after a successful guest guard."""
    require(type(prefix) is str and prefix in SERIAL_PREFIXES, 'serial-prefix')
    require(os.getuid() == os.geteuid() == 0, 'serial-uid')
    data = (prefix + json.dumps(row, sort_keys=True) + '\n').encode()
    require(len(data) <= MAX_SERIAL, 'serial-budget')
    deadline = time.monotonic() + 5
    fd = os.open('/dev/ttyS0', os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        # Linux ttyS0 is fixed at major 4, minor 64; never follow a path again.
        require(stat.S_ISCHR(info.st_mode) and os.major(info.st_rdev) == 4
                and os.minor(info.st_rdev) == 64, 'serial-device')
        offset = 0
        while offset < len(data):
            require(time.monotonic() < deadline, 'serial-timeout')
            try:
                count = os.write(fd, data[offset:offset + 8192])
            except BlockingIOError:
                time.sleep(0.01)
                continue
            except InterruptedError:
                continue
            require(type(count) is int and 0 < count <= min(8192, len(data) - offset), 'serial-write')
            offset += count
    finally:
        os.close(fd)


def emit_diagnostic(config, exc):
    # Diagnostics are best effort and cannot mask the first failure or cleanup.
    try:
        code = exc.args[0] if isinstance(exc, GuestFailure) and len(exc.args) == 1 else None
        if type(code) is not str or code not in DIAGNOSTIC_CODES:
            code = 'unknown'
        phase = ROOT_PHASE if type(ROOT_PHASE) is str and ROOT_PHASE in ROOT_PHASES else 'root-stages'
        facts = []
        for fact in COMMAND_FACTS[-8:]:
            if (type(fact) is dict and set(fact) == {'exitCode', 'outputBytes', 'outputSha256'}
                    and type(fact['exitCode']) is int and -255 <= fact['exitCode'] <= 255
                    and type(fact['outputBytes']) is int and 0 <= fact['outputBytes'] <= MAX_OUTPUT
                    and type(fact['outputSha256']) is str
                    and re.fullmatch('[0-9a-f]{64}', fact['outputSha256']) is not None):
                facts.append(dict(fact))
        emit_serial(DIAGNOSTIC_PREFIX, {'schemaVersion': 1, 'token': config['token'],
                                      'phase': phase, 'error': code, 'lastCommands': facts})
    except (GuestFailure, OSError, ValueError, KeyError, TypeError, KeyboardInterrupt):
        pass


def tick():
    require(DEADLINE is not None and time.monotonic() < DEADLINE, 'timeout')


def cancelled(signum, frame):
    raise KeyboardInterrupt


def sha(data):
    return hashlib.sha256(data).hexdigest()


def unique_json(data):
    def pairs(items):
        row = {}
        for key, value in items:
            require(key not in row, 'duplicate-json-key')
            row[key] = value
        return row
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(GuestFailure('nonfinite-json')))


def keys(row, expected):
    require(isinstance(row, dict) and set(row) == set(expected), 'closed-data-schema')


def store_path(value):
    require(isinstance(value, str) and re.fullmatch(PATH_RE, value) is not None, 'store-path')
    return value


def validate_config(config):
    keys(config, ('token', 'metadataSha256', 'metadataSize', 'installTimeout', 'fullTimeout', 'transport'))
    require(config['transport'] in TRANSPORTS, 'config-transport')
    for name in ('token', 'metadataSha256'):
        require(isinstance(config[name], str) and re.fullmatch('[0-9a-f]{64}', config[name]) is not None,
                'invalid-closed-parameter')
    require(type(config['metadataSize']) is int and 0 < config['metadataSize'] <= MAX_METADATA,
            'invalid-closed-parameter')
    for name, cap in (('installTimeout', 1200), ('fullTimeout', 7200)):
        value = config[name]
        require(type(value) in (int, float) and math.isfinite(value) and 0 < value <= cap,
                'invalid-closed-parameter')


def mounts(raw, *, mounted, root_identity):
    rows = []
    for line in raw.splitlines():
        left, right = line.split(' - ', 1)
        a, b = left.split(), right.split()
        rows.append((a[4], set(a[5].split(',')), b[0], b[1], a[2], a[3]))
    roots = [row for row in rows if row[0] == '/']
    require(len(roots) == 1 and roots[0][2] == 'ext4'
            and roots[0][4] == root_identity and roots[0][5] == '/', 'guest-root-mount')
    require(not any(fs in ('9p', 'virtiofs', 'nfs', 'nfs4', 'cifs', 'fuse.sshfs')
                    or path.startswith(('/Users', '/host', '/mnt/host')) for path, opts, fs, source, device, mount_root in rows),
            'guest-host-mount')
    if mounted:
        require(any(path == str(SEED) and fs == 'iso9660' and source == '/dev/sr0'
                    and {'ro', 'nosuid', 'nodev', 'noexec'} <= opts for path, opts, fs, source, device, mount_root in rows),
                'seed-not-readonly-iso')


def stable_stat(info):
    # Reads may update atime under relatime. Identity, permissions, links and
    # content timestamps must remain unchanged across the same open FD.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_uid,
            info.st_gid, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def private_read(path, cap, *, owner=None, mode=None, single_link=True):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and (not single_link or info.st_nlink == 1) and 0 <= info.st_size <= cap,
                'file-type-or-size')
        require(owner is None or info.st_uid == owner, 'file-owner')
        require(mode is None or stat.S_IMODE(info.st_mode) == mode, 'file-mode')
        data = stream.read(cap + 1)
        require(len(data) == info.st_size and len(data) <= cap, 'file-changed')
        require(stable_stat(os.fstat(stream.fileno())) == stable_stat(info), 'file-changed')
        return data


def guard(config, *, root, mounted=True):
    """This boundary must be stubbed before IO in every unit test of execution."""
    validate_config(config)
    require(sys.platform == 'linux' and platform.machine() == 'x86_64', 'guest-platform')
    require((os.getuid() == os.geteuid() == 0) if root else
            (os.getuid() == os.geteuid() != 0), 'guest-uid')
    require(private_read(MARKER, 65, owner=0, mode=0o444) == (config['token'] + '\n').encode(),
            'guest-nonce')
    # The fixed virtio disk identity comes from metadata, never a source alias.
    block = os.stat('/dev/vda1', follow_symlinks=False)
    root_info = os.stat('/', follow_symlinks=False)
    require(stat.S_ISBLK(block.st_mode) and stat.S_ISDIR(root_info.st_mode)
            and root_info.st_dev == block.st_rdev, 'guest-root-mount')
    root_identity = f'{os.major(block.st_rdev)}:{os.minor(block.st_rdev)}'
    mounts(Path('/proc/self/mountinfo').read_text(), mounted=mounted, root_identity=root_identity)
    network_guard(config['transport'])
    if not root:
        identity = pwd.getpwuid(os.getuid())
        require(identity.pw_name == 'envy-runtime' and identity.pw_dir == str(HOME)
                and os.environ.get('HOME') == str(HOME), 'guest-identity')
        runtime = Path('/run/user') / str(os.getuid())
        info = runtime.stat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o700
                and os.environ.get('XDG_RUNTIME_DIR') == str(runtime), 'guest-login-runtime')
        require(stat.S_ISSOCK((runtime / 'bus').stat().st_mode), 'guest-dbus')


def network_guard(transport):
    require(transport in TRANSPORTS, 'config-transport')
    deadline = time.monotonic() + 30
    while True:
        names = {p.name for p in Path('/sys/class/net').iterdir()}
        if transport == 'nix-export':
            require(names == {'lo'}, 'guest-network')
            return
        require('lo' in names and names <= {'lo', 'envy0'}, 'guest-network')
        if 'envy0' in names:
            require(Path('/sys/class/net/envy0/address').read_text().strip() == NETWORK_MAC,
                    'guest-network-mac')
            return
        tick()
        require(time.monotonic() < deadline, 'guest-network-timeout')
        time.sleep(0.1)


def nix_config(transport):
    require(transport in TRANSPORTS, 'config-transport')
    if transport == 'nix-export':
        return ('experimental-features = nix-command flakes\nsubstituters =\nextra-substituters =\n'
                'builders =\nconnect-timeout = 1\n')
    # Only the official signed cache is used. No host configuration, registry,
    # proxy, remote builder, or weakened TLS/signature policy enters this env.
    return ('experimental-features = nix-command flakes\n'
            'substituters = https://cache.nixos.org\nextra-substituters =\n'
            'builders =\nrequire-sigs = true\nssl-verify = true\n'
            'ssl-cert-file = ' + SYSTEM_CA_BUNDLE + '\n')


def env_transport(env):
    transport = env.get('ENVY_RUNTIME_TRANSPORT', 'nix-export')
    require(transport in TRANSPORTS, 'environment-transport')
    require(env.get('NIX_CONFIG', nix_config('nix-export')) == nix_config(transport),
            'environment-mode-mismatch')
    if transport == 'guest-fetch':
        require(env.get('NIX_SSL_CERT_FILE') == SYSTEM_CA_BUNDLE, 'environment-mode-mismatch')
    return transport


def offline_args(env):
    return ['--offline'] if env_transport(env) == 'nix-export' else []


def verify_lock(env, path=REPO):
    expected = env.get('ENVY_RUNTIME_LOCK_SHA256')
    if expected is not None:
        try:
            require(sha(private_read(path / 'flake.lock', MAX_SOURCE)) == expected, 'lock-drift')
        except (GuestFailure, OSError) as exc:
            exc.observation = {'status': 'partial-or-unknown'}
            raise


def environment(*, user=False, tools=None, transport='nix-export', lock_sha256=None):
    env = {'HOME': str(HOME), 'USER': 'envy-runtime', 'LOGNAME': 'envy-runtime',
           'PATH': '/nix/var/nix/profiles/default/bin:/usr/sbin:/usr/bin:/sbin:/bin',
           'LC_ALL': 'C.UTF-8', 'TERM': 'xterm-256color', 'NIX_PATH': '',
           'NIX_USER_CONF_FILES': '/dev/null', 'NIX_CONFIG': nix_config(transport),
           'ENVY_RUNTIME_TRANSPORT': transport,
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
           'GIT_CONFIG_SYSTEM': '/dev/null', 'GIT_TERMINAL_PROMPT': '0',
           'GIT_AUTHOR_NAME': 'envY Runtime Fixture', 'GIT_AUTHOR_EMAIL': 'envy-runtime@runtime.invalid',
           'GIT_COMMITTER_NAME': 'envY Runtime Fixture', 'GIT_COMMITTER_EMAIL': 'envy-runtime@runtime.invalid',
           'PYTHONPYCACHEPREFIX': str(HOME / '.cache/envy-runtime/pycache'),
           'CARGO_TARGET_DIR': str(HOME / '.cache/envy-runtime/cargo')}
    if transport == 'guest-fetch':
        # Nix's setting and its environment-selected default must agree, also
        # for installer subprocesses. Neither value comes from the host.
        env['NIX_SSL_CERT_FILE'] = SYSTEM_CA_BUNDLE
    if lock_sha256 is not None:
        require(transport == 'guest-fetch', 'environment-lock-mode')
        env['ENVY_RUNTIME_LOCK_SHA256'] = lock_sha256
    if tools:
        env['PATH'] = ':'.join(tools[name] + '/bin' for name in ('gitMinimal', 'sops', 'age', 'ssh-to-age')) + ':' + env['PATH']
    if user:
        # PAM creates these; their existence and service are checked, not invented.
        for key in ('XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'XDG_SESSION_ID'):
            if key in os.environ:
                env[key] = os.environ[key]
        env['ENVY_ROOT'] = str(REPO)
    return env


def classify_setup_failure(sample, *, line_start=True):
    """Parse the bounded plain tail into static facts, never transcript strings."""
    sample = sample[-MAX_SETUP_DIAGNOSTIC:]
    lines = re.split(rb'\r\n|\r|\n', sample)
    if not line_start:
        lines = lines[1:]  # A truncated line cannot manufacture an anchored marker.
    validation_lines = []
    in_traceback, has_frame = False, False
    child_class, last_frame = None, None
    for line in lines:
        if not in_traceback:
            if line == b'Traceback (most recent call last):':
                in_traceback = True
            else:
                validation_lines.append(line)
            continue
        if re.fullmatch(rb'  File "[^"\r\n]*", line [0-9]+, in [^\r\n]+', line):
            has_frame = True
            for name in SETUP_CHILD_FRAMES:
                if re.fullmatch(rb'  File "[^"\r\n]*", line [0-9]+, in ' + name.encode(), line):
                    last_frame = name
            continue
        if has_frame and line.startswith(b'    '):
            continue  # Python 3.13 source/caret lines belong to the frame.
        # The first other line terminates the first frame section, including
        # an unknown exception. No body, header or chain can reopen this tail.
        for name in SETUP_CHILD_CLASSES:
            tokens = (name.encode(),)
            if name in ('CalledProcessError', 'TimeoutExpired'):
                tokens += (('subprocess.' + name).encode(),)
            if line in tokens or line.startswith(tuple(token + b':' for token in tokens)):
                child_class = name
                break
        break
    validation = b'\n'.join(validation_lines)
    invalid = []
    for field_id in SETUP_FIELD_IDS:
        for message in SETUP_VALIDATION_MESSAGES:
            # Rich may wrap a fixed message or its field token across lines.
            pattern = (rb'(?m)^ERR setup:\s+' + rb'\s+'.join(re.escape(word.encode()) for word in message.split())
                       + rb'\s+field=' + re.escape(field_id.encode()) + rb'[ \t]*$')
            if re.search(pattern, validation) is not None:
                invalid.append(field_id)
                break
    return {'kind': 'python-exception' if child_class is not None else 'validation-rejected' if invalid else 'unknown',
            'exceptionClass': child_class, 'lastKnownFrame': last_frame, 'invalidFieldIds': invalid}


def initial_manifest_evaluation(sample, *, line_start=True):
    """Observe only the first fixed setup evaluation event before opaque bodies."""
    sample = sample[:MAX_SETUP_DIAGNOSTIC]
    lines = re.split(rb'\r\n|\r|\n', sample)
    if not line_start:
        lines = lines[1:]
    row = {'status': 'unknown', 'exitCode': None, 'debugEnabled': True}
    timestamp = rb'(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}'
    prefix = rb'DBG ' + timestamp + rb' eval: '
    messages = ((b'Nix manifest evaluation started', 'started', b'attr='),
                (b'Nix manifest evaluation completed', 'completed', b'elapsed='),
                (b'manifest cache hit', 'cached', b'cache='),
                (b'Nix manifest evaluation failed', 'failed', b'exit_code='))
    for index, line in enumerate(lines):
        if line == b'Traceback (most recent call last):':
            return row, True
        for message, status, parameter in messages:
            pattern = (prefix + rb'\s+'.join(map(re.escape, message.split()))
                       + rb'\s+machine=runtime-min\s+' + parameter)
            remaining = b'\n'.join(lines[index:])
            match = re.match(pattern, remaining)
            if match is None:
                continue
            if status == 'failed':
                code = re.match(rb'([0-9]{1,3})\s+detail=', remaining[match.end():])
                if code is not None and 1 <= int(code[1]) <= 255:
                    row.update(status='timeout' if int(code[1]) == 124 else 'failed', exitCode=int(code[1]))
                # A split field is pending until its delimiter or complete line arrives.
                suffix = remaining[match.end():]
                pending = re.fullmatch(rb'[0-9]{0,3}\s*(?:d|de|det|deta|detai|detail)?', suffix) is not None
                return row, code is not None or (not pending and b'\n' in suffix)
            row['status'] = status
            if status != 'started':
                return row, True
            break
        if b' detail=' in line or line.startswith(b'detail='):
            return row, True  # Never reopen an opaque multiline detail.
    return row, False


class SetupDialogue:
    """Finite dialogue for the reviewed setup UI; no speculative keystrokes."""
    def __init__(self):
        self.text = ''
        self.diagnostic_tail = b''
        self.diagnostic_line_start = True
        self.initial_evaluation = {'status': 'unknown', 'exitCode': None, 'debugEnabled': True}
        self.initial_evaluation_closed = False
        self.wire = ''
        self.alternate = False
        self.answered = {}
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.phase = 0
        self.saved = False
        self.existing = False
        self.checkout = False
        self.output_bytes = 0
        self.output_digest = hashlib.sha256()

    def feed(self, chunk):
        # Hash original bytes, including refused output and split control codes.
        self.output_bytes += len(chunk)
        self.output_digest.update(chunk)
        require(self.output_bytes <= MAX_OUTPUT, 'pty-output-limit')
        self.wire += self.decoder.decode(chunk)
        require(len(self.wire.encode()) <= MAX_OUTPUT, 'pty-output-limit')
        replies = []
        allowed_phase = self.phase
        while self.wire:
            boundary = self.wire.find('\x1b')
            if boundary != 0:
                end = boundary if boundary > 0 else len(self.wire)
                replies.append(self._plain(self.wire[:end], allowed_phase))
                self.wire = self.wire[end:]
                continue
            # Only frame the locked UI's CSI/OSC sequences, not a VT emulator.
            control = re.match(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[^\[\]])', self.wire)
            if control is None:
                break  # Keep a partial escape until the next chunk.
            code = control.group()
            self.wire = self.wire[control.end():]
            if code == '\x1b[6n':
                replies.append(b'\x1b[1;1R')
            elif code in ('\x1b[?1049h', '\x1b[?1049l'):
                entering = code.endswith('h')
                require((not self.alternate and self.phase == 0) if entering
                        else (self.alternate and self.phase == 1), 'pty-stage-order')
                self.alternate = entering
                self.text = ''  # A menu repaint is never a stdin question.
        return b''.join(replies)

    def _plain(self, value, allowed_phase):
        tail = self.diagnostic_tail + value.encode()
        if not self.initial_evaluation_closed:
            self.initial_evaluation, self.initial_evaluation_closed = initial_manifest_evaluation(
                tail, line_start=self.diagnostic_line_start)
        if len(tail) > MAX_SETUP_DIAGNOSTIC:
            if self.initial_evaluation['status'] not in ('completed', 'cached', 'failed', 'timeout'):
                self.initial_evaluation = {'status': 'unknown', 'exitCode': None, 'debugEnabled': True}
            self.initial_evaluation_closed = True
            self.diagnostic_line_start = tail[-MAX_SETUP_DIAGNOSTIC - 1] in (10, 13)
        self.diagnostic_tail = tail[-MAX_SETUP_DIAGNOSTIC:]
        self.text += value
        require(len(self.text.encode()) <= MAX_OUTPUT, 'pty-output-limit')
        self.existing |= 'Nix is already installed.' in self.text
        self.checkout |= 'Using existing checkout: /home/envy-runtime/.envy' in self.text
        if not self.alternate:
            self.saved |= 'All files saved successfully!' in self.text
        menu = 'envY Setup — menuconfig' in self.text and 's: save & exit' in self.text
        if menu and self.phase == 0:
            self.phase = 1
            self.text = ''
            return b's'
        if self.alternate:
            return b''
        patterns = (r'Apply changes and save\?\s*\[y/n\]',
                    r'Commit\s+[^?]+?to git\?\s*\[y/n\]',
                    r'Apply configuration now \(envy apply\)\?\s*\[y/n\]')
        matches = sorted((match.start(), phase, match)
                         for phase, pattern in enumerate(patterns, 1)
                         for match in re.finditer(pattern, self.text))
        replies, remaining, end = [], [], 0
        for start, phase, match in matches:
            require(phase <= allowed_phase, 'pty-stage-order')
            prefix = self.text[end:start]
            line = re.split(r'[\r\n]', prefix)[-1]
            require(not line.strip(' \t:?'), 'unknown-pty-prompt')
            remaining.append(prefix)
            prompt = re.sub(r'\s+', ' ', match.group())
            if phase < self.phase:
                # Consume only this complete answered prompt, never its suffix.
                require(self.answered.get(phase) == prompt, 'unknown-pty-prompt')
            else:
                if phase == 3:
                    require(self.saved, 'setup-not-saved')
                replies.append((b'y\n', b'n\n', b'n\n')[phase - 1])
                self.answered[phase] = prompt
                self.phase += 1
            end = match.end()
        remaining.append(self.text[end:])
        self.text = ''.join(remaining)
        if re.search(r'(?:\[y/n\]|\[Y/n\]|\[y/N\]|Choose .*?:|Press Enter|password:)', self.text):
            raise GuestFailure('unknown-pty-prompt')
        return b''.join(replies)

    def failure_observation(self, exc):
        """Closed progress facts only; no exception text or terminal transcript."""
        code = exc.args[0] if isinstance(exc, GuestFailure) and len(exc.args) == 1 else None
        if type(code) is not str or code not in SETUP_ERROR_CODES:
            code = 'unknown'
        if isinstance(exc, KeyboardInterrupt):
            code = 'cancelled'
        kind = next((name for cls, name in (
            (KeyboardInterrupt, 'KeyboardInterrupt'), (GuestFailure, 'GuestFailure'),
            (OSError, 'OSError'), (ValueError, 'ValueError'), (KeyError, 'KeyError'),
            (TypeError, 'TypeError'), (subprocess.SubprocessError, 'SubprocessError'),
        ) if isinstance(exc, cls)), 'unknown')
        number = exc.errno if isinstance(exc, OSError) else None
        if type(number) is not int or not 1 <= number <= 4095:
            number = None
        return {'status': 'partial-or-unknown', 'errorCode': code, 'exceptionKind': kind,
                'errno': number, 'dialoguePhase': self.phase, 'saved': self.saved,
                'existingNix': self.existing, 'checkout': self.checkout,
                'outputBytes': self.output_bytes, 'outputSha256': self.output_digest.hexdigest(),
                'childFailure': classify_setup_failure(self.diagnostic_tail, line_start=self.diagnostic_line_start),
                'initialManifestEvaluation': self.initial_evaluation.copy()}

    def finish(self, code):
        require(code == 0 and self.phase == 4 and self.saved and self.existing and self.checkout
                and not self.wire and not self.decoder.getstate()[0] and not self.alternate,
                'setup-incomplete')
        return {'exitCode': code, 'interactive': True, 'saved': self.saved,
                'commitDeclined': True, 'applyDeclined': True, 'existingNixBranch': self.existing}


def stop(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def classify_nix_failure(sample):
    """Return only a reviewed static code from a bounded stderr sample."""
    sample = sample[:MAX_NIX_STDERR].lower()
    for code, patterns in NIX_FAILURE_PATTERNS:
        if any(re.search(pattern, sample) is not None for pattern in patterns):
            return code
    return 'unknown'


class _NativeActivityLines:
    """Bound one native JSON line without retaining a raw history."""

    def __init__(self):
        self.pending = bytearray()
        self.unknown = False

    @staticmethod
    def uint64(value):
        return type(value) is int and 0 <= value < 2**64

    def feed(self, data):
        if self.unknown:
            return
        offset = 0
        while offset < len(data):
            end = data.find(b'\n', offset)
            boundary = len(data) if end < 0 else end
            # Check before extending, including a fragment from a giant read.
            if len(self.pending) + boundary - offset > 4096:
                self.unknown = True
                self.pending.clear()
                return
            self.pending.extend(data[offset:boundary])
            if end < 0:
                return
            try:
                if self.pending.startswith(b'@nix '):
                    del self.pending[:5]
                    self.native(unique_json(self.pending))
            except (GuestFailure, ValueError, TypeError, RecursionError):
                self.unknown = True
            finally:
                self.pending.clear()
            if self.unknown:
                return
            offset = end + 1

    def finish(self):
        if self.pending:
            self.unknown = True
            self.pending.clear()


class _PrefetchObservation(_NativeActivityLines):
    """Observe only this locked prefetch's native activities, never completion."""

    def __init__(self, url):
        super().__init__()
        self.url = url
        self.seen = set()
        self.active = {}

    def native(self, row):
        require(type(row) is dict and type(row.get('action')) is str, 'closed-data-schema')
        action = row['action']
        # Only the basic official message schema is known; text is never evidence.
        if action == 'msg':
            keys(row, ('action', 'level', 'msg'))
            require(type(row['level']) is int and 0 <= row['level'] <= 7
                    and type(row['msg']) is str, 'closed-data-schema')
            return
        expected = {'start': {'action', 'id', 'level', 'type', 'text', 'parent'},
                    'stop': {'action', 'id'}, 'result': {'action', 'id', 'type'}}
        require(action in expected, 'closed-data-schema')
        required = expected[action]
        optional = set() if action == 'stop' else {'fields'}
        require(required <= set(row) <= required | optional and self.uint64(row['id']), 'closed-data-schema')
        ident = row['id']
        fields = row.get('fields', [])
        require(type(fields) is list and all(type(f) is str or self.uint64(f) for f in fields), 'closed-data-schema')
        if action == 'stop':
            require(ident in self.active, 'closed-data-schema')
            del self.active[ident]
            return
        kind = row['type']
        require(type(kind) is int, 'closed-data-schema')
        if action == 'result':
            require(ident in self.active and 100 <= kind <= 108, 'closed-data-schema')
            return
        require(ident not in self.seen and len(self.seen) < 16
                and self.uint64(row['parent']) and type(row['level']) is int and 0 <= row['level'] <= 7
                and type(row['text']) is str and (kind == 0 or 100 <= kind <= 112), 'closed-data-schema')
        stage = 'ignored'
        if kind == 101:
            require(fields == [self.url], 'closed-data-schema')
            stage = 'download'
        elif kind == 0:
            text = row['text']
            if text == "unpacking '" + self.url + "'":
                require(not fields, 'closed-data-schema')
                stage = 'unpack'
            elif text == "adding '" + self.url + "' to the store":
                require(not fields, 'closed-data-schema')
                stage = 'store-add'
            else:
                require(not text.startswith(("unpacking '", "adding '")), 'closed-data-schema')
        self.seen.add(ident)
        self.active[ident] = stage

    def timeout_code(self, exit_observed, stdout_open, stderr_open):
        # Freeze facts already observed before cleanup; never query a PID here.
        self.finish()
        if exit_observed and (stdout_open or stderr_open):
            return 'prefetch-timeout-exit-observed-open-stream'
        if self.unknown:
            return 'prefetch-timeout-unknown'
        stages = [stage for stage in self.active.values() if stage != 'ignored']
        if len(stages) > 1:
            return 'prefetch-timeout-overlap'
        if stages:
            return 'prefetch-timeout-active-' + stages[0]
        return 'prefetch-timeout-unknown'


class _CacheCopyObservation(_NativeActivityLines):
    """Bind one official cache copy; progress and stop never prove import."""

    def __init__(self, canonical):
        super().__init__()
        self.canonical = canonical
        self.seen = set()
        self.active = set()
        self.copy_id = None
        self.copy_stopped = False
        self.progress_seen = False
        self.done = 0
        self.expected = 0

    def native(self, row):
        require(type(row) is dict and type(row.get('action')) is str, 'closed-data-schema')
        action = row['action']
        if action == 'msg':
            keys(row, ('action', 'level', 'msg'))
            require(type(row['level']) is int and 0 <= row['level'] <= 7
                    and type(row['msg']) is str, 'closed-data-schema')
            return
        schemas = {'start': {'action', 'id', 'level', 'type', 'text', 'parent'},
                   'stop': {'action', 'id'}, 'result': {'action', 'id', 'type'}}
        require(action in schemas, 'closed-data-schema')
        required = schemas[action]
        optional = set() if action == 'stop' else {'fields'}
        require(required <= set(row) <= required | optional and self.uint64(row['id']), 'closed-data-schema')
        ident = row['id']
        fields = row.get('fields', [])
        require(type(fields) is list and all(type(f) is str or self.uint64(f) for f in fields), 'closed-data-schema')
        if action == 'stop':
            require(ident in self.active, 'closed-data-schema')
            self.active.remove(ident)
            if ident == self.copy_id:
                self.copy_stopped = True
            return
        kind = row['type']
        require(type(kind) is int, 'closed-data-schema')
        if action == 'result':
            require(ident in self.active and 100 <= kind <= 108, 'closed-data-schema')
            if kind == 105:
                require(len(fields) == 4 and all(self.uint64(f) for f in fields), 'closed-data-schema')
                if ident == self.copy_id:
                    done, expected, _, _ = fields
                    require(done >= self.done and (expected > 0 or done == 0)
                            and (expected == 0 or done <= expected)
                            and (self.expected == 0 or expected == self.expected), 'closed-data-schema')
                    self.done, self.expected = done, expected
                    self.progress_seen = True
            return
        require(ident not in self.seen and len(self.seen) < 16
                and self.uint64(row['parent']) and type(row['level']) is int and 0 <= row['level'] <= 7
                and type(row['text']) is str and (kind == 0 or 100 <= kind <= 112), 'closed-data-schema')
        if kind == 100:
            require(len(fields) == 3 and fields[0] == self.canonical
                    and fields[1] in ('https://cache.nixos.org', 'https://cache.nixos.org/')
                    and fields[2] in ('local', 'daemon') and self.copy_id is None, 'closed-data-schema')
            self.copy_id = ident
        self.seen.add(ident)
        self.active.add(ident)

    def timeout_code(self, exit_observed, stdout_open, stderr_open):
        self.finish()
        if exit_observed and (stdout_open or stderr_open):
            return 'cache-copy-timeout-exit-observed-open-stream'
        if self.unknown:
            return 'cache-copy-timeout-unknown'
        if self.copy_stopped:
            return 'cache-copy-timeout-stop-observed'
        if self.copy_id is not None:
            if self.expected > 0 and self.done == self.expected:
                return 'cache-copy-timeout-unclosed-reported-total'
            if self.progress_seen:
                return 'cache-copy-timeout-unclosed-progress'
            return 'cache-copy-timeout-unclosed-no-progress'
        return 'cache-copy-timeout-unknown'


def command(argv, *, env, cwd='/', timeout=600, input_fd=None, terminal=False, dialogue=None, merge=True,
            _prefetch_url=None, _cache_copy_path=None):
    """Drain bounded output without echoing it or persisting raw secret text."""
    global LAST_NIX_FAILURE
    LAST_NIX_FAILURE = 'unknown'
    tick()
    # Refuse to spawn without non-reaping exit observation. Keep the leader's
    # PID reserved until its owned process group has been stopped.
    require(callable(getattr(os, 'waitid', None)) and all(
        type(getattr(os, name, None)) is int and getattr(os, name) > 0
        for name in ('P_PID', 'WEXITED', 'WNOHANG', 'WNOWAIT',
                     'CLD_EXITED', 'CLD_KILLED', 'CLD_DUMPED')), 'command-failed')
    wait_flags = os.WEXITED | os.WNOHANG | os.WNOWAIT
    sample = bytearray()
    classify = (argv[0] == NIX and not terminal and not merge
                and ROOT_PHASE in NIX_FAILURE_PHASES and os.getuid() == os.geteuid() == 0)
    prefetch = (_CacheCopyObservation(_cache_copy_path) if _cache_copy_path is not None else
                _PrefetchObservation(_prefetch_url) if _prefetch_url is not None else None) if classify else None
    sample_limit = 4096 if prefetch is not None else MAX_NIX_STDERR
    deadline = min(DEADLINE, time.monotonic() + timeout)
    process, pid, master = None, None, None
    can_stop = True
    output = bytearray()
    used, digest = 0, hashlib.sha256()
    code = None
    stdout_fd, stderr_fd = None, None
    streams = []
    try:
        if terminal:
            pid, master = pty.fork()
            if pid == 0:
                try:
                    import fcntl
                    import struct
                    import termios
                    fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack('HHHH', 60, 180, 0, 0))
                    os.chdir(cwd)
                    os.execve(argv[0], argv, env)
                except BaseException:
                    os._exit(126)
            streams = [master]
        else:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=input_fd if input_fd is not None else subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT if merge else subprocess.PIPE, start_new_session=True,
                                       close_fds=True)
            stdout_fd = process.stdout.fileno()
            pid, streams = process.pid, [stdout_fd]
            if not merge:
                stderr_fd = process.stderr.fileno()
                streams.append(stderr_fd)
        while streams or code is None:
            tick()
            require(time.monotonic() < deadline, 'command-timeout')
            ready, _, _ = select.select(streams, [], [], 0.05)
            for fd in ready:
                try:
                    data = os.read(fd, 8192)
                except OSError as exc:
                    if terminal and exc.errno == errno.EIO:
                        data = b''
                    else:
                        raise
                if not data:
                    if prefetch is not None and fd == stderr_fd:
                        prefetch.finish()
                    streams.remove(fd)
                    continue
                used += len(data)
                digest.update(data)
                require(used <= MAX_OUTPUT, 'command-output-limit')
                if terminal or fd == stdout_fd:
                    output.extend(data)
                elif classify:
                    if len(sample) < sample_limit:
                        sample.extend(data[:sample_limit - len(sample)])
                    if prefetch is not None:
                        prefetch.feed(data)
                if dialogue:
                    reply = dialogue.feed(data)
                    if reply:
                        os.write(master, reply)
            if code is None:
                try:
                    observed = os.waitid(os.P_PID, pid, wait_flags)
                except OSError as exc:
                    if exc.errno == errno.ECHILD:
                        # A missing child no longer reserves the PID for us.
                        can_stop = False
                    raise
                if observed is not None:
                    require(type(observed.si_pid) is int and observed.si_pid == pid
                            and type(observed.si_code) is int
                            and type(observed.si_status) is int, 'command-failed')
                    if observed.si_code == os.CLD_EXITED:
                        require(0 <= observed.si_status <= 255, 'command-failed')
                        code = observed.si_status
                    elif observed.si_code in (os.CLD_KILLED, os.CLD_DUMPED):
                        require(0 < observed.si_status <= 255, 'command-failed')
                        code = -observed.si_status
                    else:
                        raise GuestFailure('command-failed')
        if code != 0 and classify:
            LAST_NIX_FAILURE = classify_nix_failure(sample)
        COMMAND_FACTS.append({'exitCode': code, 'outputBytes': used, 'outputSha256': digest.hexdigest()})
        return code, bytes(output)
    except GuestFailure as exc:
        if prefetch is not None and len(exc.args) == 1 and type(exc.args[0]) is str and exc.args[0] in ('timeout', 'command-timeout'):
            raise GuestFailure(prefetch.timeout_code(code is not None, stdout_fd in streams, stderr_fd in streams)) from None
        raise
    finally:
        sample.clear()
        if prefetch is not None:
            prefetch.pending.clear()
        primary = sys.exc_info()[1]
        try:
            # Attempt every owned cleanup even if stop or reap itself fails.
            with ExitStack() as cleanup:
                if master is not None:
                    cleanup.callback(os.close, master)
                if pid is not None:
                    if process:
                        if not merge:
                            cleanup.callback(process.stderr.close)
                        cleanup.callback(process.stdout.close)
                        cleanup.callback(process.wait, timeout=3)
                    else:
                        cleanup.callback(os.waitpid, pid, 0)
                    if can_stop:
                        cleanup.callback(stop, pid)
        except (KeyboardInterrupt, GuestFailure, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            if primary is None:
                raise


def checked(argv, *, env, **kwargs):
    global LAST_NIX_FAILURE
    # Also reset at this boundary when command is replaced by a test double.
    LAST_NIX_FAILURE = 'unknown'
    code, output = command(argv, env=env, **kwargs)
    failure = LAST_NIX_FAILURE if argv[0] == NIX and LAST_NIX_FAILURE in NIX_FAILURE_CODES else 'command-failed'
    require(code == 0, failure)
    return output


def nix(args, env, **kwargs):
    options = ['--option', 'substituters', '', '--option', 'extra-substituters', ''] if offline_args(env) else []
    try:
        return checked([NIX, *offline_args(env), '--extra-experimental-features', 'nix-command flakes',
                        *options, '--option', 'builders', '', *args], env=env, merge=False, **kwargs)
    finally:
        if kwargs.get('cwd') == str(REPO):
            verify_lock(env)
        elif env_transport(env) == 'guest-fetch' and kwargs.get('cwd') is not None:
            verify_lock(env, Path(kwargs['cwd']))


def metadata_transport(row):
    require(isinstance(row, dict), 'closed-data-schema')
    return row.get('transport', 'nix-export')


def validate_metadata(row, transport=None):
    mode = metadata_transport(row)
    require(mode in TRANSPORTS and (transport is None or mode == transport), 'metadata-transport')
    if mode == 'guest-fetch':
        keys(row, ('schemaVersion', 'transport', 'source', 'snapshot', 'sourceNarSize'))
        require(type(row['sourceNarSize']) is int and 0 < row['sourceNarSize'] <= MAX_SOURCE * 2,
                'metadata-nar-size')
    else:
        keys(row, ('schemaVersion', 'source', 'snapshot', 'preparation'))
    require(type(row['schemaVersion']) is int and row['schemaVersion'] == 1, 'metadata-version')
    keys(row['source'], ('path', 'narHash', 'lockSha256', 'manifestSha256'))
    store_path(row['source']['path'])
    for key in ('lockSha256', 'manifestSha256'):
        require(isinstance(row['source'][key], str) and re.fullmatch('[0-9a-f]{64}', row['source'][key]) is not None,
                'metadata-digest')
    nar_hash = row['source']['narHash']
    require(isinstance(nar_hash, str) and nar_hash.startswith('sha256-'), 'metadata-nar-hash')
    decoded = base64.b64decode(nar_hash[7:], validate=True)
    require(len(decoded) == 32 and base64.b64encode(decoded).decode() == nar_hash[7:], 'metadata-nar-hash')
    keys(row['snapshot'], ('size', 'sha256'))
    records = [(row['snapshot'], MAX_SOURCE + 1024**2)]
    if mode == 'nix-export':
        keys(row['preparation'], ('format', 'objects', 'artifact', 'signatureScope'))
        prep = row['preparation']
        require(prep['format'] == 'nix-export' and prep['signatureScope'] == SIGNATURE_SCOPE, 'metadata-policy')
        keys(prep['artifact'], ('size', 'sha256'))
        records.append((prep['artifact'], 64 * 1024**3))
    for record, cap in records:
        require(type(record['size']) is int and 0 < record['size'] <= cap
                and isinstance(record['sha256'], str) and re.fullmatch('[0-9a-f]{64}', record['sha256']) is not None,
                'metadata-artifact')
    if mode == 'guest-fetch':
        return {row['source']['path']}
    objects = prep['objects']
    require(isinstance(objects, list) and 0 < len(objects) <= 100000, 'metadata-objects')
    paths = set()
    for item in objects:
        keys(item, ('path', 'narHash', 'narSize', 'references'))
        path = store_path(item['path'])
        require(path not in paths and not path.endswith('.drv'), 'metadata-object-path')
        paths.add(path)
        require(type(item['narSize']) is int and item['narSize'] > 0, 'metadata-nar-size')
        require(isinstance(item['narHash'], str) and item['narHash'].startswith('sha256-')
                and len(base64.b64decode(item['narHash'][7:], validate=True)) == 32, 'metadata-nar-hash')
        require(isinstance(item['references'], list) and item['references'] == sorted(set(item['references'])),
                'metadata-references')
        for ref in item['references']:
            store_path(ref)
    require(row['source']['path'] in paths and all(set(item['references']) <= paths for item in objects),
            'metadata-open-closure')
    require(next(item for item in objects if item['path'] == row['source']['path'])['narHash'] == row['source']['narHash'],
            'metadata-source')
    return paths


def payload_fd(name, expected):
    fd = os.open(SEED / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o222
                and info.st_size == expected['size'], 'payload-type-size-mode')
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1024**2):
            tick()
            digest.update(chunk)
        require(digest.hexdigest() == expected['sha256'] and stable_stat(os.fstat(fd)) == stable_stat(info), 'payload-digest')
        os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except BaseException:
        os.close(fd)
        raise


def source_members(data, metadata):
    """Validate all names before extracting or importing executable source."""
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
        members = archive.getmembers()
        require(0 < len(members) < 100000, 'source-inventory')
        files = {}
        for item in members:
            require(item.isfile() and item.name.startswith('source/') and item.size <= MAX_SOURCE
                    and item.name not in files and item.mode in (0o600, 0o644, 0o755), 'source-member')
            name = item.name[7:]
            parts = name.split('/')
            require(name == str(PurePosixPath(name)) and not name.startswith('/')
                    and not any(p in ('', '.', '..', '.git', '.pi', 'secrets', '.device-label', '.sops.yaml',
                                      '.nas_cred', '__pycache__', '.cache', 'node_modules', 'target') for p in parts),
                    'private-source-member')
            allowed = name in ('flake.nix', 'flake.lock', 'darwin.nix', 'home.nix', 'envy', 'install.sh',
                               'setup.sh', 'setup.py', 'requires.sh', 'snapshot-manifest.json', 'hosts/default.nix',
                               'hosts/linux/runtime-min.nix', 'hosts/linux/runtime-default.nix')
            require(allowed or parts[0] in ('modules', 'checks', 'resources', 'files', 'staticfiles'),
                    'source-namespace')
            files[item.name] = (archive.extractfile(item).read(), item.mode)
        require(sum(len(data) for data, mode in files.values()) <= MAX_SOURCE, 'source-budget')
    raw = files['source/snapshot-manifest.json'][0]
    require(sha(raw) == metadata['source']['manifestSha256'], 'source-manifest-digest')
    manifest = unique_json(raw)
    require(manifest['schemaVersion'] == 1 and manifest['fixture']['selectionStage'] == 'minimal'
            and manifest['fixture']['secrets'] == 'guest-generated-only', 'source-fixture')
    entries = manifest['files']
    require(len({entry['relativePath'] for entry in entries}) == len(entries), 'source-duplicate')
    require(set(files) == {'source/' + entry['relativePath'] for entry in entries} | {'source/snapshot-manifest.json'},
            'source-inventory')
    for entry in entries:
        value, mode = files['source/' + entry['relativePath']]
        require(sha(value) == entry['sha256'] and f'{mode:04o}' == entry['mode'], 'source-content-mode')
    require(sha(files['source/flake.lock'][0]) == manifest['lockSha256'] == metadata['source']['lockSha256'],
            'source-lock')
    return files


def native_info(value, path):
    if isinstance(value, list):
        rows = [r for r in value if r.get('path') == path]
        require(len(rows) == 1, 'native-info')
        row = rows[0]
    else:
        rows = value.get('info', value)
        row = rows.get(path, rows.get(Path(path).name))
    require(isinstance(row, dict), 'native-info')
    refs = [ref if ref.startswith('/nix/store/') else '/nix/store/' + ref for ref in row['references']]
    return {'path': path, 'narHash': row['narHash'], 'narSize': row['narSize'], 'references': sorted(refs)}


def import_payload(fd, metadata, env):
    require(metadata_transport(metadata) == env_transport(env) == 'nix-export', 'import-transport')
    # Only this import invocation waives signatures. Content is checked below;
    # the original public cache signature verification happened before export.
    imported = checked([STORE, '--option', 'require-sigs', 'false', '--option', 'substituters', '',
                        '--option', 'builders', '', '--import'], input_fd=fd, env=env, timeout=1200)
    observed = [line for line in imported.decode('utf-8').splitlines() if line.startswith('/nix/store/')]
    require(sorted(observed) == sorted(item['path'] for item in metadata['preparation']['objects']),
            'import-inventory-mismatch')
    for item in metadata['preparation']['objects']:
        tick()
        actual = native_info(unique_json(nix(['path-info', '--json', item['path']], env)), item['path'])
        require(actual == item, 'native-object-mismatch')
        nix(['store', 'verify', '--no-trust', '--no-substitute', item['path']], env)
    source = metadata['source']['path']
    archive = unique_json(nix(['flake', 'archive', '--json', '--no-update-lock-file', 'path:' + source], env))
    approved = {item['path'] for item in metadata['preparation']['objects']}
    def visit(row):
        require(row['path'] in approved, 'archive-unapproved-source')
        for child in row.get('inputs', {}).values():
            visit(child)
    visit(archive)
    require(archive['path'] == source, 'archive-source')


def bootstrap_tools(metadata, env):
    global ROOT_PHASE
    require(metadata_transport(metadata) == env_transport(env), 'bootstrap-transport')
    source = metadata['source']['path']
    reference = 'path:' + source + '?narHash=' + quote(metadata['source']['narHash'], safe='')
    expression = ('let f = builtins.getFlake ' + json.dumps(reference) + '; '
                  'p = f.inputs.nixpkgs.legacyPackages.x86_64-linux; in '
                  '{ inherit (p) gitMinimal sops age ssh-to-age; }')
    if metadata_transport(metadata) == 'guest-fetch':
        ROOT_PHASE = 'setup-build'
        # Build the real production runtime before resolving its locked tools.
        nix(['build', '--no-link', '--no-update-lock-file', 'path:' + source + '#setup'],
            env, cwd=source, timeout=1800)
        ROOT_PHASE = 'locked-tools'
        rows = unique_json(nix(['build', '--json', '--no-link', '--no-update-lock-file',
                               '--expr', 'builtins.attrValues (' + expression + ')'], env, cwd=source, timeout=1800))
        require(isinstance(rows, list) and len(rows) == 4, 'bootstrap-build-outputs')
        realized = set()
        for row in rows:
            keys(row['outputs'], ('out',))
            output = row['outputs']['out']
            realized.add(store_path(output['path'] if isinstance(output, dict) else output))
    ROOT_PHASE = 'locked-tools'
    locked = ['--no-update-lock-file'] if metadata_transport(metadata) == 'guest-fetch' else []
    tools = unique_json(nix(['eval', '--json', *locked, '--expr',
                            'builtins.mapAttrs (_: p: p.outPath) (' + expression + ')'], env,
                           **({'cwd': source} if metadata_transport(metadata) == 'guest-fetch' else {})))
    keys(tools, ('gitMinimal', 'sops', 'age', 'ssh-to-age'))
    if metadata_transport(metadata) == 'nix-export':
        approved = {item['path'] for item in metadata['preparation']['objects']}
        require(all(store_path(path) in approved for path in tools.values()), 'bootstrap-not-approved')
    else:
        require({store_path(path) for path in tools.values()} == realized, 'bootstrap-not-realized')
        verify_tools(tools)
    return tools


def verify_tools(tools):
    keys(tools, ('gitMinimal', 'sops', 'age', 'ssh-to-age'))
    for name, binary in (('gitMinimal', 'git'), ('sops', 'sops'), ('age', 'age'), ('ssh-to-age', 'ssh-to-age')):
        executable = Path(store_path(tools[name])) / 'bin' / binary
        require(executable.is_file() and os.access(executable, os.X_OK), 'bootstrap-not-executable')


def extract_source(files):
    fixture = HOME / 'source-fixture'
    fixture.mkdir(mode=0o700, exist_ok=False)
    for name, (data, mode) in files.items():
        path = fixture / name[7:]
        path.parent.mkdir(parents=True, exist_ok=True)
        write_new(path, data, mode)
    return fixture


def github_prefetch_inputs(raw):
    """Validate the entire public lock before constructing any CDN request."""
    lock = unique_json(raw)
    keys(lock, ('version', 'root', 'nodes'))
    nodes = lock['nodes']
    require(type(lock['version']) is int and lock['version'] == 7
            and type(nodes) is dict and 0 < len(nodes) <= 128
            and type(lock['root']) is str and lock['root'] in nodes, 'input-prefetch-lock')
    require(type(nodes[lock['root']]) is dict and 'locked' not in nodes[lock['root']], 'input-prefetch-lock')
    inputs, count = set(), 0
    for node in nodes.values():
        require(type(node) is dict and set(node) <= {'inputs', 'locked', 'original', 'flake'},
                'input-prefetch-lock')
        if 'flake' in node:
            require(type(node['flake']) is bool, 'input-prefetch-lock')
        if 'inputs' in node:
            require(type(node['inputs']) is dict, 'input-prefetch-lock')
            for target in node['inputs'].values():
                require((type(target) is str and target in nodes)
                        or (type(target) is list and all(type(part) is str and part for part in target)),
                        'input-prefetch-lock')
        if 'locked' not in node:
            continue
        locked = node['locked']
        require(type(locked) is dict and type(locked.get('type')) is str, 'input-prefetch-lock')
        if locked['type'] != 'github':
            continue
        count += 1
        require(count <= 64, 'input-prefetch-lock')
        required = {'type', 'owner', 'repo', 'rev', 'narHash'}
        require(required <= set(locked) <= required | {'host', 'lastModified'}, 'input-prefetch-source')
        require(locked.get('host', 'github.com') == 'github.com', 'input-prefetch-source')
        if 'lastModified' in locked:
            require(type(locked['lastModified']) is int and locked['lastModified'] >= 0, 'input-prefetch-source')
        owner, repo, rev, nar_hash = (locked[k] for k in ('owner', 'repo', 'rev', 'narHash'))
        require(type(owner) is str and re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?', owner)
                is not None and type(repo) is str
                and re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,99}', repo) is not None
                and '..' not in repo, 'input-prefetch-source')
        require(type(rev) is str and re.fullmatch('[0-9a-f]{40}', rev) is not None, 'input-prefetch-source')
        require(type(nar_hash) is str and re.fullmatch(r'sha256-[A-Za-z0-9+/]{43}=', nar_hash) is not None,
                'input-prefetch-source')
        digest = base64.b64decode(nar_hash[7:], validate=True)
        require(len(digest) == 32 and base64.b64encode(digest).decode() == nar_hash[7:], 'input-prefetch-source')
        if 'original' in node:
            original = node['original']
            require(type(original) is dict
                    and {'type', 'owner', 'repo'} <= set(original) <= {'type', 'owner', 'repo', 'ref', 'rev', 'host'}
                    and all(original.get(k) == locked[k] for k in ('type', 'owner', 'repo'))
                    and original.get('host', 'github.com') == 'github.com', 'input-prefetch-source')
            require(all(type(original[k]) is str for k in ('ref', 'rev') if k in original), 'input-prefetch-source')
        inputs.add((owner, repo, rev, nar_hash))
    return sorted(inputs)


def emit_prefetch_progress(config, current, total, completed, step):
    validate_config(config)
    require(config['transport'] == 'guest-fetch', 'config-transport')
    require(os.getuid() == os.geteuid() == 0, 'guest-uid')
    require(type(step) is str and step in ('start', 'verified')
            and all(type(value) is int for value in (current, total, completed))
            and 1 <= current <= total <= 64
            and completed == current - (1 if step == 'start' else 0), 'invalid-closed-parameter')
    tick()
    emit_serial(PREFETCH_PREFIX, {'schemaVersion': 1, 'token': config['token'],
                                 'currentIndex': current, 'totalInputs': total,
                                 'completedInputs': completed, 'step': step})


def _prefetch_input_info(value, path, nar_hash):
    """Bind cache metadata to one complete native store path, never a basename."""
    if type(value) is list:
        require(len(value) == 1 and type(value[0]) is dict
                and value[0].get('path') == path, 'native-info')
        row = value[0]
    else:
        require(type(value) is dict, 'native-info')
        rows = value.get('info', value)
        require(type(rows) is dict and set(rows) == {path}, 'native-info')
        row = rows[path]
        require(type(row) is dict and row.get('path', path) == path, 'native-info')
    require(row.get('narHash') == nar_hash, 'input-prefetch-hash')
    require(type(row.get('narSize')) is int and row['narSize'] > 0
            and type(row.get('references')) is list, 'native-info')
    for ref in row['references']:
        store_path(ref)
    return native_info(value, path)


def _locked_prefetch_native(args, env, fixture, source, *, fixed=False, **kwargs):
    """Keep each cache operation within the input budget and original lock."""
    verify_lock(env, fixture)
    verify_lock(env, source)
    try:
        tick()
        timeout = DEADLINE - time.monotonic()
        if fixed:
            return checked([STORE, '--option', 'builders', '', *args], env=env,
                           merge=False, timeout=timeout)
        return nix(args, env, timeout=timeout, **kwargs)
    finally:
        primary = sys.exc_info()[1]
        try:
            with ExitStack() as checks:
                checks.callback(verify_lock, env, source)
                checks.callback(verify_lock, env, fixture)
        except BaseException:
            if primary is None:
                raise


def prefetch_github_inputs(metadata, env, fixture, config):
    """Cache only the original locked NARs; native archive still verifies its graph."""
    global DEADLINE
    validate_config(config)
    require(config['transport'] == metadata_transport(metadata) == env_transport(env) == 'guest-fetch', 'fetch-transport')
    require(os.getuid() == os.geteuid() == 0, 'guest-uid')
    tick()
    full_deadline = DEADLINE
    DEADLINE = min(full_deadline, time.monotonic() + 1800)
    source = Path(metadata['source']['path'])
    try:
        expected = metadata['source']['lockSha256']
        require(env.get('ENVY_RUNTIME_LOCK_SHA256') == expected, 'lock-drift')
        raw = private_read(fixture / 'flake.lock', MAX_METADATA)
        require(sha(raw) == expected, 'lock-drift')
        require(sha(private_read(source / 'flake.lock', MAX_METADATA)) == expected, 'lock-drift')
        inputs = github_prefetch_inputs(raw)
        for index, (owner, repo, rev, nar_hash) in enumerate(inputs, 1):
            tick()
            stage_deadline = DEADLINE
            DEADLINE = min(stage_deadline, time.monotonic() + 600)
            try:
                emit_prefetch_progress(config, index, len(inputs), index - 1, 'start')
                tick()
                url = 'https://codeload.github.com/' + owner + '/' + repo + '/tar.gz/' + rev
                canonical = None
                if owner == 'NixOS' and repo == 'nixpkgs':
                    printed = _locked_prefetch_native(
                        ['--print-fixed-path', '--recursive', 'sha256', nar_hash, 'source'],
                        env, fixture, source, fixed=True).decode('ascii')
                    require(re.fullmatch(PATH_RE + r'\n?', printed) is not None, 'input-prefetch-result')
                    canonical = store_path(printed.removesuffix('\n'))
                    require(re.fullmatch(r'/nix/store/[0-9abcdfghijklmnpqrsvwxyz]{32}-source', canonical)
                            is not None, 'input-prefetch-result')
                    _locked_prefetch_native(['copy', '--from', 'https://cache.nixos.org',
                                             '--option', 'require-sigs', 'true', '--option', 'http2', 'false',
                                             '--log-format', 'internal-json', canonical],
                                            env, fixture, source, _cache_copy_path=canonical)
                    _prefetch_input_info(unique_json(_locked_prefetch_native(
                        ['path-info', '--json', canonical], env, fixture, source)), canonical, nar_hash)
                args = ['store', 'prefetch-file', '--unpack', '--name', 'source', '--json',
                        '--log-format', 'internal-json', '--expected-hash', nar_hash, url]
                result = unique_json(
                    _locked_prefetch_native(args, env, fixture, source, _prefetch_url=url)
                    if canonical is not None else
                    nix(args, env, timeout=DEADLINE - time.monotonic(), _prefetch_url=url))
                require(type(result) is dict, 'input-prefetch-result')
                path = store_path(result.get('storePath'))
                require(result.get('hash') == nar_hash, 'input-prefetch-hash')
                require(canonical is None or path == canonical, 'input-prefetch-result')
                tick()
                args = ['path-info', '--json', path]
                info = unique_json(
                    _locked_prefetch_native(args, env, fixture, source) if canonical is not None else
                    nix(args, env, timeout=DEADLINE - time.monotonic()))
                actual = (_prefetch_input_info(info, path, nar_hash) if canonical is not None else
                          native_info(info, path))
                require(actual['narHash'] == nar_hash, 'input-prefetch-hash')
                tick()
                verify_lock(env, fixture)
                verify_lock(env, source)
                emit_prefetch_progress(config, index, len(inputs), index, 'verified')
            finally:
                DEADLINE = stage_deadline
        tick()
    finally:
        primary = sys.exc_info()[1]
        try:
            with ExitStack() as checks:
                checks.callback(verify_lock, env, source)
                checks.callback(verify_lock, env, fixture)
        except BaseException:
            if primary is None:
                raise
        finally:
            DEADLINE = full_deadline


def fetch_payload(metadata, env, fixture, config):
    global ROOT_PHASE
    validate_config(config)
    require(config['transport'] == metadata_transport(metadata) == env_transport(env) == 'guest-fetch', 'fetch-transport')
    source = metadata['source']['path']
    ROOT_PHASE = 'source-add'
    added = nix(['store', 'add-path', '--name', 'source', str(fixture)], env).decode().strip()
    require(added == source, 'native-source-path')
    ROOT_PHASE = 'source-info'
    actual = native_info(unique_json(nix(['path-info', '--json', source], env)), source)
    require(actual['narHash'] == metadata['source']['narHash']
            and type(actual['narSize']) is int and actual['narSize'] == metadata['sourceNarSize'],
            'native-source-nar')
    ROOT_PHASE = 'input-prefetch'
    prefetch_github_inputs(metadata, env, fixture, config)
    ROOT_PHASE = 'flake-archive'
    # This is the first evaluation of any repository code. Native Nix owns
    # locked input fetching, TLS, hashes, and official cache signatures.
    try:
        archive = unique_json(nix(['flake', 'archive', '--json', '--no-update-lock-file', 'path:' + source],
                                 env, timeout=1800))
    finally:
        verify_lock(env, fixture)
        verify_lock(env, Path(source))
    require(archive['path'] == source, 'archive-source')
    try:
        return bootstrap_tools(metadata, env)
    finally:
        verify_lock(env, fixture)
        verify_lock(env, Path(source))


def install_official(config, env):
    path = Path('/usr/local/lib/envy-runtime-install-probe.py')
    # This file is root-owned cloud-init data containing install.guest_program;
    # only the non-root entry is invoked. Its root main would power off.
    output = checked(['/usr/bin/sudo', '-n', '-u', 'envy-runtime', '--', '/usr/bin/env', '-i',
                      *[k + '=' + v for k, v in env.items()], '/usr/bin/python3', str(path)],
                     env=env, timeout=config['installTimeout'] + 120)
    receipts = [unique_json(line[len(INSTALL_PREFIX):]) for line in output.decode('utf-8', 'replace').splitlines()
                if line.startswith(INSTALL_PREFIX)]
    require(len(receipts) == 1, 'official-install-receipt')
    row = receipts[0]
    require(row['token'] == config['token'] and row['arch'] == 'x86_64' and row['version'] == '2.33.1'
            and row['error'] is None and all(row.get(key) is True for key in
            ('initialNoNix', 'daemon', 'daemonService', 'profile', 'installerExitZero'))
            and type(row['buildUsers']) is int and 0 < row['buildUsers'] <= 128
            and all(value is True for value in row['stages'].values()), 'official-install-incomplete')
    return row


def write_new(path, data, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def system_ca_metadata():
    """Guest-root only call site; inspect the image's public bundle, never bytes."""
    info = os.stat(SYSTEM_CA_BUNDLE, follow_symlinks=False)
    facts = {'regularFile': stat.S_ISREG(info.st_mode), 'rootOwned': info.st_uid == 0,
             'notGroupOtherWritable': not bool(info.st_mode & 0o022)}
    require(facts['regularFile'], 'file-type-or-size')
    require(facts['rootOwned'], 'file-owner')
    require(facts['notGroupOtherWritable'], 'file-mode')
    return facts


def configure_daemon_ca(env):
    """Install one exclusive runtime override in the guarded disposable guest."""
    require(os.getuid() == os.geteuid() == 0, 'guest-uid')
    require(env_transport(env) == 'guest-fetch', 'environment-mode-mismatch')
    tick()
    # Anchor every component to an open directory; no symlink traversal or
    # reuse of a preexisting service drop-in directory is permitted.
    with ExitStack() as stack:
        parent = None
        for component in ('/', 'run', 'systemd', 'system'):
            fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                         **({'dir_fd': parent} if parent is not None else {}))
            stack.callback(os.close, fd)
            info = os.fstat(fd)
            require(stat.S_ISDIR(info.st_mode), 'file-type-or-size')
            require(info.st_uid == 0, 'file-owner')
            require(not info.st_mode & 0o022, 'file-mode')
            parent = fd
        os.mkdir('nix-daemon.service.d', mode=0o700, dir_fd=parent)
        fd = os.open('nix-daemon.service.d', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        stack.callback(os.close, fd)
        info = os.fstat(fd)
        require(stat.S_ISDIR(info.st_mode), 'file-type-or-size')
        require(info.st_uid == 0, 'file-owner')
        require(stat.S_IMODE(info.st_mode) == 0o700, 'file-mode')
        file_fd = os.open('envy-runtime-ca.conf', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          0o600, dir_fd=fd)
        with os.fdopen(file_fd, 'wb') as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'file-type-or-size')
            require(info.st_uid == 0, 'file-owner')
            require(stat.S_IMODE(info.st_mode) == 0o600, 'file-mode')
            stream.write(('[Service]\nEnvironment=NIX_SSL_CERT_FILE=' + SYSTEM_CA_BUNDLE + '\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
    checked(['/usr/bin/systemctl', 'daemon-reload'], env=env, timeout=15)
    checked(['/usr/bin/systemctl', 'restart', 'nix-daemon.service'], env=env, timeout=30)
    active = checked(['/usr/bin/systemctl', 'is-active', 'nix-daemon.service'], env=env, timeout=10)
    require(active == b'active\n', 'daemon-inactive')


def native_tls_probe(config, env):
    """Read only one setting and the official cache; never emit native text."""
    require(os.getuid() == os.geteuid() == 0, 'guest-uid')
    require(config['transport'] == env_transport(env) == 'guest-fetch', 'environment-mode-mismatch')
    for label, args in (('nix-ca-setting', ['config', 'show', 'ssl-cert-file']),
                        ('nix-cache-ping', ['store', 'ping', '--store', 'https://cache.nixos.org', '--json'])):
        count = len(COMMAND_FACTS)
        code, matches, error = 0, None, None
        try:
            output = nix(args, env, timeout=15)
            if label == 'nix-ca-setting':
                matches = output == (SYSTEM_CA_BUNDLE + '\n').encode()
        except GuestFailure as exc:
            if exc.args != ('command-failed',):
                raise
            error, code = 'command-failed', None
            if len(COMMAND_FACTS) == count + 1:
                value = COMMAND_FACTS[-1].get('exitCode')
                if type(value) is int and -255 <= value <= 255:
                    code = value
        emit_serial(NATIVE_TLS_PREFIX, {'schemaVersion': 1, 'token': config['token'],
                                       'endpoint': label, 'exitCode': code,
                                       'settingMatchesSystemBundle': matches, 'error': error})


def public_tls_values(output):
    # curl's write-out is the only accepted stdout grammar. Unknown/partial
    # output is not evidence of verification or an HTTP response.
    match = re.fullmatch(rb'([0-9]{1,10}) ([0-9]{3})\n', output) if type(output) is bytes else None
    if match is None:
        return None, None
    verify, status = map(int, match.groups())
    return (verify if verify <= 2147483647 else None,
            status if 100 <= status <= 599 else None)


def public_tls_probe(config, env, bundle):
    require(config['transport'] == env_transport(env) == 'guest-fetch', 'environment-mode-mismatch')
    keys(bundle, ('regularFile', 'rootOwned', 'notGroupOtherWritable'))
    require(all(value is True for value in bundle.values()), 'closed-data-schema')
    for label, endpoint in PUBLIC_TLS_ENDPOINTS:
        # -q must be first: no curlrc, credentials, proxy or inherited env.
        # HEAD 4xx is useful evidence; --fail would obscure this distinction.
        code, output = command(
            ['/usr/bin/curl', '-q', '--head', '--output', '/dev/null',
             '--write-out', '%{ssl_verify_result} %{http_code}\n',
             '--silent', '--show-error', '--max-time', '12', '--proto', '=https',
             '--cacert', SYSTEM_CA_BUNDLE, endpoint],
            env={'LC_ALL': 'C.UTF-8'}, timeout=12, merge=False)
        require(type(code) is int and -255 <= code <= 255, 'command-failed')
        verify, status = public_tls_values(output)
        emit_serial(TLS_PREFIX, {'schemaVersion': 1, 'token': config['token'],
                                'bundle': dict(bundle), 'endpoint': label, 'exitCode': code,
                                'sslVerifyResult': verify, 'httpStatus': status})


def root_stages(config):
    global ROOT_PHASE
    guard(config, root=True, mounted=False)
    transport = config['transport']
    bundle = system_ca_metadata() if transport == 'guest-fetch' else None
    env = environment(transport=transport)
    require(not os.path.lexists('/nix') and not os.path.lexists(REPO), 'fresh-guest-required')
    SEED.mkdir(mode=0o755, exist_ok=False)
    checked(['/usr/bin/mount', '-o', 'ro,nosuid,nodev,noexec', '/dev/sr0', str(SEED)], env=env)
    guard(config, root=True)
    ROOT_PHASE = 'seed-payload'
    fd = payload_fd('acceptance.json', {'size': config['metadataSize'], 'sha256': config['metadataSha256']})
    try:
        metadata = unique_json(os.read(fd, config['metadataSize'] + 1))
    finally:
        os.close(fd)
    validate_metadata(metadata, transport)
    if transport == 'guest-fetch':
        env = environment(transport=transport, lock_sha256=metadata['source']['lockSha256'])
    source_fd = payload_fd('source.tar', metadata['snapshot'])
    export_fd = None
    try:
        with os.fdopen(os.dup(source_fd), 'rb') as stream:
            files = source_members(stream.read(MAX_SOURCE + 1024**2 + 1), metadata)
        if transport == 'guest-fetch':
            fixture = extract_source(files)
        else:
            export_fd = payload_fd('dependencies.export', metadata['preparation']['artifact'])
        ROOT_PHASE = 'official-install'
        installed = install_official(config, env)
        emit_serial(INSTALL_PREFIX, installed)
        if transport == 'guest-fetch':
            ROOT_PHASE = 'daemon-ca'
            configure_daemon_ca(env)
            public_tls_probe(config, env, bundle)
            ROOT_PHASE = 'native-tls'
            native_tls_probe(config, env)
            tools = fetch_payload(metadata, env, fixture, config)
        else:
            ROOT_PHASE = 'payload-import'
            import_payload(export_fd, metadata, env)
    finally:
        os.close(source_fd)
        if export_fd is not None:
            os.close(export_fd)
    if transport == 'nix-export':
        tools = bootstrap_tools(metadata, env)
        fixture = extract_source(files)
    identity = pwd.getpwnam('envy-runtime')
    for parent, directories, names in os.walk(fixture):
        os.chown(parent, identity.pw_uid, identity.pw_gid)
        for name in names:
            os.chown(Path(parent) / name, identity.pw_uid, identity.pw_gid)
    write_new(CONTEXT, json.dumps({'metadata': metadata, 'tools': tools}).encode(), 0o444)
    # pam_systemd must create an actual user login session and bus. Failure to
    # obtain them is fatal in guard(); no synthetic XDG runtime is constructed.
    ROOT_PHASE = 'user-session'
    checked(['/usr/bin/loginctl', 'enable-linger', 'envy-runtime'], env=env)
    checked(['/usr/bin/systemctl', 'start', 'user@' + str(identity.pw_uid) + '.service'], env=env)
    argv = ['/usr/bin/systemd-run', '--quiet', '--wait', '--collect', '--pty',
            '--unit=envy-runtime-lifecycle.service', '--uid=envy-runtime', '--property=PAMName=login',
            '--property=KillMode=control-group', '--property=TimeoutStopSec=5',
            '--property=RuntimeMaxSec=' + str(config['fullTimeout']),
            '--property=WorkingDirectory=' + str(HOME)]
    for key, value in environment(tools=tools, transport=transport,
                                  lock_sha256=metadata['source']['lockSha256'] if transport == 'guest-fetch' else None).items():
        argv.append('--setenv=' + key + '=' + value)
    try:
        checked([*argv, '/usr/bin/python3', SCRIPT, 'user'], env=env, terminal=True, timeout=config['fullTimeout'])
    finally:
        stop_user_unit(env)
    ROOT_PHASE = 'user-result'
    row = unique_json(private_read(RESULT, 256 * 1024, owner=identity.pw_uid, mode=0o600))
    require(row['token'] == config['token'] and row['source'] == metadata['source'], 'user-result-binding')
    if transport == 'guest-fetch':
        require(row.get('transport') == transport
                and row.get('evidence') == 'linux-vm-envy-lifecycle-networked', 'user-result-transport')
    else:
        require('transport' not in row and row.get('evidence') == 'linux-vm-envy-lifecycle', 'user-result-transport')
    row['install'] = installed
    observations = unique_json(private_read(OBSERVATIONS, 128 * 1024, owner=identity.pw_uid, mode=0o600))
    keys(observations, ('token', 'commandCount', 'lastCommands'))
    require(observations['token'] == config['token'], 'observations-binding')
    for fact in observations['lastCommands']:
        keys(fact, ('exitCode', 'outputBytes', 'outputSha256'))
    emit_serial(OBSERVATIONS_PREFIX, observations)
    emit_serial(PREFIX, row)
    return row



def stop_user_unit(env):
    global DEADLINE
    previous = DEADLINE
    DEADLINE = time.monotonic() + 15
    try:
        command(['/usr/bin/systemctl', 'kill', '--kill-whom=all', '--signal=KILL',
                 'envy-runtime-lifecycle.service'], env=env, timeout=5)
        # --collect may already have removed a completed unit. Nonzero means
        # absent/failed, not successful cleanup; poweroff is still mandatory.
        command(['/usr/bin/systemctl', 'stop', 'envy-runtime-lifecycle.service'], env=env, timeout=8)
    finally:
        DEADLINE = previous


def git(argv, env, *, cwd):
    return checked([env['PATH'].split(':')[0] + '/git', '-c', 'core.hooksPath=/dev/null',
                    '-c', 'commit.gpgsign=false', '-c', 'tag.gpgsign=false', *argv], env=env, cwd=cwd)


def prepare_checkout(env):
    fixture = HOME / 'source-fixture'
    git(['init', '--initial-branch=master', str(fixture)], env, cwd=str(HOME))
    git(['add', '--all'], env, cwd=str(fixture))
    git(['commit', '--no-gpg-sign', '-m', 'test(runtime): freeze synthetic guest source'], env, cwd=str(fixture))
    git(['clone', '--no-hardlinks', '--', str(fixture), str(REPO)], env, cwd=str(HOME))
    require(git(['rev-list', '--count', 'HEAD'], env, cwd=str(REPO)).strip() == b'1', 'synthetic-commit-count')
    require(not git(['remote', 'get-url', 'origin'], env, cwd=str(REPO)).strip().startswith(b'http'),
            'synthetic-remote')
    # Remove the synthetic origin too; install.sh exercises existing-checkout.
    git(['remote', 'remove', 'origin'], env, cwd=str(REPO))
    checked(['/bin/bash', str(REPO / 'checks/runtime/seed-secrets.sh')], env=env, cwd=str(REPO))
    machine = REPO / 'hosts/linux/runtime-min.nix'
    original = private_read(machine, 1024**2).decode()
    match = re.fullmatch(r'import ../../checks/runtime/fixture.nix (\{.*\})\s*', original, re.S)
    require(match is not None, 'minimal-machine-source')
    # This non-managed declaration must survive the real setup writer.
    machine.write_text(BASELINE.replace('@ARGS@', match[1]))
    return git(['rev-parse', 'HEAD'], env, cwd=str(REPO)).strip()



def normalize_seed_identity():
    # snapshot.GUEST_SEED_SCRIPT creates a valid sops encryption rule but lacks
    # the managed keys/anchors required by keys.storage.read_sops_yaml_keys.
    # Use production writers only after reentry with the production Python.
    sys.path.insert(0, str(REPO / 'resources/scripts'))
    from envy.keys.storage import current_device_public_key, write_sops_yaml_keys
    from envy.keys.identity import set_sops_label
    public = current_device_public_key()
    require(isinstance(public, str) and re.fullmatch(r'age1[0-9a-z]+', public) is not None,
            'synthetic-recipient-unavailable')
    write_sops_yaml_keys({'runtime_fixture': public})
    set_sops_label('runtime_fixture')


def setup_stage(env, revision):
    child_env = env.copy()
    child_env['ENVY_DEBUG'] = '1'
    dialogue = SetupDialogue()
    try:
        try:
            code, _ = command(['/bin/bash', str(REPO / 'install.sh'), '--mirror', 'upstream', '--target', str(REPO)],
                              env=child_env, cwd=str(HOME), terminal=True, dialogue=dialogue, timeout=1800)
        finally:
            primary = sys.exc_info()[1]
            try:
                verify_lock(env)
            except (KeyboardInterrupt, GuestFailure, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                if primary is None:
                    raise
        result = dialogue.finish(code)
        require(git(['rev-parse', 'HEAD'], env, cwd=str(REPO)).strip() == revision
                and git(['rev-list', '--count', 'HEAD'], env, cwd=str(REPO)).strip() == b'1', 'setup-unexpected-commit')
        require('home.file.".config/envy-runtime/proof".text' in
                private_read(REPO / 'hosts/linux/runtime-min.nix', 1024**2).decode(), 'setup-lost-proof')
        return result
    except (KeyboardInterrupt, GuestFailure, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        exc.observation = dialogue.failure_observation(exc)
        raise


def backend():
    # Load only the verified, guest-owned checkout after guard and preparation.
    sys.path.insert(0, str(REPO / 'resources/scripts'))
    from envy import journal
    from envy.workflows.generations import current_generation
    return journal, current_generation


def journal_summary(entry):
    source = entry.get('source') or {}
    stages = entry.get('stages', [])
    def status(name):
        values = [s['status'] for s in stages if s['name'] == name]
        return values[-1] if values else None
    return {'operationId': entry['operationId'], 'operation': entry['operation'],
            'result': entry['result'], 'terminal': entry['terminal'],
            'journalPersisted': entry['journalPersisted'], 'sourceVerified': source.get('verified') is True,
            'sourceFrozenPath': source.get('frozenPath'), 'sourceOutputPath': source.get('outputPath'),
            'bindingTarget': (entry.get('binding') or {}).get('target'), 'activateStatus': status('activate'),
            'verifyStatus': status('verify'), 'conflictPartial': entry.get('conflict') is True}


def validate_journal(entry, *, fault):
    summary = journal_summary(entry)
    expected = 'partial' if fault else 'ok'
    require(summary['result'] == summary['terminal'] == expected and summary['journalPersisted'] is True
            and summary['sourceVerified'] is True, 'journal-terminal-unverified')
    store_path(summary['sourceFrozenPath'])
    store_path(summary['sourceOutputPath'])
    require(summary['activateStatus'] == expected and summary['conflictPartial'] is fault,
            'journal-activation-unverified')
    require(summary['verifyStatus'] is None and summary['bindingTarget'] is None if fault else
            summary['verifyStatus'] == 'ok' and summary['bindingTarget'] == summary['sourceOutputPath'],
            'journal-binding-unverified')
    return summary


def secret_facts():
    def fact(path):
        # Only stat metadata: never open an age identity or decrypted value.
        try:
            info = path.stat()
            return {'exists': stat.S_ISREG(info.st_mode), 'mode0600': stat.S_IMODE(info.st_mode) == 0o600}
        except FileNotFoundError:
            return {'exists': False, 'mode0600': False}
    key = fact(HOME / '.config/sops/age/keys.txt')
    decrypted = {name: fact(Path(os.environ['XDG_RUNTIME_DIR']) / 'secrets' / name) for name in SECRET_NAMES}
    require(key == {'exists': True, 'mode0600': True}
            and all(row == key for row in decrypted.values()), 'secret-permissions')
    return {'ageKeyPresent': key['exists'], 'ageKeyMode0600': key['mode0600'], 'decrypted': decrypted}


def managed_facts():
    path = HOME / PROOF_REL
    require(path.is_symlink(), 'proof-not-home-manager-link')
    target = path.resolve(strict=True)
    require(str(target).startswith('/nix/store/'), 'proof-not-store-backed')
    data = target.read_bytes()
    require(data == PROOF.encode(), 'proof-content')
    modes = {'link': stat.S_IMODE(path.lstat().st_mode), 'target': stat.S_IMODE(target.stat().st_mode)}
    return sha(data), sha(json.dumps(modes, sort_keys=True).encode())


def apply_stage(env, *, fault=False, tui=False):
    observation = {'status': 'partial-or-unknown'}
    try:
        journal, generation = backend()
        before = {row['operationId'] for row in journal.read(operation='apply', strict=True)}
        try:
            code, output = command([NIX, *offline_args(env), 'run', '--no-update-lock-file', 'path:' + str(REPO) + '#envy',
                                    '--', 'apply'], env=env, cwd=str(REPO), timeout=1800)
            observation.update(exitCode=code, outputBytes=len(output), outputSha256=sha(output))
        finally:
            verify_lock(env)
        entries = [row for row in journal.read(operation='apply', strict=True) if row['operationId'] not in before]
        require(len(entries) == 1, 'apply-operation-cardinality')
        entry = entries[0]
        observation['journal'] = journal_summary(entry)
        summary = validate_journal(entry, fault=fault)
        require(entry.get('exitCode') == code, 'apply-exit-unbound')
        current = generation()
        observation['observedGeneration'] = {'number': current.number, 'target': str(current.target)} if current else None
        if fault:
            require(code == 97 and b'ENVY_RUNTIME_FIXED_FAULT_97' in output, 'fault-not-activated')
            policy = journal.retry_policy(entry)
            require(policy['allowed'] is False and policy['step'] == 'verify-generation', 'fault-retry-allowed')
            try:
                journal.verify_retry(entry['operationId'], 'verify-generation')
            except ValueError:
                pass
            else:
                raise GuestFailure('fault-retry-not-rejected')
            return {'exitCode': code, 'target': summary['sourceOutputPath'], 'journal': summary,
                    'observedGeneration': {'number': current.number, 'target': str(current.target)} if current else None,
                    'injectedAfter': 'linkGeneration', 'retryAllowed': policy['allowed'],
                    'originalMachineRestored': False, 'partialReadableAfterRecovery': False}
        require(code == 0 and current is not None and str(current.target) == summary['sourceOutputPath'],
                'apply-generation-unverified')
        content, permissions = managed_facts()
        return {'exitCode': code, 'target': str(current.target), 'generation': current.number, 'journal': summary,
                'managedSha256': content, 'permissionsSha256': permissions, 'secrets': secret_facts(),
                'tuiBuildTests': tui}
    except (KeyboardInterrupt, GuestFailure, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        exc.observation = observation
        raise


def tui_tests(env):
    target = 'path:' + str(REPO) + '#checks.x86_64-linux.envy-tui-tests'
    drv = nix(['eval', '--raw', target + '.drvPath'], env, cwd=str(REPO)).decode().strip()
    store_path(drv)
    # Inspect doCheck and then force a guest rebuild, so a substituted output
    # cannot masquerade as an observed test run.
    row = unique_json(nix(['derivation', 'show', drv], env))
    rows = row.get('derivations', row)
    detail = rows.get(drv, rows.get(Path(drv).name))
    require(detail is not None and detail.get('env', {}).get('doCheck') == '1', 'tui-check-disabled')
    nix(['build', '--no-link', '--no-update-lock-file', target], env, cwd=str(REPO), timeout=1800)
    nix(['build', '--rebuild', '--no-link', '--no-update-lock-file', target], env, cwd=str(REPO), timeout=1800)
    return True


def lifecycle(env, revision):
    stages = {}
    active = 'setup'
    try:
        stages['setup'] = setup_stage(env, revision)
        active = 'firstApply'
        tested = tui_tests(env)
        stages['firstApply'] = apply_stage(env, tui=tested)
        active = 'repeatApply'
        stages['repeatApply'] = apply_stage(env, tui=tested)
        first = stages['firstApply']
        require(all(stages['repeatApply'][key] == first[key] for key in
                    ('target', 'managedSha256', 'permissionsSha256')), 'repeat-drift')
        machine = REPO / 'hosts/linux/runtime-min.nix'
        baseline = private_read(machine, 1024**2)
        original_path = REPO / 'hosts/linux/runtime-original.nix'
        # Keep the original module in the same directory for relative imports;
        # this file is removed before recovery, restoring the source byte set.
        write_new(original_path, baseline, 0o644)
        machine.write_text(FAULT)
        active = 'faultApply'
        stages['faultApply'] = apply_stage(env, fault=True)
        require(stages['faultApply']['target'] != first['target'], 'fault-target-unchanged')
        machine.write_bytes(baseline)
        original_path.unlink()
        require(private_read(machine, 1024**2) == baseline, 'machine-not-restored')
        stages['faultApply']['originalMachineRestored'] = True
        active = 'recoveryApply'
        stages['recoveryApply'] = apply_stage(env, tui=tested)
        require(all(stages['recoveryApply'][key] == first[key] for key in
                    ('target', 'managedSha256', 'permissionsSha256')), 'recovery-drift')
        journal, _ = backend()
        old = next((row for row in journal.read(operation='apply', strict=True)
                    if row['operationId'] == stages['faultApply']['journal']['operationId']), None)
        require(old is not None and validate_journal(old, fault=True) == stages['faultApply']['journal'],
                'old-partial-lost')
        stages['faultApply']['partialReadableAfterRecovery'] = True
        return stages, None
    except KeyboardInterrupt as exc:
        if hasattr(exc, 'observation'):
            stages[active] = exc.observation
        return stages, 'cancelled'
    except (GuestFailure, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        if hasattr(exc, 'observation'):
            stages[active] = exc.observation
        # No rollback claim: an in-flight apply remains partial/unknown in the
        # real backend journal, and no later destructive stage runs.
        return stages, 'failed-or-unknown:' + active


def user_stages(config):
    guard(config, root=False)
    context = unique_json(private_read(CONTEXT, MAX_METADATA, owner=0, mode=0o444))
    keys(context, ('metadata', 'tools'))
    transport = config['transport']
    approved = validate_metadata(context['metadata'], transport)
    require(sha((json.dumps(context['metadata'], sort_keys=True) + '\n').encode()) == config['metadataSha256'],
            'context-metadata-binding')
    keys(context['tools'], ('gitMinimal', 'sops', 'age', 'ssh-to-age'))
    if transport == 'nix-export':
        require(all(store_path(path) in approved for path in context['tools'].values()), 'context-tools-binding')
    else:
        verify_tools(context['tools'])
    env = environment(user=True, tools=context['tools'], transport=transport,
                      lock_sha256=context['metadata']['source']['lockSha256'] if transport == 'guest-fetch' else None)
    os.environ.clear()
    os.environ.update(env)
    require(checked(['/usr/bin/systemctl', '--user', 'is-active', 'default.target'], env=env).strip() == b'active',
            'user-manager-inactive')
    if sys.argv[1:] == ['user']:
        prepare_checkout(env)
    else:
        normalize_seed_identity()
    # Only the full-default manifest is evaluated, never built or activated.
    nix(['eval', '--json', '--no-update-lock-file', 'path:' + str(REPO) +
         '#homeConfigurations.runtime-default.config.envy.machine.manifest'], env, cwd=str(REPO))
    # Build the production runtime in the guest; ENVY_DEV_SHELL is never set.
    for name in ('setup', 'envy'):
        nix(['build', '--no-link', '--no-update-lock-file', 'path:' + str(REPO) + '#' + name],
            env, cwd=str(REPO), timeout=1800)
    if sys.argv[1:] == ['user']:
        # Resolve the interpreter from the actual production setup wrapper,
        # rather than duplicating mkEnvyPython's dependency recipe.
        rows = unique_json(nix(['build', '--json', '--no-link', '--no-update-lock-file',
                                'path:' + str(REPO) + '#setup'], env, cwd=str(REPO)))
        require(isinstance(rows, list) and len(rows) == 1, 'setup-runtime-output')
        output = rows[0]['outputs']['out']
        if isinstance(output, dict):
            output = output['path']
        store_path(output)
        wrapper = private_read(Path(output) / 'bin/envy-setup', 1024**2, single_link=False).decode()
        candidates = sorted(set(re.findall(PATH_RE + r'/bin', wrapper)))
        interpreters = [path + '/python3' for path in candidates if (Path(path) / 'python3').is_file()]
        require(len(interpreters) == 1, 'production-python-unavailable')
        checked([interpreters[0], '-c', 'import typer, rich, prompt_toolkit, yaml'], env=env)
        os.execve(interpreters[0], [interpreters[0], SCRIPT, 'lifecycle'], env)
        raise GuestFailure('runtime-exec-returned')
    revision = git(['rev-parse', 'HEAD'], env, cwd=str(REPO)).strip()
    stages, error = lifecycle(env, revision)
    row = {'schemaVersion': 1, 'evidence': 'linux-vm-envy-lifecycle-networked' if transport == 'guest-fetch'
           else 'linux-vm-envy-lifecycle', 'arch': platform.machine(),
           'token': config['token'], 'source': context['metadata']['source'], 'install': None,
           'stages': stages, 'error': error}
    if transport == 'guest-fetch':
        row['transport'] = transport
    RESULT.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    observations = {'token': config['token'], 'commandCount': len(COMMAND_FACTS),
                    'lastCommands': COMMAND_FACTS[-256:]}
    write_new(OBSERVATIONS, json.dumps(observations, sort_keys=True).encode())
    write_new(RESULT, json.dumps(row, sort_keys=True).encode())
    return row


def main(config):
    global DEADLINE, ROOT_PHASE
    validate_config(config)
    DEADLINE = time.monotonic() + config['fullTimeout']
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, cancelled)
    root = len(sys.argv) == 1
    require(root or sys.argv[1:] in (['user'], ['lifecycle']), 'closed-entrypoint')
    # No cleanup or poweroff happens before a successful guest guard.
    guard(config, root=root, mounted=not root)
    ROOT_PHASE = 'root-stages'
    try:
        if root:
            return root_stages(config)
        return user_stages(config)
    except KeyboardInterrupt:
        if root:
            emit_diagnostic(config, GuestFailure('cancelled'))
        else:
            print('ENVY_RUNTIME_GUEST_FAILURE cancelled', flush=True)
        return None
    except (GuestFailure, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        if root:
            emit_diagnostic(config, exc)
        else:
            print('ENVY_RUNTIME_GUEST_FAILURE failed-or-unknown', flush=True)
        return None
    finally:
        if root:
            DEADLINE = time.monotonic() + 12
            checked(['/usr/bin/systemctl', 'poweroff', '--no-block'], env=environment(transport=config['transport']), timeout=10)


def seed_entrypoint(config):
    """Report an unhandled guard refusal without cleanup on an unproven host."""
    try:
        return 0 if main(config) is not None else 1
    except (GuestFailure, OSError) as exc:
        # Never serialize raw exceptions, environment values or paths. Even a
        # syntactically safe but unreviewed error maps to the fixed fallback.
        code = exc.args[0] if isinstance(exc, GuestFailure) and len(exc.args) == 1 else None
        if type(code) is not str or code not in ABORT_CODES:
            code = 'unknown'
        token = config.get('token') if isinstance(config, dict) else None
        if type(token) is str and re.fullmatch('[0-9a-f]{64}', token) is not None:
            print(ABORT_PREFIX + json.dumps({'schemaVersion': 1, 'token': token, 'error': code},
                                           sort_keys=True), flush=True)
        return 1


def guest_program(token, metadata, *, install_timeout=1200, full_timeout=7200):
    """Render a fixed program. No command, host path or shell text parameter."""
    validate_metadata(metadata)
    raw = (json.dumps(metadata, sort_keys=True) + '\n').encode()
    config = {'token': token, 'metadataSha256': sha(raw), 'metadataSize': len(raw),
              'installTimeout': install_timeout, 'fullTimeout': full_timeout, 'transport': metadata_transport(metadata)}
    validate_config(config)
    return 'RUN_CONFIG = ' + repr(config) + '\n' + Path(__file__).read_text()


def seed_data(token, metadata, *, install_timeout=1200, full_timeout=7200):
    import install
    program = guest_program(token, metadata, install_timeout=install_timeout, full_timeout=full_timeout)
    # Reuse the official installer seed unchanged except its root launch. The
    # closed lifecycle owns poweroff and invokes only its non-root user entry.
    user_data, instance = install.seed_data(token, install_timeout)
    old = 'runcmd:\n  - [ /usr/bin/python3, /usr/local/lib/envy-runtime-install-probe.py ]\n'
    require(user_data.endswith(old), 'official-seed-boundary-changed')
    additions = ('  - path: ' + str(MARKER) + '\n    owner: root:root\n    permissions: \'0444\'\n'
                 '    content: ' + json.dumps(token + '\n') + '\n'
                 '  - path: ' + SCRIPT + '\n    owner: root:root\n    permissions: \'0555\'\n'
                 '    content: ' + json.dumps(program) + '\n'
                 'runcmd:\n  - [ /usr/bin/python3, ' + SCRIPT + ' ]\n')
    return user_data[:-len(old)] + additions, instance


if __name__ == '__main__':
    # A bare host invocation is inert, even on Linux with UID 0.
    if 'RUN_CONFIG' not in globals():
        raise SystemExit('Guest seed rendering required; direct execution is forbidden.')
    raise SystemExit(seed_entrypoint(globals()['RUN_CONFIG']))
