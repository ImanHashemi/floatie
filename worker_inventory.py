#!/usr/bin/env python3
"""Read-only worker scope and terminal inventory, streamable to the bound VM.

Git identity mirrors worker-init/worker-rm: an exact registered path, common
storage, and the per-worktree gitdir backlink. Safety counts belong exclusively
to lifecycle-guard; null here means they have not been observed.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess


def now():
    return datetime.now(timezone.utc).isoformat()


def read_command(argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=15,
                              env={**os.environ, 'LC_ALL': 'C', 'GIT_OPTIONAL_LOCKS': '0'})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f'{argv[0]} observation unavailable: {exc}') from exc


def git(path, *args):
    result = read_command(['git', '-C', str(path), *args])
    if result.returncode:
        raise ValueError('Git identity unavailable: ' + (result.stderr.strip() or 'command failed'))
    return result.stdout.rstrip('\n')


def repo_name(name):
    return bool(re.fullmatch(r'[A-Za-z0-9._-]+', name)) and name not in ('.', '..')


def inspect_repository(path):
    if (path.is_symlink() or (path / '.git').is_symlink() or not path.is_dir()
            or not ((path / '.git').is_file() or (path / '.git').is_dir())):
        raise ValueError('Not an actual repository with its own .git')
    if git(path, 'rev-parse', '--show-toplevel') != str(path):
        raise ValueError('Git top-level does not match the exact path')
    listing = git(path, 'worktree', 'list', '--porcelain', '-z')
    paths = [field[len('worktree '):] for field in listing.split('\0') if field.startswith('worktree ')]
    if paths.count(str(path)) != 1:
        raise ValueError('Missing or duplicate exact Git worktree registration')
    common = (path / git(path, 'rev-parse', '--git-common-dir')).resolve(strict=True)
    gitdir = Path(git(path, 'rev-parse', '--absolute-git-dir')).resolve(strict=True)
    if gitdir != common:
        backlink = gitdir / 'gitdir'
        if gitdir.parent != common / 'worktrees' or backlink.is_symlink() or not backlink.is_file():
            raise ValueError('Invalid per-worktree Git metadata')
        target = gitdir / backlink.read_text().rstrip('\n')
        if target.parent.resolve(strict=True) / target.name != path / '.git':
            raise ValueError('Git worktree backlink does not match the exact path')
    return {'common': common, 'gitdir': gitdir, 'paths': paths}


def branch_name(path):
    # symbolic-ref succeeds before the first commit. Exit 1 means detached HEAD;
    # other failures are errors, never an excuse to invent a branch or clean state.
    result = read_command(['git', '-C', str(path), 'symbolic-ref', '--quiet', '--short', 'HEAD'])
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    if result.returncode == 1:
        return git(path, 'rev-parse', '--abbrev-ref', 'HEAD')
    raise ValueError('Git branch unavailable: ' + (result.stderr.strip() or 'empty branch observation'))


def repo_observation(path, info=None, error=None):
    branch = None
    if info is not None and error is None:
        try:
            branch = branch_name(path)
        except ValueError as exc:
            error = str(exc)
    row = dict(repo=path.name, path=str(path), branch=branch,
               clean=None, uncommitted=None, unpushed=None)
    if error:
        row['error'] = error
    return row


def sessions(work_dir):
    """Observe all sessions; a missing tmux server is a known empty observation."""
    # Preserve literal format tabs even under read_command's LC_ALL=C. Without
    # UTF-8 client output, tmux can replace those separators with underscores.
    result = read_command(['tmux', '-u', 'list-panes', '-a', '-F',
        '#{session_name}\t#{session_path}\t#{pane_current_path}\t#{@floatie-agent}'])
    if result.returncode:
        # tmux uses exit 1 for an absent server/socket as well as real failures.
        error = result.stderr.strip()
        if result.returncode == 1 and (error.startswith('no server running on ') or
                (error.startswith('error connecting to ') and error.endswith('(No such file or directory)'))):
            return {}
        raise ValueError('tmux observation unavailable: ' + (error or 'command failed'))
    observed = {}
    for line in result.stdout.splitlines():
        fields = line.split('\t')
        if len(fields) != 4 or not fields[0]:
            raise ValueError('Malformed tmux observation')
        name, start, cwd, marker = fields
        target = work_dir / '.worktrees' / name
        recognized = bool(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', name)) and name not in ('all', 'stack', 'workspace')
        evidence = marker in ('codex', 'claude') or any(
            Path(p) == target or target in Path(p).parents for p in (start, cwd) if p)
        observed[name] = observed.get(name, False) or (recognized and evidence)
    return observed


def collect(work_dir: Path) -> dict:
    work_dir = Path(work_dir).resolve()
    errors, repo_errors = [], []
    session_error = None
    try:
        live = sessions(work_dir)
        session_time = now()
    except ValueError as exc:
        live, session_time, session_error = {}, None, str(exc)
        errors.append(session_error)

    clones, root_repos, available, common_seen = {}, [], [], set()
    try:
        children = sorted(work_dir.iterdir())
    except OSError as exc:
        children = []
        repo_errors.append(f'Workspace inventory unavailable: {exc}')
    for path in children:
        if not repo_name(path.name) or path.is_symlink() or not (path / '.git').exists():
            continue
        try:
            info = inspect_repository(path)
            clones[path.name] = info
            if info['common'] in common_seen:
                continue
            common_seen.add(info['common'])
            available.append(path.name)
            root_repos.append(repo_observation(path, info))
        except (OSError, ValueError) as exc:
            reason = f'{path.name}: {exc}'
            repo_errors.append(reason)
            root_repos.append(repo_observation(path, error=reason))

    base = work_dir / '.worktrees'
    saved = {}
    try:
        if base.is_symlink():
            raise ValueError('Symlinked worker directory; inventory unavailable')
        if base.exists():
            saved = {path.name: path for path in sorted(base.iterdir())}
    except (OSError, ValueError) as exc:
        repo_errors.append(str(exc))

    # Preserve rows for registered-but-missing paths and reject nested/partial
    # registrations just like reconnect. Do not silently shrink a saved scope.
    registrations = {}
    for info in clones.values():
        for value in info['paths']:
            path = Path(value)
            try:
                relative = path.relative_to(base)
            except ValueError:
                continue
            if relative.parts:
                registrations.setdefault(relative.parts[0], set()).add((value, info['common']))

    envs = [dict(name='workspace', kind='workspace', session='main',
        sessionLive=None if session_error else 'main' in live, repos=root_repos,
        scopeKnown=not repo_errors, managementReason='Root workspace cannot be removed')]
    for name in sorted((set(saved) | set(registrations) | set(live)) - {'main'}):
        path = base / name
        repos, reasons, seen = [], [], set()
        valid_paths = {}
        has_saved = name in saved or name in registrations
        kind = 'worker' if has_saved or live.get(name) else 'unknown'
        if has_saved:
            try:
                if base.is_symlink() or path.is_symlink() or not path.is_dir():
                    raise ValueError('Saved worker directory is missing or invalid')
                entries = sorted(path.iterdir())
                if not entries:
                    reasons.append('Empty saved scope; inspect before reconnecting')
                for repo in entries:
                    try:
                        if not repo_name(repo.name) or repo.name not in clones:
                            raise ValueError('No matching valid root clone')
                        info = inspect_repository(repo)
                        if (not (repo / '.git').is_file() or info['common'] != clones[repo.name]['common']
                                or info['gitdir'] == info['common']):
                            raise ValueError('Saved worktree does not belong to the matching root clone')
                        if info['common'] in seen:
                            raise ValueError('Saved repositories share Git storage')
                        seen.add(info['common'])
                        valid_paths[str(repo)] = info['common']
                        repos.append(repo_observation(repo, info))
                    except (OSError, ValueError) as exc:
                        reason = f'{repo.name}: {exc}'
                        reasons.append(reason)
                        repos.append(repo_observation(repo, error=reason))
                for registered, common in sorted(registrations.get(name, set())):
                    if valid_paths.get(registered) != common:
                        reasons.append(f'Missing or unexpected registered worktree: {registered}')
            except (OSError, ValueError) as exc:
                reasons.append(str(exc))
        else:
            reasons.append('Session only; repository scope unavailable' if kind == 'worker'
                           else 'Unrecognized session; inspect manually')
        scope_known = has_saved and not reasons and not repo_errors
        if repo_errors:
            reasons.append('Root clone inventory incomplete; inspect before managing')
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', name) or name in ('stack', 'workspace', 'all'):
            reasons.append('Unsupported worker name; inspect manually')
        envs.append(dict(name=name, kind=kind, session=name,
            sessionLive=None if session_error else name in live, repos=repos,
            scopeKnown=scope_known, managementReason='; '.join(reasons) or None))
    errors.extend(repo_errors)
    return dict(environments=envs, availableRepos=available, inventoryKnown=not errors,
                observedAt=now() if not errors else None, error='; '.join(errors) or None,
                repositoryObservedAt=now() if not repo_errors else None,
                repositoryError='; '.join(repo_errors) or None,
                sessionObservedAt=session_time, sessionError=session_error)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, default=Path.home() / 'work')
    parser.add_argument('--human', action='store_true')
    args = parser.parse_args(argv)
    result = collect(args.work_dir)
    if not args.human:
        print(json.dumps(result))
        return 0
    if result['error']:
        print('Inventory partially unavailable: ' + result['error'])
    print(f'{"WORKER":<18} {"TERMINAL":<10} {"REPOSITORY":<24} BRANCH / SCOPE')
    for row in result['environments']:
        if row['kind'] == 'workspace':
            continue
        terminal = 'unknown' if row['sessionLive'] is None else 'present' if row['sessionLive'] else 'absent'
        for repo in row['repos'] or [{}]:
            # Escape control characters from untrusted session names/errors in a terminal.
            values = [row['name'], terminal, repo.get('repo', '—'), repo.get('branch') or 'unknown']
            values = [json.dumps(v, ensure_ascii=False)[1:-1] for v in values]
            print(f'{values[0]:<18} {values[1]:<10} {values[2]:<24} {values[3]}')
        if row['managementReason']:
            print('  ' + json.dumps(row['managementReason'], ensure_ascii=False)[1:-1])
    print('Root workspace: floatie attach · floatie orchestrate (excluded from worker rows)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
