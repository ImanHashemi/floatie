import type { AppSnapshot } from '../controller.js';
import { TerminalCommand } from './SectionForm.js';

export function Credentials({ snapshot }: { snapshot: AppSnapshot }) {
  return <div className="settings-content">
    <p>Values are never sent to the browser or displayed.</p>
    <dl>{snapshot.state?.secrets.map(secret => <div key={secret.key} className="credential-row">
      <dt>{secret.key}</dt><dd>{snapshot.stateError ? 'unknown · last observed ' : ''}{secret.state}</dd>
    </div>)}</dl>
    {!snapshot.state?.secrets.length && <p>No credential presence information is available.</p>}
    <section className="settings-guidance" aria-label="Credential setup">
      <h3>Set up credentials on the host</h3>
      <p>Edit the host .env.secrets file. floatie create copies it to the VM when present. Running processes may need manual restart or login to use new credentials.</p>
      <TerminalCommand snapshot={snapshot} recipe="create" />
    </section>
  </div>;
}
