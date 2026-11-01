#!/usr/bin/env python3
"""floatie ui — optional local viewing & management pane for one workspace.

Stdlib only (no third-party imports). Importing this module is side-effect-free;
the server starts only under `if __name__ == "__main__":`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import stat
import tempfile
from decimal import Decimal
from pathlib import Path
import os
import re
import secrets as _secrets
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ui_assets import static_response

# Editable keys use the same plain scalars / indented block lists as the justfile.
_LIST_KEYS = ("repos", "egress", "plugins")
_CONFIG_KEYS = {"cpus": "cpus", "memoryGiB": "memory", "permissive": "permissive",
                "gitUserName": "git_user_name", "gitUserEmail": "git_user_email",
                **{key: key for key in _LIST_KEYS}}
CONFIG_LOCK = threading.RLock()


def _strip_inline_comment(s: str) -> str:
    return "" if s.lstrip().startswith("#") else re.sub(r"\s+#.*$", "", s).strip()


def _plain_string(value, key, allow_empty=False):
    # These values are also interpolated by the existing shell recipes. Refuse
    # quoting, expansion, controls and YAML forms that cannot round-trip there.
    if (not isinstance(value, str) or value != value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(c in value for c in '"`$\\#{}[]') or ': ' in value
            or (value and value[0] in "'&*!|>@%,?-")
            or (not value and not allow_empty)):
        raise ValueError(f'{key}: use a plain, safe single-line string')
    if key in _LIST_KEYS and any(c.isspace() for c in value):
        raise ValueError(f'{key}: list items cannot contain whitespace')
    return value


def validate_changes(config: dict, changes: dict) -> dict:
    """Validate the supported edit subset without coercing client types."""
    if not isinstance(changes, dict) or set(changes) - _CONFIG_KEYS.keys():
        raise ValueError('changes must contain only supported config keys')
    for key, value in changes.items():
        if key == 'cpus':
            if type(value) is not int or value <= 0:
                raise ValueError('cpus must be a positive integer')
        elif key == 'memoryGiB':
            try:
                valid = type(value) in (int, float) and math.isfinite(value) and value > 0
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError('memoryGiB must be a finite positive number')
        elif key == 'permissive':
            if type(value) is not bool:
                raise ValueError('permissive must be a boolean')
        elif key in _LIST_KEYS:
            if not isinstance(value, list):
                raise ValueError(f'{key} must be a list of strings')
            for item in value:
                _plain_string(item, key)
                if key == 'egress' and (len(item) > 253 or not re.fullmatch(
                        r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
                        r'(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*', item)):
                    raise ValueError('egress: use domain names without schemes, ports or wildcards')
        else:
            _plain_string(value, key, allow_empty=True)
            if config.get(key) and not value:
                raise ValueError(f'{key}: clearing a saved identity is unsupported; unset it manually in the terminal')
    return {key: list(value) if isinstance(value, list) else value for key, value in changes.items()}


def _workspace_fields(text):
    """Read only our supported subset, retaining line positions for surgical edits.

    Unknown keys and their content are opaque. Editable keys cannot use duplicate
    definitions, quoting, anchors, flow collections (except []) or nested values.
    """
    out = {key: [] if key in _LIST_KEYS else None for key in _CONFIG_KEYS}
    out.update(name=None, permissive=False)
    reverse = {value: key for key, value in _CONFIG_KEYS.items()}
    reverse['name'] = 'name'
    fields = {}
    active = None
    closed = False
    indent = None
    parent_seen = False
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        if line.lstrip().startswith('#'):
            if line.startswith('#'):
                closed = True  # justfile awk ends list blocks at any column-zero line
            continue
        if not line[0].isspace():
            active, closed, indent = None, False, None
            match = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_-]*):(?:[ \t]*(.*))', line)
            if not match:
                if re.match(r'(?:cpus|memory|repos|plugins|egress|permissive|git_user_name|git_user_email)\s+:', line):
                    raise ValueError('editable keys require top-level key: value syntax')
                if re.match(r'''["'](?:cpus|memory|repos|plugins|egress|permissive|git_user_name|git_user_email)["']\s*:''', line) or line.startswith(('-', '{', '[', '%', '?')):
                    raise ValueError('unsupported workspace YAML shape')
                continue
            yaml_key, raw = match.groups()
            parent_seen = True
            key = reverse.get(yaml_key)
            if key is None:
                continue
            if line[len(yaml_key) + 1:] and not line[len(yaml_key) + 1].isspace():
                raise ValueError(f'{yaml_key}: a space is required after the colon')
            if key in fields:
                raise ValueError(f'duplicate editable key: {yaml_key}')
            fields[key] = {'line': index, 'items': []}
            active = key
            val = _strip_inline_comment(raw)
            if key in _LIST_KEYS:
                if val not in ('', '[]'):
                    raise ValueError(f'{key}: use an indented block list')
                closed = val == '[]'
            elif key == 'cpus':
                if not re.fullmatch(r'[0-9]+', val):
                    raise ValueError('cpus must be a positive integer')
                out[key] = int(val)
            elif key == 'memoryGiB':
                if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?(?:[Gg][Ii][Bb])?', val):
                    raise ValueError('memory must be a positive number with optional GiB suffix')
                out[key] = float(re.sub(r'[Gg][Ii][Bb]$', '', val))
            elif key == 'permissive':
                if val.lower() not in ('true', 'false'):
                    raise ValueError('permissive must be true or false')
                out[key] = val.lower() == 'true'
            elif key == 'name':
                out[key] = val
            else:
                # Unlike lists, the justfile does not strip identity comments.
                out[key] = _plain_string(raw.strip(), key, allow_empty=True)
            continue
        if not parent_seen and re.match(r'\s+(?:cpus|memory|repos|plugins|egress|permissive|git_user_name|git_user_email)\s*:', line):
            raise ValueError('editable keys must be top-level')
        if active is not None:
            item = re.fullmatch(r'( +)- +(.+)', line)
            if active not in _LIST_KEYS or closed or not item:
                raise ValueError(f'{active}: unsupported nested or multiline value')
            if indent is not None and indent != len(item[1]):
                raise ValueError(f'{active}: inconsistent list indentation')
            indent = len(item[1])
            value = _strip_inline_comment(item[2])
            _plain_string(value, active)
            out[active].append(value)
            fields[active]['items'].append(index)
    validate_changes({}, {key: value for key, value in out.items() if key in _CONFIG_KEYS and value is not None})
    return out, fields


def parse_workspace_yml(text: str) -> dict:
    """Parse the editable plain/block subset; unsupported editable syntax is an error."""
    return _workspace_fields(text)[0]


def render_workspace_edit(text: str, changes: dict) -> str:
    """Edit supported fields, inserting absent ones and preserving unrelated text."""
    config, fields = _workspace_fields(text)
    changes = validate_changes(config, changes)
    lines = text.splitlines(keepends=True)
    newline = '\r\n' if '\r\n' in text else '\n'
    replacements = {}
    additions = []
    for key, value in changes.items():
        yaml_key = _CONFIG_KEYS[key]
        field = fields.get(key)
        index = field['line'] if field else None
        original = lines[index].rstrip('\r\n') if field else ''
        comment = re.search(r'[ \t]+#.*$', original)
        suffix = comment[0] if comment else ''
        ending = ('\r\n' if lines[index].endswith('\r\n') else '\n' if lines[index].endswith('\n') else '') if field else newline
        if key in _LIST_KEYS:
            header = f'{yaml_key}:{suffix}'
            items = [f'  - {item}' for item in value]
            if field:
                positions = field['items']
                replacements[index] = header + (ending or (newline if items else ''))
                if not positions:
                    replacements[index] += ''.join(item + newline for item in items)
                for offset, pos in enumerate(positions):
                    old = lines[pos]
                    old_comment = re.search(r'[ \t]+#[^\r\n]*', old)
                    item_comment = old_comment[0] if old_comment else ''
                    end = '\r\n' if old.endswith('\r\n') else '\n' if old.endswith('\n') else ''
                    replacements[pos] = (items[offset] + item_comment + end if offset < len(items)
                                         else ('  ' + item_comment.lstrip() + end if item_comment else ''))
                if positions and len(items) > len(positions):
                    last = positions[-1]
                    if not replacements[last].endswith('\n'):
                        replacements[last] += newline
                    replacements[last] += ''.join(item + newline for item in items[len(positions):])
            else:
                additions.append(header + newline + ''.join(item + newline for item in items))
        else:
            if key == 'memoryGiB':
                rendered = format(Decimal(str(value)), 'f') + 'GiB'
            elif key == 'permissive':
                rendered = 'true' if value else 'false'
            else:
                rendered = str(value)
            replacement = f'{yaml_key}: {rendered}{suffix}{ending}'
            if field:
                replacements[index] = replacement
            else:
                additions.append(replacement)
    result = ''.join(replacements.get(index, line) for index, line in enumerate(lines))
    if additions:
        if result and not result.endswith('\n'):
            result += newline
        result += ''.join(additions)
    parsed = parse_workspace_yml(result)
    if any(parsed[key] != value for key, value in changes.items()):
        raise ValueError('config edit cannot round-trip in the supported YAML subset')
    return result


def read_config(ws_dir: str) -> tuple[dict, str, bytes]:
    raw = Path(ws_dir, 'workspace.yml').read_bytes()
    revision = hashlib.sha256(raw).hexdigest()
    return parse_workspace_yml(raw.decode('utf-8')), revision, raw


class ConfigConflict(ValueError):
    def __init__(self, config, revision):
        super().__init__('Config conflict: saved config changed.')
        self.config = config
        self.revision = revision


class IdentityConflict(ValueError):
    pass


def _require_identity(config, observed_vm):
    if not observed_vm or config.get('name') != observed_vm:
        raise IdentityConflict('Workspace VM identity changed; mutations blocked. Reopen the UI for the intended workspace.')


def validate_identity(ws_dir: str, observed_vm: str) -> None:
    with CONFIG_LOCK:
        try:
            config, _, _ = read_config(ws_dir)
        except (OSError, ValueError):
            raise IdentityConflict('Workspace VM identity unavailable; mutations blocked.') from None
        _require_identity(config, observed_vm)


def _capture_action(ws_dir, vm_name, expected_vm, expected_revision=None, require_revision=False):
    """Validate and freeze one saved observation under the same lock as Save."""
    with CONFIG_LOCK:
        if expected_vm != vm_name:
            raise IdentityConflict('Expected VM does not match the UI target; mutations blocked.')
        try:
            config, revision, _ = read_config(ws_dir)
        except (OSError, ValueError):
            raise IdentityConflict('Workspace VM identity unavailable; mutations blocked.') from None
        _require_identity(config, vm_name)
        if require_revision and (not expected_revision or expected_revision != revision):
            raise ConfigConflict(config, revision)
        return {'permissive': config['permissive'], 'egress': list(config['egress'])}


def save_config(ws_dir: str, expected_revision: str, changes: dict) -> tuple[dict, str]:
    """Process-local CAS + atomic replacement; no VM work.

    Uncooperative external editors can still write between our final byte check
    and os.replace. This is optimistic detection, not a cross-process file lock.
    """
    if not isinstance(expected_revision, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_revision):
        raise ValueError('expectedRevision must be a config SHA-256 revision')
    with CONFIG_LOCK:
        config, revision, original = read_config(ws_dir)
        validate_changes(config, changes)
        if revision != expected_revision:
            raise ConfigConflict(config, revision)
        new = render_workspace_edit(original.decode('utf-8'), changes).encode('utf-8')
        path = Path(ws_dir, 'workspace.yml')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=ws_dir, prefix='.workspace-', suffix='.tmp', delete=False) as fh:
                temporary = fh.name
                os.fchmod(fh.fileno(), stat.S_IMODE(path.stat().st_mode))
                fh.write(new)
                fh.flush()
                os.fsync(fh.fileno())
            current, current_revision, current_bytes = read_config(ws_dir)
            if current_bytes != original:
                raise ConfigConflict(current, current_revision)
            os.replace(temporary, path)
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)
        return parse_workspace_yml(new.decode('utf-8')), hashlib.sha256(new).hexdigest()


# ---------------------------------------------------------------------------
# Secrets classification — reads keys only, never returns values.
# ---------------------------------------------------------------------------

# Known example placeholders shipped in .env.secrets.example.
_PLACEHOLDERS = {"github_pat_DUMMY"}
# Secrets always shown even if absent from the file.
# Claude Code auth is NOT in .env.secrets — it uses the cached ~/.claude login.
_CORE_SECRETS = ("GITHUB_TOKEN",)


def classify_secret(key: str, value: str) -> str:
    v = (value or "").strip()
    if v == "":
        return "unset"
    if v in _PLACEHOLDERS:
        return "placeholder"
    return "set"


def _parse_env_keys(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def mcp_referenced_vars(mcp_text: str) -> set:
    return set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", mcp_text or ""))


def read_secrets(ws_dir: str) -> list:
    """Return [{key, state}] for core + MCP-referenced secrets. Values never returned."""
    env_path = os.path.join(ws_dir, ".env.secrets")
    mcp_path = os.path.join(ws_dir, ".floatie-mcp.json")
    env_vals = {}
    if os.path.isfile(env_path):
        try:
            with open(env_path, encoding="utf-8", errors="replace") as fh:
                env_vals = _parse_env_keys(fh.read())
        except OSError:
            env_vals = {}
    mcp_vars = set()
    if os.path.isfile(mcp_path):
        try:
            with open(mcp_path, encoding="utf-8", errors="replace") as fh:
                mcp_vars = mcp_referenced_vars(fh.read())
        except OSError:
            mcp_vars = set()
    keys = list(dict.fromkeys([*_CORE_SECRETS, *sorted(mcp_vars)]))
    return [{"key": k, "state": classify_secret(k, env_vals.get(k, ""))} for k in keys]


# ---------------------------------------------------------------------------
# Provider seam — LiveProvider (real reads) and FixtureProvider (canned JSON).
# build_state(provider) assembles the one state document served to the UI.
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _run(cmd, stdin=None, timeout=60):
    """Read a subprocess boundary without conflating failure with empty stdout."""
    try:
        result = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise ValueError(f'{cmd[0]} observation unavailable: {exc}') from exc
    if result.returncode:
        raise ValueError(f'{cmd[0]} observation failed ({result.returncode}): ' + result.stderr.strip())
    return result.stdout


def build_state(provider) -> dict:
    observation = provider.config_observation()
    vm = provider.vm()
    inventory = provider.inventory()
    workspace = provider.workspace()
    matches = bool(workspace.get('name')) and observation['config'].get('name') == workspace['name'] and not observation['configError']
    return {
        "workspace": workspace,
        "identity": {"matches": matches, "error": None if matches else "Workspace identity changed or unavailable; mutations blocked. Reads remain on the original VM."},
        "vm": vm,
        "firewall": provider.firewall(),
        "environments": inventory["environments"],
        "availableRepos": inventory["availableRepos"],
        "inventory": {k: v for k, v in inventory.items() if k not in ("environments", "availableRepos")},
        "secrets": provider.secrets(),
        "config": observation["config"],
        "configRevision": observation["configRevision"],
        "configError": observation["configError"],
        "generatedAt": _now_iso(),
    }


def _config_observation(ws_dir):
    try:
        config, revision, _ = read_config(ws_dir)
        return {'config': config, 'configRevision': revision, 'configError': None}
    except (OSError, ValueError):
        # Do not echo file contents, decoding snippets or arbitrary OS errors.
        return {'config': {}, 'configRevision': None,
                'configError': 'workspace.yml is unreadable or uses unsupported config syntax'}


class FixtureProvider:
    """Serves a canned state JSON file (Playwright + unit tests; no VM)."""

    def __init__(self, path: str, ws_dir=None):
        self.ws_dir = ws_dir
        self._config_state = None
        with open(path, encoding="utf-8") as fh:
            self._d = self.resolve_times(json.loads(fh.read()))

    def workspace(self): return self._d.get("workspace", {})
    def vm(self): return self._d.get("vm", {})
    def firewall(self):
        cfg = self.config()
        return {**self._d.get("firewall", {}), 'permissiveConfigured': cfg.get('permissive'),
                'egress': cfg.get('egress')}
    def environments(self): return self._d.get("environments", [])
    def secrets(self): return self._d.get("secrets", [])
    def config_observation(self):
        if self._config_state is None:
            self._config_state = (_config_observation(self.ws_dir) if self.ws_dir is not None else
                {'config': self._d.get('config', {}), 'configRevision': None,
                 'configError': 'Fixture has no backing workspace file'})
        return self._config_state

    def config(self): return self.config_observation()['config']

    @staticmethod
    def resolve_times(data):
        """Materialize explicit fixture ages for one observation; null stays unknown."""
        observed = datetime.now(timezone.utc)
        def resolve(value):
            if isinstance(value, dict):
                if set(value) == {'secondsAgo'}:
                    age = value['secondsAgo']
                    if type(age) is not int or age < 0:
                        raise ValueError('fixture secondsAgo must be a nonnegative integer')
                    return (observed - timedelta(seconds=age)).isoformat()
                return {key: resolve(child) for key, child in value.items()}
            if isinstance(value, list):
                return [resolve(child) for child in value]
            return value
        return resolve(data)

    def inventory(self):
        return {'environments': self.environments(), 'availableRepos': self._d['availableRepos'],
                **self._d['inventory']}


class LiveProvider:
    """Read one request's VM, inventory, preservation and firewall observations."""

    def __init__(self, repo_dir: str, ws_dir: str, vm_name: str):
        self.repo_dir = repo_dir
        self.ws_dir = ws_dir
        self.vm_name = vm_name
        self._vm_state = None
        self._config_state = None

    def _status(self) -> str:
        return self.vm()['status']

    def workspace(self):
        return {"name": self.vm_name, "dir": self.ws_dir}

    def config_observation(self):
        if self._config_state is None:
            self._config_state = _config_observation(self.ws_dir)
        return self._config_state

    def config(self):
        return self.config_observation()['config']

    def secrets(self):
        return read_secrets(self.ws_dir)

    def vm(self):
        if self._vm_state is not None:
            return self._vm_state
        info = {'status': 'unknown', 'observedAt': None, 'error': None}
        try:
            out = _run(['limactl', 'list', '--filter', '.name == ' + json.dumps(self.vm_name), '--json']).strip()
            if not out:
                info.update(status='absent', observedAt=_now_iso())
            else:
                rec = json.loads(out)
                if (not isinstance(rec, dict) or rec.get('status') not in
                        ('Running', 'Stopped', 'Starting', 'Stopping', 'Broken', 'Uninitialized')
                        or rec.get('name') != self.vm_name):
                    raise ValueError('Malformed or mismatched VM observation')
                mem = rec.get('memory')
                if mem is not None and (type(mem) not in (int, float) or mem < 0):
                    raise ValueError('Malformed VM memory observation')
                info.update(status=rec['status'], cpus=rec.get('cpus'),
                            memoryGiB=round(mem / (1024 ** 3), 1) if mem is not None else None,
                            diskPct=None, observedAt=_now_iso())
        except ValueError as exc:
            info['error'] = str(exc)
        if info['status'] == 'Running':
            info.update(diskObservedAt=None, diskError=None)
            try:
                df = _run(['limactl', 'shell', self.vm_name, '--', 'df', '--output=pcent', '/'])
                match = re.search(r'(\d+)%', df)
                if not match:
                    raise ValueError('Disk usage observation unavailable')
                info.update(diskPct=int(match.group(1)), diskObservedAt=_now_iso())
            except ValueError as exc:
                info['diskError'] = str(exc)
        self._vm_state = info
        return info

    def firewall(self):
        cfg = self.config()
        fw = {'mode': 'unknown', 'permissiveConfigured': cfg.get('permissive'),
              'egress': cfg.get('egress'), 'observedAt': None, 'error': None}
        if self._status() != 'Running':
            fw['error'] = 'Firewall observation unavailable until the VM is running'
            return fw
        try:
            out = _run(['limactl', 'shell', self.vm_name, '--', 'sudo', 'iptables', '-S', 'OUTPUT'])
            if not re.search(r'^-P OUTPUT (ACCEPT|DROP)\s*$', out, re.M):
                raise ValueError('Malformed or empty firewall observation')
            fw.update(mode='on' if '--match-set allowed' in out else 'off', observedAt=_now_iso())
        except ValueError as exc:
            fw['error'] = str(exc)
        return fw

    def _guard_counts(self):
        """Keep exact path keys; unknown, malformed and duplicate counts fail closed."""
        try:
            with open(os.path.join(self.repo_dir, 'lifecycle-guard.sh'), encoding='utf-8') as fh:
                script = fh.read()
            out = _run(['limactl', 'shell', self.vm_name, '--', 'bash', '-s', '--', '--json'], stdin=script)
            items = json.loads(out)
            if not isinstance(items, list):
                raise ValueError('Malformed preservation counts')
            counts = {}
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get('path'), str):
                    raise ValueError('Malformed preservation path')
                path = item['path']
                if path in counts:
                    raise ValueError('Duplicate preservation path')
                counts[path] = item
            return counts, _now_iso(), None
        except (OSError, ValueError) as exc:
            return {}, None, str(exc)

    def inventory(self):
        unavailable = dict(environments=[], availableRepos=[], inventoryKnown=False,
                           observedAt=None, error=None, countsObservedAt=None, countsError=None)
        if self._status() != 'Running':
            unavailable['error'] = 'Inventory unavailable until the VM is running: ' + (self.vm().get('error') or self._status())
            return unavailable
        try:
            with open(os.path.join(self.repo_dir, 'worker_inventory.py'), encoding='utf-8') as fh:
                script = fh.read()
            result = json.loads(_run(['limactl', 'shell', self.vm_name, '--', 'python3', '-'], stdin=script))
            if (not isinstance(result, dict) or not isinstance(result.get('environments'), list)
                    or not isinstance(result.get('availableRepos'), list)
                    or type(result.get('inventoryKnown')) is not bool
                    or 'observedAt' not in result or 'error' not in result
                    or any(not isinstance(name, str) for name in result['availableRepos'])):
                raise ValueError('Malformed worker inventory')
            for env in result['environments']:
                if (not isinstance(env, dict) or not isinstance(env.get('name'), str)
                        or env.get('kind') not in ('workspace', 'worker', 'unknown')
                        or not isinstance(env.get('session'), str)
                        or type(env.get('scopeKnown')) is not bool
                        or 'sessionLive' not in env or type(env['sessionLive']) not in (bool, type(None))
                        or not isinstance(env.get('repos'), list)):
                    raise ValueError('Malformed worker observation')
                for repo in env['repos']:
                    if (not isinstance(repo, dict) or not isinstance(repo.get('repo'), str)
                            or not isinstance(repo.get('path'), str)):
                        raise ValueError('Malformed repository observation')
        except (OSError, ValueError) as exc:
            unavailable['error'] = str(exc)
            return unavailable
        counts, observed_at, error = self._guard_counts()
        errors = [error] if error else []
        for env in result['environments']:
            for repo in env['repos']:
                count = counts.get(repo['path'])
                reason = repo.get('error') or error
                if not reason:
                    if count is None:
                        reason = 'Preservation counts missing for exact repository path'
                    elif count.get('error'):
                        reason = count['error']
                    elif (type(count.get('clean')) is not bool or any(
                            type(count.get(k)) is not int or count[k] < 0 for k in ('uncommitted', 'unpushed'))
                            or count['clean'] != (count['uncommitted'] == 0 and count['unpushed'] == 0)):
                        reason = 'Preservation counts unknown or invalid'
                if reason:
                    repo.update(clean=None, uncommitted=None, unpushed=None, error=reason, observedAt=None)
                    errors.append(str(reason))
                else:
                    repo.update({key: count[key] for key in ('clean', 'uncommitted', 'unpushed')})
                    repo['observedAt'] = observed_at
        result.update(countsObservedAt=observed_at if not errors else None,
                      countsError='; '.join(dict.fromkeys(errors)) or None)
        return result

    def environments(self):
        return self.inventory()['environments']


# ---------------------------------------------------------------------------
# Action runner — allowlisted recipe dispatch + argument validation.
# This is a SECURITY boundary: no free-form command passthrough.
# ---------------------------------------------------------------------------

_WORKER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _just(repo_dir, ws_dir, *recipe_args, vm_name, policy=None):
    args = ['just', '--justfile', os.path.join(repo_dir, 'justfile'), '-d', ws_dir,
            '--set', 'name', vm_name]
    if policy is not None:
        validate_changes({}, policy)
        args.extend(['--set', 'permissive', 'true' if policy['permissive'] else 'false',
                     '--set', 'egress', '\n'.join(policy['egress'])])
    return [*args, *recipe_args]


def action_command(repo_dir: str, ws_dir: str, key: str, params: dict, *, vm_name: str, policy=None) -> list:
    """Resolve only allowlisted recipes, passing target and captured policy literally."""
    if not isinstance(params, dict):
        raise ValueError('params must be an object')
    if key in ('start', 'stop', 'reload'):
        recipe_args = [key]
    elif key in ('firewall-on', 'firewall-off'):
        if policy is None:
            raise ValueError('saved network policy required')
        # Both legacy action keys now apply the saved snapshot. No persistent-off shortcut.
        recipe_args = ['firewall']
    elif key in ('worker-create', 'worker-rm'):
        name = params.get('name', '')
        if not isinstance(name, str) or not _WORKER_RE.fullmatch(name):
            raise ValueError('invalid worker name')
        if name in ('workspace', 'main', 'stack', 'all'):
            raise ValueError('reserved worker name')
        recipe_args = ['_worker-create' if key == 'worker-create' else 'worker-rm', name]
        if key == 'worker-create':
            repos = params.get('repos')
            if (not isinstance(repos, list) or not repos
                    or any(not isinstance(repo, str) or not _REPO_RE.fullmatch(repo)
                           or repo in ('.', '..') for repo in repos) or len(set(repos)) != len(repos)):
                raise ValueError('explicit repository scope required')
            recipe_args.extend(repos)
    else:
        raise ValueError(f'unknown action: {key}')
    return _just(repo_dir, ws_dir, *recipe_args, vm_name=vm_name, policy=policy)


def _timeout_output(exc):
    def text(value):
        return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value or ''
    return text(exc.stdout) + text(exc.stderr)


def run_action(repo_dir: str, ws_dir: str, key: str, params: dict, *, vm_name: str, policy=None) -> dict:
    cmd = action_command(repo_dir, ws_dir, key, params, vm_name=vm_name, policy=policy)
    if os.environ.get('FLOATIE_UI_FIXTURES'):
        return {'ok': True, 'fixture': True, 'outcome': 'completed', 'exitCode': 0,
                'output': '[fixture] would run: ' + ' '.join(cmd)}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return {'ok': r.returncode == 0, 'fixture': False, 'exitCode': r.returncode,
                'outcome': 'completed' if r.returncode == 0 else 'failed',
                'output': (r.stdout or '') + (r.stderr or '')}
    except subprocess.TimeoutExpired as exc:
        return {'ok': False, 'fixture': False, 'exitCode': None, 'outcome': 'unknown',
                'output': _timeout_output(exc) + '\nTimed out. Outcome unknown; inspect before any manual retry.'}
    except OSError as exc:
        return {'ok': False, 'fixture': False, 'exitCode': -1, 'outcome': 'failed', 'output': str(exc)}


# Agent transport shares the installed helper with the host/VM just recipes.
_AGENT_LIMIT = 64 * 1024
_AGENT_REMOTE = 'exec python3 "$HOME/.local/share/floatie/agent_tools.py" "$@"'


def agent_arguments(payload):
    """Validate the exact action shape before any fixture or subprocess dispatch."""
    fields = {"list": set(), "read": {"target", "pane"},
              "start": {"target", "harness"}, "send": {"target", "pane", "message"}}
    if not isinstance(payload, dict) or not isinstance(payload.get("action"), str):
        raise ValueError("expected an action object")
    action = payload["action"]
    if action not in fields or set(payload) - (fields[action] | {"action", "expectedVm"}):
        raise ValueError("invalid action or fields")
    args = [action]
    if action != "list":
        target = payload.get("target")
        if not isinstance(target, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", target) or target in ("all", "stack"):
            raise ValueError("invalid target")
        args.append(target)
    if action == "start":
        harness = payload.get("harness", "codex")
        if not isinstance(harness, str) or harness not in ("codex", "claude"):
            raise ValueError("invalid harness")
        args.append(harness)
    if "pane" in payload:
        pane = payload["pane"]
        if not isinstance(pane, str) or not re.fullmatch(r"%[0-9]{1,20}", pane):
            raise ValueError("invalid pane")
        args.extend(["--pane", pane])
    message = payload.get("message")
    if action == "send":
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be nonempty text")
        try:
            size = len(message.encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("message must be valid UTF-8") from None
        if size > _AGENT_LIMIT or any((ord(c) < 32 and c not in "\n\t") or 127 <= ord(c) < 160 for c in message):
            raise ValueError("message too large or contains control characters")
    return args, message


def run_agent(vm_name, args, message):
    try:
        result = subprocess.run(
            ["limactl", "shell", vm_name, "--", "bash", "-lc", _AGENT_REMOTE, "--", *args],
            input=message, capture_output=True, text=True, encoding="utf-8", timeout=40)
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "outcome": "unknown", "output": _timeout_output(exc), "error": "agent helper timed out; outcome unknown — inspect before any manual retry"}
    except (OSError, UnicodeError) as e:
        return {"ok": False, "error": str(e)}
    try:
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
            raise ValueError("invalid response shape")
    except (ValueError, TypeError):
        return {"ok": False, "error": (result.stderr or "agent helper returned invalid JSON")[:4096]}
    if result.returncode and data["ok"]:
        return {"ok": False, "error": "agent helper failed"}
    return data


class FixtureAgents:
    """In-memory synthetic terminal sessions, isolated to this UI server."""
    def __init__(self):
        self.lock = threading.Lock()
        self.rows = {
            "main": dict(target="main", pane="%0", harness=None, state="shell", cwd="/home/demo/work"),
            "alpha": dict(target="alpha", pane="%1", harness="claude", state="running", cwd="/home/demo/work/.worktrees/alpha"),
            "beta": dict(target="beta", pane="%2", harness=None, state="shell", cwd="/home/demo/work/.worktrees/beta"),
        }
        self.output = {name: "[fixture] Synthetic terminal output\n" for name in self.rows}
        self.output["alpha"] += '<img src=x onerror=alert(1)> is literal terminal text\n'

    def request(self, payload):
        with self.lock:
            action = payload["action"]
            if action == "list":
                return {"ok": True, "fixture": True, "agents": [dict(r) for r in self.rows.values()]}
            target = payload["target"]
            row = self.rows.get(target)
            if not row or ("pane" in payload and payload["pane"] != row["pane"]):
                return {"ok": False, "error": "target or pane not found"}
            if action == "start":
                started = row["state"] != "running"
                if started:
                    row.update(harness=payload.get("harness", "codex"), state="running")
                    self.output[target] += "[fixture] Started " + row["harness"] + "\n"
                return {"ok": True, "fixture": True, **row, "started": started}
            if action == "send":
                if row["state"] != "running":
                    return {"ok": False, "error": "no running agent"}
                output = self.output[target] + "[fixture] input submitted: " + payload["message"] + "\n"
                self.output[target] = "\n".join(output.splitlines()[-200:]).encode("utf-8")[-_AGENT_LIMIT:].decode("utf-8", errors="ignore")
                return {"ok": True, "fixture": True, "target": target, "pane": row["pane"], "status": "input submitted"}
            output = self.output[target]
            cursor = hashlib.sha256((json.dumps(row, sort_keys=True) + output).encode()).hexdigest()
            return {"ok": True, "fixture": True, **row, "output": output, "cursor": cursor,
                    "observedAt": "2026-09-25T12:00:00Z"}


# ---------------------------------------------------------------------------
# HTTP handler, server factory, and main entry point.
# ---------------------------------------------------------------------------

def _host_ok(host_header: str) -> bool:
    host = (host_header or "").rsplit(":", 1)[0].strip("[]")
    return host in {"127.0.0.1", "localhost", "::1"}


def make_handler(token: str, repo_dir: str, ws_dir: str, vm_name: str):
    """Return a request handler class bound to the given token and directories."""
    build_dir = Path(repo_dir, "ui", "dist")
    fixture_agents = FixtureAgents()

    def provider():
        fix = os.environ.get("FLOATIE_UI_FIXTURES")
        source = FixtureProvider(fix, ws_dir) if fix else LiveProvider(repo_dir, ws_dir, vm_name)
        if fix:
            source._d['workspace'] = {'name': vm_name, 'dir': ws_dir}
        return source

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence default stderr logging
            pass

        def end_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            super().end_headers()

        def _send(self, code: int, body, ctype: str = "application/json") -> None:
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _host_guard(self) -> bool:
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1 or not _host_ok(hosts[0]):
                self._send(403, json.dumps({"error": "bad host"}))
                return False
            return True

        def _guard(self) -> bool:
            """Enforce loopback Host header and per-run token. Returns False (and sends 403) on rejection."""
            if not self._host_guard():
                return False
            supplied = self.headers.get("X-Floatie-Token", "") or ""
            if not _secrets.compare_digest(supplied, token):
                self._send(403, json.dumps({"error": "bad token"}))
                return False
            return True

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if not self._host_guard():
                return
            if path.startswith('/api/') and not self._guard():
                return
            asset = static_response(build_dir, self.path)
            if asset is not None:
                self._send(asset.status, asset.body, asset.content_type)
                return
            if path == '/api/base-domains':
                try:
                    domains = _run(['bash', os.path.join(repo_dir, 'firewall.sh'), '--print-base-domains']).splitlines()
                    self._send(200, json.dumps({'domains': domains}))
                except ValueError:
                    self._send(500, json.dumps({'error': 'Base domains unavailable'}))
                return
            if path == "/api/state":
                self._send(200, json.dumps(build_state(provider())))
                return
            self._send(404, json.dumps({"error": "not found"}))

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if not self._guard():
                return
            if path == "/api/agents":
                try:
                    lengths = self.headers.get_all("Content-Length", [])
                    if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,6}", lengths[0]):
                        raise ValueError("invalid Content-Length")
                    length = int(lengths[0])
                    if not 0 < length <= _AGENT_LIMIT or self.headers.get("Transfer-Encoding"):
                        raise ValueError("request must be 1–65536 bytes with Content-Length")
                    self.connection.settimeout(5)
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError("incomplete request body")
                    payload = json.loads(raw.decode("utf-8"))
                    args, message = agent_arguments(payload)
                    if payload['action'] in ('start', 'send'):
                        _capture_action(ws_dir, vm_name, payload.get('expectedVm'))
                except IdentityConflict as exc:
                    self._send(409, json.dumps({'ok': False, 'error': str(exc)}))
                    return
                except (ValueError, OSError) as e:
                    self._send(400, json.dumps({"ok": False, "error": str(e)}))
                    return
                result = fixture_agents.request(payload) if os.environ.get("FLOATIE_UI_FIXTURES") else run_agent(vm_name, args, message)
                if payload['action'] in ('list', 'read'):
                    observation = _config_observation(ws_dir)
                    matches = observation['config'].get('name') == vm_name and not observation['configError']
                    result['identity'] = {'matches': bool(matches), 'error': None if matches else 'Workspace identity changed or unavailable; reads remain on the original VM.'}
                self._send(200, json.dumps(result))
                return
            try:
                lengths = self.headers.get_all('Content-Length', [])
                if len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,6}', lengths[0]):
                    raise ValueError('invalid Content-Length')
                length = int(lengths[0])
                if not 0 < length <= _AGENT_LIMIT or self.headers.get('Transfer-Encoding'):
                    raise ValueError('request must be 1–65536 bytes with Content-Length')
                self.connection.settimeout(5)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError('incomplete request body')
                payload = json.loads(raw.decode('utf-8'))
                if not isinstance(payload, dict):
                    raise ValueError('expected an action object')
            except (ValueError, OSError):
                self._send(400, json.dumps({'error': 'invalid request body'}))
                return
            if path == "/api/action":
                key = payload.get("action", "")
                params = payload.get("params", {}) or {}
                try:
                    policy = _capture_action(ws_dir, vm_name, payload.get('expectedVm'),
                        payload.get('expectedRevision'), key in ('firewall-on', 'firewall-off'))
                    # Validate shape before fresh availability observation or dispatch.
                    action_command(repo_dir, ws_dir, key, params, vm_name=vm_name, policy=policy)
                    if key == 'worker-create':
                        displayed = params.get('availableRepos')
                        inventory = provider().inventory()
                        available = inventory['availableRepos']
                        if (not inventory['inventoryKnown'] or not isinstance(displayed, list)
                                or any(not isinstance(repo, str) for repo in displayed)
                                or sorted(displayed) != sorted(available)
                                or not set(params['repos']) <= set(available)):
                            raise IdentityConflict('Repository availability changed or is unknown; refresh before creating.')
                    result = run_action(repo_dir, ws_dir, key, params, vm_name=vm_name, policy=policy)
                except ConfigConflict as exc:
                    self._send(409, json.dumps({'ok': False, 'error': str(exc), 'config': exc.config, 'configRevision': exc.revision}))
                    return
                except IdentityConflict as exc:
                    self._send(409, json.dumps({'ok': False, 'error': str(exc)}))
                    return
                except ValueError as e:
                    self._send(400, json.dumps({"error": str(e)}))
                    return
                self._send(200, json.dumps(result))
                return
            if path == "/api/config":
                try:
                    _capture_action(ws_dir, vm_name, payload.get('expectedVm'))
                    if not isinstance(payload, dict) or set(payload) != {'expectedVm', 'expectedRevision', 'changes'}:
                        raise ValueError('config request requires expectedRevision and changes')
                    with CONFIG_LOCK:
                        _capture_action(ws_dir, vm_name, payload.get('expectedVm'))
                        config, revision = save_config(ws_dir, payload['expectedRevision'], payload['changes'])
                except IdentityConflict as exc:
                    self._send(409, json.dumps({'ok': False, 'error': str(exc)}))
                    return
                except ConfigConflict as exc:
                    self._send(409, json.dumps({'ok': False, 'error': str(exc),
                        'config': exc.config, 'configRevision': exc.revision}))
                    return
                except (ValueError, TypeError) as exc:
                    # Decode errors may include file bytes; never return those.
                    message = 'workspace.yml is not valid UTF-8' if isinstance(exc, UnicodeError) else str(exc)
                    self._send(400, json.dumps({'ok': False, 'error': message}))
                    return
                except OSError:
                    self._send(500, json.dumps({'ok': False, 'error': 'Unable to read or atomically save workspace.yml'}))
                    return
                self._send(200, json.dumps({'ok': True, 'config': config, 'configRevision': revision}))
                return
            self._send(404, json.dumps({"error": "not found"}))

    return Handler


def make_server(host: str, port: int, token: str, repo_dir: str, ws_dir: str, vm_name: str) -> ThreadingHTTPServer:
    """Create and return a ThreadingHTTPServer bound to (host, port). Does NOT start serving."""
    return ThreadingHTTPServer((host, port), make_handler(token, repo_dir, ws_dir, vm_name))


def main() -> None:
    parser = argparse.ArgumentParser(prog="floatie ui")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--workspace", help=argparse.SUPPRESS)
    parser.add_argument("--instance-id", help=argparse.SUPPRESS)
    args = parser.parse_args()

    repo_dir = os.path.dirname(os.path.abspath(__file__))
    ws_dir = os.path.realpath(args.workspace or os.getcwd())
    vm_name = os.environ.get("FLOATIE_VM", "")
    if not vm_name:
        cfg: dict = {}
        try:
            with open(os.path.join(ws_dir, "workspace.yml"), encoding="utf-8") as fh:
                cfg = parse_workspace_yml(fh.read())
        except OSError:
            pass
        vm_name = cfg.get("name", "")

    token = _secrets.token_urlsafe(24)
    httpd = make_server("127.0.0.1", args.port, token, repo_dir, ws_dir, vm_name)
    url = f"http://127.0.0.1:{args.port}/?t={token}"
    # flush=True so the URL appears immediately even when stdout is a pipe/log
    # (Python only line-buffers to a TTY); otherwise the link can sit unseen.
    print(f"==> floatie ui: open {url}", flush=True)
    print("    (bound to 127.0.0.1 only; Ctrl-C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n==> floatie ui: stopped")
        httpd.shutdown()


if __name__ == "__main__":
    main()
