import type { MouseEvent } from 'react';
import type { Route, Section } from '../api.js';

export const settingsSections: readonly { id: Section; label: string; purpose: string }[] = [
  { id: 'resources', label: 'Resources', purpose: 'CPU and memory allocation for the workspace VM.' },
  { id: 'repositories', label: 'Repositories', purpose: 'Repositories configured for this workspace.' },
  { id: 'agents', label: 'Agents & Git', purpose: 'Agent setup, Claude plugins and Git author configuration.' },
  { id: 'network', label: 'Network', purpose: 'Saved network policy and observed firewall state.' },
  { id: 'credentials', label: 'Credentials', purpose: 'Credential presence and setup guidance. Values stay outside the browser.' },
];

export type Navigate = (route: Route, focus?: boolean) => void;

// Preserve native links (including modified clicks) while focusing only this navigation.
export function followRoute(event: MouseEvent<HTMLAnchorElement>, route: Route, onNavigate: Navigate) {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  onNavigate(route);
}

export function SettingsNav({ route, onNavigate }: { route: Route; onNavigate: Navigate }) {
  return <>
    <nav className="settings-nav" aria-label="Settings sections">
      <ul>{settingsSections.map(section => {
        const target: Route = `settings/${section.id}`;
        return <li key={section.id}><a href={`#${target}`} aria-current={route === target ? 'page' : undefined}
          onClick={event => followRoute(event, target, onNavigate)}>{section.label}</a></li>;
      })}</ul>
    </nav>
    <div className="settings-selector"><label htmlFor="settings-section">Settings section</label>
      <select id="settings-section" value={route.slice('settings/'.length)} onChange={event => {
        const section = settingsSections.find(item => item.id === event.target.value);
        if (section) onNavigate(`settings/${section.id}`, false);
      }}>
        {settingsSections.map(section => <option key={section.id} value={section.id}>{section.label}</option>)}
      </select>
    </div>
  </>;
}
