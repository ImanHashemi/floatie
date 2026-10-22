import assert from 'node:assert/strict';
import test from 'node:test';
import type { Config } from '../api.js';
import {
  createDrafts, editDraft, sectionChanges, isDirty,
  setPendingText, addPendingEntry, editListEntry, removeListEntry,
  adoptObservation, discardDraft, useReviewedBaseline, recordSaveResult,
} from './drafts.js';

function config(overrides: Partial<Config> = {}): Config {
  return {
    name: 'demo', cpus: 4, memoryGiB: 4, permissive: false,
    repos: ['github.com/example/api'], plugins: ['tool@market'], egress: ['example.com'],
    gitUserName: 'Example Author', gitUserEmail: 'author@example.com', ...overrides,
  };
}

test('sparse resources and identity remain blank and are omitted from unrelated saves', () => {
  const initial = createDrafts(config({ cpus: null, memoryGiB: null, gitUserName: null, gitUserEmail: null }), 'r1');
  assert.equal(initial.resources.draft.cpus, '');
  assert.equal(initial.resources.draft.memoryGiB, '');
  assert.equal(initial.agents.draft.gitUserName, '');
  assert.deepEqual(sectionChanges(initial, 'resources'), {});
  const edited = editDraft(initial, 'agents', 'gitUserName', 'New Author');
  assert.deepEqual(sectionChanges(edited, 'agents'), { gitUserName: 'New Author' });
  assert.equal(initial.agents.draft.gitUserName, '');
});

test('raw numeric edits stay visible but equivalent values produce no save payload', () => {
  const initial = createDrafts(config(), 'r1');
  const formatted = editDraft(initial, 'resources', 'cpus', '04');
  assert.equal(formatted.resources.draft.cpus, '04');
  assert.deepEqual(sectionChanges(formatted, 'resources'), {});
  const edited = editDraft(formatted, 'resources', 'memoryGiB', '6.25');
  assert.deepEqual(sectionChanges(edited, 'resources'), { memoryGiB: 6.25 });
  assert.deepEqual(sectionChanges(edited, 'network'), {});
});

test('unfinished list text is dirty, blocks saving, and is retained until added or cleared', () => {
  const initial = createDrafts(config(), 'r1');
  const pending = setPendingText(initial, 'agents', 'plugins', 'next@market');
  assert.equal(isDirty(pending.agents), true);
  assert.deepEqual(pending.agents.dirtyFields, []);
  assert.throws(() => sectionChanges(pending, 'agents'), /add.*clear/i);
  assert.equal(pending.agents.pending.plugins, 'next@market');
  assert.equal(isDirty(setPendingText(pending, 'agents', 'plugins', '').agents), false);
  const added = addPendingEntry(pending, 'agents', 'plugins');
  assert.equal(added.agents.pending.plugins, '');
  assert.deepEqual(sectionChanges(added, 'agents'), { plugins: ['tool@market', 'next@market'] });
  assert.equal(initial.agents.pending.plugins, '');
});

test('removing one duplicate preserves the other occurrence and later IDs are never reused', () => {
  const initial = createDrafts(config({ repos: ['same', 'same', 'last'] }), 'r1');
  const [first, second, last] = initial.repositories.draft.repos;
  assert.notEqual(first.id, second.id);
  const removed = removeListEntry(initial, 'repositories', 'repos', first.id);
  assert.deepEqual(removed.repositories.draft.repos, [second, last]);
  const added = addPendingEntry(setPendingText(removed, 'repositories', 'repos', 'same'), 'repositories', 'repos');
  assert.equal(added.repositories.draft.repos[0].id, second.id);
  assert.notEqual(added.repositories.draft.repos[2].id, first.id);
  assert.deepEqual(sectionChanges(added, 'repositories'), { repos: ['same', 'last', 'same'] });
  assert.equal(initial.repositories.draft.repos.length, 3);
});

test('editing one duplicate keeps that row identity and serializes only string values', () => {
  const initial = createDrafts(config({ plugins: ['same', 'same'] }), 'r1');
  const [first, second] = initial.agents.draft.plugins;
  const edited = editListEntry(initial, 'agents', 'plugins', second.id, 'changed');
  assert.deepEqual(edited.agents.draft.plugins, [first, { id: second.id, value: 'changed' }]);
  assert.deepEqual(sectionChanges(edited, 'agents'), { plugins: ['same', 'changed'] });
  const reverted = editListEntry(edited, 'agents', 'plugins', second.id, 'same');
  assert.equal(isDirty(reverted.agents), false);
});

test('invalid CPU text is retained and cannot become zero, a truncated integer, or a rounded integer', () => {
  for (const value of ['', ' ', '0', '-1', '1.5', 'NaN', 'Infinity', '1e309', '4x', '0x10', '9007199254740993']) {
    const edited = editDraft(createDrafts(config(), 'r1'), 'resources', 'cpus', value);
    assert.equal(edited.resources.draft.cpus, value);
    assert.throws(() => sectionChanges(edited, 'resources'), /positive.*integer/i, value);
  }
});

test('invalid memory text is retained and cannot become zero or nonfinite JSON', () => {
  for (const value of ['', ' ', '0', '-0.5', 'NaN', 'Infinity', '1e309', '6GiB', '0x10']) {
    const edited = editDraft(createDrafts(config(), 'r1'), 'resources', 'memoryGiB', value);
    assert.equal(edited.resources.draft.memoryGiB, value);
    assert.throws(() => sectionChanges(edited, 'resources'), /finite positive number/i, value);
  }
});

test('valid resource text produces positive numeric values without changing raw inputs', () => {
  const initial = createDrafts(config({ cpus: null, memoryGiB: null }), 'r1');
  for (const value of ['8', '8.0', '8e0', '0.008e3', '80e-1']) {
    const edited = editDraft(editDraft(initial, 'resources', 'cpus', value), 'resources', 'memoryGiB', '.25');
    assert.deepEqual(sectionChanges(edited, 'resources'), { cpus: 8, memoryGiB: 0.25 });
    assert.equal(edited.resources.draft.cpus, value);
    assert.equal(edited.resources.draft.memoryGiB, '.25');
  }
});

test('clearing either saved Git identity field gives manual-unset guidance and preserves the input', () => {
  for (const key of ['gitUserName', 'gitUserEmail'] as const) {
    const edited = editDraft(createDrafts(config(), 'r1'), 'agents', key, '');
    assert.throws(() => sectionChanges(edited, 'agents'), /unset.*manually.*terminal/i);
    assert.equal(edited.agents.draft[key], '');
    assert.equal(edited.agents.revision, 'r1');
  }
});

test('returning an unset identity to blank omits it while a nonempty identity is sent unchanged', () => {
  const initial = createDrafts(config({ gitUserName: null, gitUserEmail: null, plugins: [] }), 'r1');
  const typed = editDraft(initial, 'agents', 'gitUserName', 'New Author');
  const reverted = editDraft(typed, 'agents', 'gitUserName', '');
  const added = addPendingEntry(setPendingText(reverted, 'agents', 'plugins', 'new@market'), 'agents', 'plugins');
  assert.deepEqual(sectionChanges(added, 'agents'), { plugins: ['new@market'] });
  assert.deepEqual(sectionChanges(typed, 'agents'), { gitUserName: 'New Author' });
});

test('whitespace-only pending text also blocks save and cannot be added as an empty row', () => {
  const pending = setPendingText(createDrafts(config(), 'r1'), 'network', 'egress', '   ');
  assert.equal(isDirty(pending.network), true);
  assert.throws(() => sectionChanges(pending, 'network'), /add.*clear/i);
  assert.throws(() => addPendingEntry(pending, 'network', 'egress'), /enter.*item/i);
  assert.equal(pending.network.pending.egress, '   ');
});

test('clean observations adopt new values while repeated polls preserve duplicate row identities', () => {
  const initial = createDrafts(config({ repos: ['same', 'same'] }), 'r1');
  assert.equal(adoptObservation(initial, config({ repos: ['same', 'same'] }), 'r1'), initial);
  const observed = adoptObservation(initial, config({ repos: ['same', 'same'], memoryGiB: 6.25 }), 'r2');
  assert.equal(observed.resources.revision, 'r2');
  assert.equal(observed.resources.draft.memoryGiB, '6.25');
  assert.deepEqual(observed.repositories.draft.repos, initial.repositories.draft.repos);
  assert.equal(isDirty(observed.resources), false);
});

test('dirty observations retain the original baseline and raw edits until explicit review', () => {
  const edited = editDraft(createDrafts(config(), 'r1'), 'resources', 'cpus', '8');
  const observed = adoptObservation(edited, config({ cpus: 2, memoryGiB: 6.25 }), 'r2');
  assert.equal(observed.resources.revision, 'r1');
  assert.equal(observed.resources.baseline.cpus, 4);
  assert.deepEqual(observed.resources.draft, { cpus: '8', memoryGiB: '4' });
  assert.equal(observed.resources.conflict?.revision, 'r2');
  assert.equal(observed.resources.conflict?.config.cpus, 2);
  assert.throws(() => sectionChanges(observed, 'resources'), /review.*baseline/i);
  const reviewed = useReviewedBaseline(observed, 'resources');
  assert.equal(reviewed.resources.revision, 'r2');
  assert.equal(reviewed.resources.baseline.cpus, 2);
  assert.deepEqual(reviewed.resources.draft, { cpus: '8', memoryGiB: '6.25' });
  assert.deepEqual(reviewed.resources.dirtyFields, ['cpus']);
  assert.equal(reviewed.resources.conflict, null);
  assert.deepEqual(sectionChanges(reviewed, 'resources'), { cpus: 8 });
  assert.equal(observed.resources.revision, 'r1');
});

test('new conflicts replace the review candidate without silently acknowledging earlier observations', () => {
  const edited = editDraft(createDrafts(config(), 'r1'), 'resources', 'cpus', '8');
  const first = adoptObservation(edited, config({ memoryGiB: 6 }), 'r2');
  const next = adoptObservation(first, config({ memoryGiB: 7 }), 'r3');
  assert.equal(next.resources.revision, 'r1');
  assert.equal(next.resources.conflict?.revision, 'r3');
  assert.equal(useReviewedBaseline(next, 'resources').resources.draft.memoryGiB, '7');
  const reverted = editDraft(next, 'resources', 'cpus', '4');
  const polled = adoptObservation(reverted, config({ memoryGiB: 7 }), 'r3');
  assert.equal(polled.resources.revision, 'r1');
  assert.throws(() => sectionChanges(polled, 'resources'), /review.*baseline/i);
});

test('review preserves list occurrence edits and pending text but adopts untouched fields', () => {
  const initial = createDrafts(config({ plugins: ['same', 'same'] }), 'r1');
  const second = initial.agents.draft.plugins[1];
  const edited = editListEntry(initial, 'agents', 'plugins', second.id, 'local');
  const pending = setPendingText(edited, 'agents', 'plugins', 'unfinished@');
  const observed = adoptObservation(pending, config({ plugins: ['external'], gitUserName: 'Other Author' }), 'r2');
  const reviewed = useReviewedBaseline(observed, 'agents');
  assert.deepEqual(reviewed.agents.draft.plugins, edited.agents.draft.plugins);
  assert.equal(reviewed.agents.pending.plugins, 'unfinished@');
  assert.equal(reviewed.agents.draft.gitUserName, 'Other Author');
  assert.deepEqual(reviewed.agents.dirtyFields, ['plugins']);
  assert.equal(reviewed.agents.revision, 'r2');
  assert.throws(() => sectionChanges(reviewed, 'agents'), /add.*clear/i);
});

test('pending text alone holds the baseline and review adopts saved rows without losing the text', () => {
  const pending = setPendingText(createDrafts(config(), 'r1'), 'network', 'egress', 'unfinished.');
  const observed = adoptObservation(pending, config({ permissive: true, egress: ['new.example'] }), 'r2');
  assert.equal(observed.network.revision, 'r1');
  assert.equal(observed.network.draft.permissive, false);
  const reviewed = useReviewedBaseline(observed, 'network');
  assert.equal(reviewed.network.draft.permissive, true);
  assert.deepEqual(reviewed.network.draft.egress.map(entry => entry.value), ['new.example']);
  assert.equal(reviewed.network.pending.egress, 'unfinished.');
  assert.equal(isDirty(reviewed.network), true);
});

test('review recalculates dirty fields when the saved value already matches the local edit', () => {
  const edited = editDraft(createDrafts(config(), 'r1'), 'resources', 'cpus', '8');
  const reviewed = useReviewedBaseline(adoptObservation(edited, config({ cpus: 8 }), 'r2'), 'resources');
  assert.equal(isDirty(reviewed.resources), false);
  assert.deepEqual(sectionChanges(reviewed, 'resources'), {});
  assert.equal(useReviewedBaseline(edited, 'resources'), edited);
});

test('discard explicitly takes the supplied latest observation and leaves a dirty sibling untouched', () => {
  let edited = editDraft(createDrafts(config(), 'r1'), 'resources', 'cpus', '8');
  edited = setPendingText(edited, 'agents', 'plugins', 'pending@market');
  const observed = adoptObservation(edited, config({ cpus: 2 }), 'r2');
  const discarded = discardDraft(observed, 'agents', { config: config({ plugins: ['latest'] }), revision: 'r3' });
  assert.equal(discarded.agents.revision, 'r3');
  assert.equal(discarded.agents.pending.plugins, '');
  assert.equal(discarded.agents.conflict, null);
  assert.equal(isDirty(discarded.agents), false);
  assert.deepEqual(discarded.agents.draft.plugins.map(entry => entry.value), ['latest']);
  assert.equal(discarded.resources, observed.resources);
});

test('successful save advances the saved section and clean siblings but retains dirty sibling baselines', () => {
  let edited = editDraft(createDrafts(config(), 'r1'), 'resources', 'cpus', '8');
  edited = editDraft(edited, 'agents', 'gitUserName', 'Local Author');
  edited = setPendingText(edited, 'agents', 'plugins', 'pending@market');
  const saved = recordSaveResult(edited, 'resources', {
    outcome: 'saved', latest: { config: config({ cpus: 8 }), revision: 'r2' },
  });
  assert.equal(saved.resources.revision, 'r2');
  assert.equal(saved.resources.baseline.cpus, 8);
  assert.equal(isDirty(saved.resources), false);
  assert.equal(saved.resources.feedback?.kind, 'saved');
  assert.equal(saved.network.revision, 'r2');
  assert.equal(saved.agents.revision, 'r1');
  assert.equal(saved.agents.baseline.gitUserName, 'Example Author');
  assert.equal(saved.agents.draft.gitUserName, 'Local Author');
  assert.equal(saved.agents.pending.plugins, 'pending@market');
  assert.equal(saved.agents.conflict?.revision, 'r2');
});

test('failed and unknown saves retain every edit and their feedback through subsequent polls', () => {
  const initial = createDrafts(config(), 'r1');
  const edited = editListEntry(initial, 'agents', 'plugins', initial.agents.draft.plugins[0].id, 'local');
  const pending = setPendingText(edited, 'agents', 'plugins', 'pending@market');
  for (const outcome of ['failed', 'unknown'] as const) {
    const result = recordSaveResult(pending, 'agents', { outcome, message: 'Save not confirmed.' });
    assert.equal(result.agents.revision, 'r1');
    assert.deepEqual(result.agents.draft, pending.agents.draft);
    assert.deepEqual(result.agents.pending, pending.agents.pending);
    assert.equal(result.agents.feedback?.kind, outcome);
    const observed = adoptObservation(result, config({ plugins: ['local'] }), 'r2');
    assert.equal(observed.agents.revision, 'r1');
    assert.equal(observed.agents.feedback?.kind, outcome);
    assert.equal(observed.agents.pending.plugins, 'pending@market');
    assert.equal(observed.agents.conflict?.revision, 'r2');
  }
});

test('a conflict save response retains the draft and supplies an explicit review candidate', () => {
  const edited = editDraft(createDrafts(config(), 'r1'), 'resources', 'cpus', '8');
  const failed = recordSaveResult(edited, 'resources', {
    outcome: 'failed', message: 'Configuration changed.',
    latest: { config: config({ memoryGiB: 6.25 }), revision: 'r2' },
  });
  assert.equal(failed.resources.revision, 'r1');
  assert.equal(failed.resources.feedback?.kind, 'failed');
  assert.equal(failed.resources.conflict?.revision, 'r2');
  assert.equal(failed.resources.draft.cpus, '8');
  assert.deepEqual(useReviewedBaseline(failed, 'resources').resources.draft, { cpus: '8', memoryGiB: '6.25' });
});

test('a successful list save retains the occurrence IDs used by the current form', () => {
  const edited = addPendingEntry(setPendingText(createDrafts(config(), 'r1'), 'agents', 'plugins', 'tool@market'), 'agents', 'plugins');
  const saved = recordSaveResult(edited, 'agents', {
    outcome: 'saved', latest: { config: config({ plugins: ['tool@market', 'tool@market'] }), revision: 'r2' },
  });
  assert.deepEqual(saved.agents.draft.plugins, edited.agents.draft.plugins);
  assert.equal(isDirty(saved.agents), false);
  assert.deepEqual(sectionChanges(saved, 'agents'), {});
});

test('fractional CPU text that JavaScript rounds to an integer is still rejected', () => {
  for (const value of ['4.0000000000000001', '9007199254740990.9', '1.00000000000000001e1']) {
    const edited = editDraft(createDrafts(config(), 'r1'), 'resources', 'cpus', value);
    assert.throws(() => sectionChanges(edited, 'resources'), /positive.*integer/i, value);
  }
});

test('a file reverted to the original revision becomes the current review candidate', () => {
  const edited = editDraft(createDrafts(config(), 'r1'), 'resources', 'cpus', '8');
  const conflicted = adoptObservation(edited, config({ memoryGiB: 6.25 }), 'r2');
  const reverted = adoptObservation(conflicted, config(), 'r1');
  assert.equal(reverted.resources.conflict?.revision, 'r1');
  assert.equal(reverted.resources.conflict?.config.memoryGiB, 4);
  const reviewed = useReviewedBaseline(reverted, 'resources');
  assert.deepEqual(reviewed.resources.draft, { cpus: '8', memoryGiB: '4' });
  assert.equal(reviewed.resources.revision, 'r1');
});

function freeze<T>(value: T): T {
  if (value && typeof value === 'object') {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}

test('transitions accept frozen state and returned payloads cannot mutate drafts or saved observations', () => {
  const source = config();
  const initial = freeze(createDrafts(source, 'r1'));
  source.repos.push('external-mutation');
  assert.deepEqual(initial.repositories.baseline.repos, ['github.com/example/api']);
  const pending = freeze(setPendingText(initial, 'repositories', 'repos', 'github.com/example/next'));
  const added = freeze(addPendingEntry(pending, 'repositories', 'repos'));
  const payload = sectionChanges(added, 'repositories');
  payload.repos!.pop();
  assert.equal(added.repositories.draft.repos.length, 2);
  const latest = config({ repos: ['external'] });
  const observed = freeze(adoptObservation(added, latest, 'r2'));
  latest.repos.push('later-mutation');
  assert.deepEqual(observed.repositories.conflict?.config.repos, ['external']);
  const reviewed = freeze(useReviewedBaseline(observed, 'repositories'));
  assert.deepEqual(sectionChanges(reviewed, 'repositories'), { repos: ['github.com/example/api', 'github.com/example/next'] });
  const failed = freeze(recordSaveResult(reviewed, 'repositories', { outcome: 'failed', message: 'Rejected.' }));
  const discarded = discardDraft(failed, 'repositories', freeze({ config: config({ repos: [] }), revision: 'r3' }));
  assert.deepEqual(sectionChanges(discarded, 'repositories'), {});
  assert.equal(initial.repositories.revision, 'r1');
  assert.equal(pending.repositories.pending.repos, 'github.com/example/next');
});

test('network payloads contain only changed config keys and never change the saved policy', () => {
  const initial = createDrafts(config({ egress: ['first.example', 'second.example'] }), 'r1');
  const changed = editDraft(initial, 'network', 'permissive', true);
  assert.deepEqual(sectionChanges(changed, 'network'), { permissive: true });
  const removed = removeListEntry(changed, 'network', 'egress', changed.network.draft.egress[0].id);
  assert.deepEqual(sectionChanges(removed, 'network'), { permissive: true, egress: ['second.example'] });
  assert.equal(removed.network.baseline.permissive, false);
  assert.deepEqual(removed.network.baseline.egress, ['first.example', 'second.example']);
  assert.equal(removeListEntry(removed, 'network', 'egress', 'missing'), removed);
  assert.equal(editListEntry(removed, 'network', 'egress', 'missing', 'ignored'), removed);
});

// Compile-time contracts: this function is never run. Each directive must stay
// necessary, so a mutable type or an editable read-only section fails tsc.
function immutableContracts(drafts: ReturnType<typeof createDrafts>) {
  // @ts-expect-error Credentials has no editable draft.
  editDraft(drafts, 'credentials', 'gitUserName', 'author');
  // @ts-expect-error Fields cannot cross section boundaries.
  editDraft(drafts, 'resources', 'gitUserName', 'author');
  // @ts-expect-error Network mode is a boolean, never input text.
  editDraft(drafts, 'network', 'permissive', 'true');
  // @ts-expect-error Pending keys belong to their list section.
  setPendingText(drafts, 'agents', 'egress', 'example.com');
  // @ts-expect-error Numeric display text is immutable.
  drafts.resources.draft.cpus = '0';
  // @ts-expect-error Lists cannot be mutated by consumers.
  drafts.repositories.draft.repos.push({ id: 'new', value: 'new' });
  // @ts-expect-error Occurrences cannot be retargeted by consumers.
  drafts.agents.draft.plugins[0].value = 'new';
  // @ts-expect-error Saved observations are deeply readonly.
  drafts.network.baseline.egress.push('new');
  // @ts-expect-error Pending text changes go through a pure transition.
  drafts.network.pending.egress = 'new';
}
void immutableContracts;
