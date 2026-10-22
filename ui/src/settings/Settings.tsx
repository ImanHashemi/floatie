import type { Dispatch, SetStateAction } from 'react';
import type { EditableSection, Section } from '../api.js';
import type { AppSnapshot, Controller } from '../controller.js';
import { configObservedAfter } from '../controller.js';
import { identityMatches, inventoryCurrent, vmCurrent } from '../model.js';
import { Feedback } from '../components/Feedback.js';
import { settingsSections } from '../components/SettingsNav.js';
import { addPendingEntry, adoptObservation, discardDraft, editDraft, isDirty, recordSaveResult, removeListEntry, sectionChanges,
  setPendingText, useReviewedBaseline } from './drafts.js';
import type { ConfigObservation, DraftState } from './drafts.js';
import { Credentials } from './Credentials.js';
import { Network, operationMessage } from './Network.js';
import { DraftList, Field, SectionForm, TerminalCommand } from './SectionForm.js';

export function Settings({ section, snapshot, controller, drafts, setDrafts }: {
  section: Section; snapshot: AppSnapshot; controller: Controller; drafts: DraftState | null;
  setDrafts: Dispatch<SetStateAction<DraftState | null>>;
}) {
  const update = (fn: (previous: DraftState) => DraftState) => setDrafts(previous => previous && fn(previous));
  const busy = ['waiting', 'submitting', 'observing'].includes(snapshot.operation.phase);
  const locked = busy || snapshot.refreshing;
  // Refresh/identity failure prevents dispatch without disabling local draft editing.
  const savedAvailable = identityMatches(snapshot) && !!snapshot.state?.configRevision;
  if (section === 'credentials') return <Credentials snapshot={snapshot} />;
  if (!drafts) return <Feedback scope="Settings observation" message={snapshot.state?.configError || snapshot.stateError || 'Waiting for saved configuration.'} />;
  const draft = drafts[section];
  const label = settingsSections.find(item => item.id === section)!.label;
  const saved = draft.conflict?.config ?? draft.baseline;
  const allocationKnown = savedAvailable && vmCurrent(snapshot, snapshot.now);
  const repositoriesKnown = savedAvailable && inventoryCurrent(snapshot, snapshot.now);
  const message = (text: string, unknown = false, latest?: ConfigObservation) => update(previous => {
    const next = latest ? adoptObservation(previous, latest.config, latest.revision) : previous;
    return { ...next, [section]: { ...next[section], feedback: { kind: unknown ? 'unknown' : 'failed', message: text } } };
  });
  const canSave = savedAvailable && !locked && !snapshot.blockedUnknownKeys.includes('config:save');
  const save = async () => {
    if (!canSave) return;
    let changes;
    try { changes = sectionChanges(drafts, section); }
    catch (error) { message((draft.conflict ? 'Configuration conflict. ' : '') + (error as Error).message); return; }
    if (!Object.keys(changes).length) { message('No changed values to save.'); return; }
    const result = await controller.perform({ kind: 'save', expectedVm: snapshot.boundVm!, section,
      expectedRevision: draft.revision, changes });
    const response = result.outcome === 'blocked' ? null : result.response;
    const latest = response?.config && response.configRevision ? { config: response.config, revision: response.configRevision } : undefined;
    const observed = configObservedAfter(controller.getSnapshot(), result);
    update(previous => {
      const next = recordSaveResult(previous, section, result.outcome === 'completed' && latest ?
        { outcome: 'saved', latest } : { outcome: result.outcome === 'unknown' ? 'unknown' : 'failed',
          message: operationMessage(result), latest });
      return observed ? adoptObservation(next, observed.config, observed.revision) : next;
    });
  };
  const discard = () => {
    if (!locked && savedAvailable) {
      update(previous => {
        const current = previous[section];
        return discardDraft(previous, section, current.conflict ?? { config: current.baseline, revision: current.revision });
      });
    }
  };
  const add = (owner: Exclude<EditableSection, 'resources'>) => {
    try {
      // Each list's section/key pairing is fixed at the call sites below.
      const next = owner === 'repositories' ? addPendingEntry(drafts, owner, 'repos') :
        owner === 'agents' ? addPendingEntry(drafts, owner, 'plugins') : addPendingEntry(drafts, owner, 'egress');
      if (!locked) setDrafts(next);
    } catch (error) { message((error as Error).message); }
  };
  const list = (owner: 'repositories' | 'agents' | 'network') => {
    if (owner === 'repositories') return <DraftList id="repos-input" label="Repositories" inputLabel="Add repository"
      entries={drafts.repositories.draft.repos} pending={drafts.repositories.pending.repos} locked={locked}
      onPending={value => update(p => setPendingText(p, owner, 'repos', value))} onAdd={() => add(owner)}
      onRemove={id => update(p => removeListEntry(p, owner, 'repos', id))} />;
    if (owner === 'agents') return <DraftList id="plugins-input" label="Claude plugins" inputLabel="Add Claude plugin"
      entries={drafts.agents.draft.plugins} pending={drafts.agents.pending.plugins} locked={locked}
      onPending={value => update(p => setPendingText(p, owner, 'plugins', value))} onAdd={() => add(owner)}
      onRemove={id => update(p => removeListEntry(p, owner, 'plugins', id))} />;
    return <DraftList id="egress-input" label="Extra domains" inputLabel="Add domain"
      entries={drafts.network.draft.egress} pending={drafts.network.pending.egress} locked={locked}
      onPending={value => update(p => setPendingText(p, owner, 'egress', value))} onAdd={() => add(owner)}
      onRemove={id => update(p => removeListEntry(p, owner, 'egress', id))} />;
  };
  return <>
    {snapshot.state?.configError && <Feedback scope="Saved configuration" message={snapshot.state.configError} />}
    <SectionForm label={label} locked={locked} canSave={canSave} canDiscard={savedAvailable && !locked}
      onSave={() => void save()} onDiscard={discard}
      status={draft.feedback?.message || (draft.conflict ? 'Configuration conflict. Review the latest saved values before saving.' : '')}>
      {section === 'resources' && <div className="resource-fields">
        <Field id="cfg-cpus" label="CPUs" value={drafts.resources.draft.cpus} locked={locked}
          hint="Positive whole number. An empty value means no value was configured."
          onChange={value => update(p => editDraft(p, 'resources', 'cpus', value))} />
        <Field id="cfg-memoryGiB" label="Memory (GiB)" value={drafts.resources.draft.memoryGiB} locked={locked}
          hint="Positive number; fractional GiB values are supported."
          onChange={value => update(p => editDraft(p, 'resources', 'memoryGiB', value))} />
      </div>}
      {section === 'resources' && <section aria-label="Observed allocation">
        <h3>Observed allocation</h3>
        <p>{allocationKnown ? '' : 'Allocation unknown. Last known: '}{snapshot.state?.vm.cpus ?? 'Unknown'} CPUs / {snapshot.state?.vm.memoryGiB ?? 'Unknown'} GiB</p>
        <p>Saved resource changes do not resize the VM. {snapshot.state?.vm.error}</p>
      </section>}
      {section === 'repositories' && list(section)}
      {section === 'repositories' && <section aria-label="Observed repository availability">
        <h3>Observed repository availability</h3>
        {repositoriesKnown ? <ul>{saved.repos.map((url, index) => {
          const name = url.replace(/\/$/, '').split('/').pop()!.replace(/\.git$/, '');
          return <li key={`${index}:${url}`}>{name} — {snapshot.state!.availableRepos.includes(name) ? 'Clone name observed' : 'Not observed'}</li>;
        })}</ul> : <p>Availability unknown. Refresh repository observations. {snapshot.state?.inventory.repositoryError || snapshot.state?.inventory.error}</p>}
        <p>Clone names do not verify remote URLs. Retargeted repositories require manual reconciliation.</p>
      </section>}
      {section === 'agents' && <>
        <p>These plugins are for Claude only. Codex plugins are configured separately.</p>{list(section)}
        <Field id="cfg-gitUserName" label="Git author name" value={drafts.agents.draft.gitUserName} locked={locked}
          onChange={value => update(p => editDraft(p, 'agents', 'gitUserName', value))} />
        <Field id="cfg-gitUserEmail" label="Git author email" value={drafts.agents.draft.gitUserEmail} locked={locked}
          onChange={value => update(p => editDraft(p, 'agents', 'gitUserEmail', value))} />
        <p>Clearing a saved Git author requires a manual unset in the host configuration and VM Git configuration.</p>
      </>}
      {section === 'network' && <>
        <div role="group" aria-label="Draft network mode" className="settings-buttons">
          {[false, true].map(permissive => <button key={String(permissive)} type="button" disabled={locked}
            aria-pressed={drafts.network.draft.permissive === permissive}
            onClick={() => { if (!locked) update(p => editDraft(p, 'network', 'permissive', permissive)); }}>
            {permissive ? 'Unrestricted' : 'Restricted'}</button>)}
        </div>{list(section)}
        <p>Use domain names without schemes, ports or wildcards.</p>
      </>}
      {draft.conflict && <section className="settings-conflict" aria-label="Configuration conflict">
        <h3>Configuration conflict</h3>
        <p>Your draft still uses its original baseline. Review the latest saved values and your edits before adopting the new baseline.</p>
        <table><thead><tr><th>Field</th><th>Latest saved</th><th>Your draft</th></tr></thead>
          <tbody>{Object.entries(draft.draft).map(([key, value]) => <tr key={key}><th>{key}</th>
            <td>{display(draft.conflict!.config[key as keyof typeof draft.conflict.config])}</td><td>{display(value)}</td></tr>)}</tbody></table>
        <button type="button" disabled={!savedAvailable || locked} onClick={() => {
          if (savedAvailable && !locked) update(p => useReviewedBaseline(p, section));
        }}>Use reviewed baseline</button>
        <p>Keeps only your local edits; untouched fields use the latest saved values. Discard replaces the entire section with saved values.</p>
      </section>}
      {snapshot.blockedUnknownKeys.includes('config:save') && <Feedback scope="Save recovery"
        message="Outcome unknown. Explicitly Refresh saved configuration before retrying any section." />}
      {isDirty(draft) && <p role="status" aria-label="Unsaved changes">Unsaved changes</p>}
    </SectionForm>
    {section === 'network' ? <Network snapshot={snapshot} controller={controller} onResult={message}
      observation={drafts.network.conflict ?? { config: drafts.network.baseline, revision: drafts.network.revision }} /> :
      <section className="settings-guidance" aria-label="Apply saved changes">
        <h3>Apply saved changes</h3>
        <section aria-label={`Saved ${label}`}>
          <h3>Saved values</h3>
          {!savedAvailable && <p>Last known saved values. Refresh before applying.</p>}
          {section === 'resources' && <p>{saved.cpus ?? 'Not configured'} CPUs / {saved.memoryGiB ?? 'Not configured'} GiB</p>}
          {section === 'repositories' && <p>{display(saved.repos)}</p>}
          {section === 'agents' && <dl><dt>Claude plugins</dt><dd>{display(saved.plugins)}</dd>
            <dt>Git author name</dt><dd>{display(saved.gitUserName)}</dd>
            <dt>Git author email</dt><dd>{display(saved.gitUserEmail)}</dd></dl>}
        </section>
        <p>Commands read configuration when executed; copied commands do not pin these values. Unsaved draft edits are excluded.</p>
        {section === 'resources' && <>
          <p>Requires VM rebuild to take effect. floatie rebuild recreates VM data, authentication and signing identity. Back up work first and plan to authenticate again.</p>
          <TerminalCommand snapshot={snapshot} recipe="rebuild" />
        </>}
        {section === 'repositories' && <>
          <p>Run floatie create to reconcile provisioning and clone missing repositories. It also configures identity, copies available secrets and context, and applies the firewall.</p>
          <p>Removing or retargeting entries changes the desired list only. Existing clones and workers require manual reconciliation; they are not deleted or repointed.</p>
          <TerminalCommand snapshot={snapshot} recipe="create" />
        </>}
        {section === 'agents' && <>
          <p>For Claude plugin additions, reload attempts installation; authentication failures may warn. Completion does not prove installation. Removing a plugin here does not uninstall it; remove it manually in Claude.</p>
          <TerminalCommand snapshot={snapshot} recipe="reload" />
          <p>Nonempty Git author edits apply through create; repository-local overrides may take precedence.</p>
          <TerminalCommand snapshot={snapshot} recipe="create" />
          <p>For agent setup, run create before codex-setup, then log in inside the VM. Rebuild does not reinstall Codex. Saved configuration does not prove installation or login.</p>
          <TerminalCommand snapshot={snapshot} recipe="codex-setup" />
          <TerminalCommand snapshot={snapshot} recipe="attach" />
        </>}
      </section>}
  </>;
}

function display(value: unknown): string {
  if (value === null || value === '') return 'Not configured';
  if (Array.isArray(value)) return value.map(item => typeof item === 'object' ? item.value : item).join(', ') || 'None';
  return String(value);
}
