import type { Config, ConfigChanges, EditableSection } from '../api.js';

export type SavedConfig = {
  readonly [K in keyof Config]: Config[K] extends string[] ? readonly string[] : Config[K];
};
export interface ConfigObservation {
  readonly config: SavedConfig;
  readonly revision: string;
}
export interface ListEntry {
  readonly id: string;
  readonly value: string;
}
interface DisplayValues {
  resources: { readonly cpus: string; readonly memoryGiB: string };
  repositories: { readonly repos: readonly ListEntry[] };
  agents: { readonly plugins: readonly ListEntry[]; readonly gitUserName: string; readonly gitUserEmail: string };
  network: { readonly permissive: boolean; readonly egress: readonly ListEntry[] };
}
interface PendingValues {
  resources: Readonly<Record<never, never>>;
  repositories: { readonly repos: string };
  agents: { readonly plugins: string };
  network: { readonly egress: string };
}
const fields = {
  resources: ['cpus', 'memoryGiB'], repositories: ['repos'],
  agents: ['plugins', 'gitUserName', 'gitUserEmail'], network: ['permissive', 'egress'],
} as const;
type Field<S extends EditableSection> = typeof fields[S][number];
type ListSection = Exclude<EditableSection, 'resources'>;
type ListKey<S extends ListSection> = keyof PendingValues[S] & keyof DisplayValues[S];
type ScalarKey<S extends EditableSection> = Exclude<keyof DisplayValues[S], 'repos' | 'plugins' | 'egress'>;
export interface DraftFeedback {
  readonly kind: 'saved' | 'failed' | 'unknown';
  readonly message: string;
}
export interface SectionDraft<S extends EditableSection> {
  readonly baseline: SavedConfig;
  readonly revision: string;
  readonly draft: DisplayValues[S];
  readonly pending: PendingValues[S];
  readonly dirtyFields: readonly Field<S>[];
  readonly conflict: ConfigObservation | null;
  readonly feedback: DraftFeedback | null;
  readonly nextEntryId: number;
}
export type DraftState = { readonly [S in EditableSection]: SectionDraft<S> };
export type SaveResult =
  | { readonly outcome: 'saved'; readonly latest: ConfigObservation }
  | { readonly outcome: 'failed' | 'unknown'; readonly message: string; readonly latest?: ConfigObservation };

export function adoptObservation(drafts: DraftState, config: SavedConfig, revision: string): DraftState {
  let updated = drafts;
  for (const section of Object.keys(fields) as EditableSection[]) {
    const current = drafts[section];
    if ((current.conflict?.revision ?? current.revision) === revision) continue;
    const next = isDirty(current) || current.conflict
      ? { ...current, conflict: { config: copyConfig(config), revision } }
      : { ...newSection(section, config, revision, current), feedback: current.feedback };
    updated = { ...updated, [section]: next };
  }
  return updated;
}

export function discardDraft<S extends EditableSection>(drafts: DraftState, section: S, latest: ConfigObservation): DraftState {
  return { ...drafts, [section]: newSection(section, latest.config, latest.revision, drafts[section]) };
}

export function useReviewedBaseline<S extends EditableSection>(drafts: DraftState, section: S): DraftState {
  const current = drafts[section];
  if (!current.conflict) return drafts;
  const { config, revision } = current.conflict;
  const reviewed = newSection(section, config, revision, current);
  let draft = reviewed.draft;
  const local = current.draft as Record<string, unknown>;
  for (const key of current.dirtyFields) draft = { ...draft, [key]: local[key] };
  return {
    ...drafts,
    [section]: { ...reviewed, draft, pending: current.pending, dirtyFields: dirtyFields(section, draft, reviewed.baseline) },
  };
}

// Call only with the controller's confirmed result. Inputs remain locked for
// the reserved operation; an observation or an unknown response is not a save.
export function recordSaveResult<S extends EditableSection>(drafts: DraftState, section: S, result: SaveResult): DraftState {
  const observed = result.latest ? adoptObservation(drafts, result.latest.config, result.latest.revision) : drafts;
  const next = result.outcome === 'saved'
    ? newSection(section, result.latest.config, result.latest.revision, drafts[section])
    : observed[section];
  return {
    ...observed,
    [section]: {
      ...next,
      feedback: { kind: result.outcome, message: result.outcome === 'saved' ? 'Changes saved.' : result.message },
    },
  };
}

function copyConfig(config: SavedConfig): SavedConfig {
  return { ...config, repos: [...config.repos], plugins: [...config.plugins], egress: [...config.egress] };
}

function newSection<S extends EditableSection>(
  section: S, config: SavedConfig, revision: string, previous?: SectionDraft<S>,
): SectionDraft<S> {
  const baseline = copyConfig(config);
  let nextEntryId = previous?.nextEntryId ?? 0;
  const draft: Record<string, string | boolean | readonly ListEntry[]> = {};
  const pending: Record<string, string> = {};
  for (const key of fields[section]) {
    const value = baseline[key];
    if (Array.isArray(value)) {
      // Match duplicate occurrences in order, retaining existing keys on unchanged polls.
      const old = previous?.draft as Record<string, unknown> | undefined;
      const available = [...((old?.[key] ?? []) as readonly ListEntry[])];
      draft[key] = value.map(item => {
        const index = available.findIndex(entry => entry.value === item);
        if (index >= 0) return available.splice(index, 1)[0];
        return { id: `${section}:${key}:${nextEntryId++}`, value: item };
      });
      pending[key] = '';
    } else {
      draft[key] = typeof value === 'boolean' ? value : value === null ? '' : String(value);
    }
  }
  return {
    baseline, revision, draft: draft as DisplayValues[S], pending: pending as PendingValues[S],
    dirtyFields: [], conflict: null, feedback: null, nextEntryId,
  };
}

export function createDrafts(config: SavedConfig, revision: string): DraftState {
  return {
    resources: newSection('resources', config, revision),
    repositories: newSection('repositories', config, revision),
    agents: newSection('agents', config, revision),
    network: newSection('network', config, revision),
  };
}

export function isDirty(section: { readonly dirtyFields: readonly unknown[]; readonly pending: object }): boolean {
  return section.dirtyFields.length > 0 || Object.values(section.pending).some(value => value !== '');
}

function dirtyFields<S extends EditableSection>(section: S, draft: DisplayValues[S], baseline: SavedConfig): readonly Field<S>[] {
  const display = draft as Record<string, unknown>;
  return fields[section].filter(key => {
    const value = display[key];
    const saved = baseline[key];
    if (Array.isArray(value) && Array.isArray(saved)) {
      return value.length !== saved.length || value.some((entry: ListEntry, index) => entry.value !== saved[index]);
    }
    return value !== (typeof saved === 'boolean' ? saved : saved === null ? '' : String(saved));
  });
}

function updateDraft<S extends EditableSection>(drafts: DraftState, section: S, draft: DisplayValues[S]): DraftState {
  const current = drafts[section];
  return {
    ...drafts,
    [section]: { ...current, draft, dirtyFields: dirtyFields(section, draft, current.baseline), feedback: null },
  };
}

export function editDraft<S extends EditableSection, K extends ScalarKey<S>>(
  drafts: DraftState, section: S, key: K, value: DisplayValues[S][K],
): DraftState {
  return updateDraft(drafts, section, { ...drafts[section].draft, [key]: value });
}

export function setPendingText<S extends ListSection>(
  drafts: DraftState, section: S, key: ListKey<S>, value: string,
): DraftState {
  const current = drafts[section];
  return { ...drafts, [section]: { ...current, pending: { ...current.pending, [key]: value }, feedback: null } };
}

function listEntries<S extends ListSection>(drafts: DraftState, section: S, key: ListKey<S>): readonly ListEntry[] {
  return drafts[section].draft[key] as readonly ListEntry[];
}

export function addPendingEntry<S extends ListSection>(drafts: DraftState, section: S, key: ListKey<S>): DraftState {
  const current = drafts[section];
  const value = current.pending[key] as string;
  if (!value.trim()) throw new Error('Enter a list item before adding it.');
  const entry = { id: `${section}:${String(key)}:${current.nextEntryId}`, value };
  const updated = updateDraft(drafts, section, { ...current.draft, [key]: [...listEntries(drafts, section, key), entry] });
  return {
    ...updated,
    [section]: { ...updated[section], pending: { ...current.pending, [key]: '' }, nextEntryId: current.nextEntryId + 1 },
  };
}

export function editListEntry<S extends ListSection>(
  drafts: DraftState, section: S, key: ListKey<S>, id: string, value: string,
): DraftState {
  const entries = listEntries(drafts, section, key);
  if (!entries.some(entry => entry.id === id)) return drafts;
  return updateDraft(drafts, section, {
    ...drafts[section].draft, [key]: entries.map(entry => entry.id === id ? { ...entry, value } : entry),
  });
}

export function removeListEntry<S extends ListSection>(
  drafts: DraftState, section: S, key: ListKey<S>, id: string,
): DraftState {
  const entries = listEntries(drafts, section, key);
  if (!entries.some(entry => entry.id === id)) return drafts;
  return updateDraft(drafts, section, { ...drafts[section].draft, [key]: entries.filter(entry => entry.id !== id) });
}

function exactInteger(text: string): boolean {
  // Check decimal precision before Number can round away a fractional tail.
  const [coefficient, exponent = '0'] = text.toLowerCase().split('e');
  const decimalPlaces = coefficient.split('.')[1]?.length ?? 0;
  const trailingZeros = coefficient.replace('.', '').match(/0*$/)![0].length;
  return decimalPlaces - Number(exponent) <= trailingZeros;
}

// Basic input preflight only. The backend remains authoritative for plain
// string/domain/YAML validation; list values are sent without normalization.
export function sectionChanges(drafts: DraftState, section: EditableSection): ConfigChanges {
  const current = drafts[section];
  if (current.conflict) throw new Error('Review the saved values and use the reviewed baseline, or discard this draft, before saving.');
  if (Object.values(current.pending).some(value => value !== '')) throw new Error('Add or clear pending list text before saving.');
  const changes: ConfigChanges = {};
  const display = current.draft as Record<string, string | boolean | readonly ListEntry[]>;
  for (const key of current.dirtyFields) {
    const value = display[key];
    if (key === 'cpus' || key === 'memoryGiB') {
      // Parse decimal input deliberately; Number('') and Number('0x10') would
      // otherwise silently turn incomplete or non-decimal text into a value.
      const text = (value as string).trim();
      const number = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(text) ? Number(text) : NaN;
      if (!Number.isFinite(number) || number <= 0 || (key === 'cpus' && (!Number.isSafeInteger(number) || !exactInteger(text)))) {
        throw new Error(key === 'cpus'
          ? 'CPUs must be a positive, safely representable integer.'
          : 'Memory must be a finite positive number.');
      }
      if (number !== current.baseline[key]) changes[key] = number;
    } else if (key === 'permissive') {
      changes[key] = value as boolean;
    } else if (key === 'repos' || key === 'plugins' || key === 'egress') {
      changes[key] = (value as readonly ListEntry[]).map(entry => entry.value);
    } else {
      if (current.baseline[key] && !value) {
        throw new Error(`${key}: clearing a saved identity is unsupported; unset it manually in the terminal.`);
      }
      changes[key] = value as string;
    }
  }
  return changes;
}
