"""Deterministic synthetic Workspace inventories; no VM commands."""
import copy
import json
from pathlib import Path


def workspace_fixture(count=10):
    state = json.loads((Path(__file__).parent / 'fixtures/state_running.json').read_text())
    state['identity'] = {'matches': True, 'error': None}
    state['workspace'] = {'name': 'studio', 'dir': '/workspaces/studio'}
    root = copy.deepcopy(state['environments'][0])
    state['environments'] = [root]
    agents = [dict(target='main', state='running', pane='%0', harness='codex')]
    for i in range(count):
        name = f'worker-{i:02d}'
        repo = 'web' if i < 3 else 'api'
        absent = i % 4 == 3
        state['environments'].append(dict(name=name, kind='worker', session=name,
            sessionLive=not absent, scopeKnown=True, managementReason=None,
            repos=[dict(repo=repo, branch='worker/' + name, clean=i % 5 != 1,
                        uncommitted=1 if i % 5 == 1 else 0, unpushed=0,
                        observedAt={'secondsAgo': 0})]))
        if not absent:
            agents.append(dict(target=name, state='running', pane=f'%{i+1}', harness='codex'))
    return state, agents
