import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { Dispatch, ReactNode, SetStateAction } from 'react';
import type { Filter, Route } from './api.js';
import type { Controller } from './controller.js';
import { current, identityMatches } from './model.js';
import { Settings } from './settings/Settings.js';
import { adoptObservation, createDrafts, isDirty } from './settings/drafts.js';
import type { DraftState } from './settings/drafts.js';
import { Workspace } from './workspace/Workspace.js';
import { useController } from './useController.js';
import { Feedback } from './components/Feedback.js';
import { followRoute, SettingsNav, settingsSections } from './components/SettingsNav.js';

type Theme = 'system' | 'light' | 'dark';

// Views consume this in-memory store; navigation and observations never recreate it.
// Raw field/list input is separate from validated changes so incomplete input survives.
export interface WorkspaceViewState {
  search: string;
  filter: Filter;
  expanded: ReadonlySet<string>;
  vmDetails: boolean;
}
export interface ShellState {
  workspace: WorkspaceViewState;
  setWorkspace: Dispatch<SetStateAction<WorkspaceViewState>>;
  drafts: DraftState | null;
  setDrafts: Dispatch<SetStateAction<DraftState | null>>;
}
const ShellContext = createContext<ShellState | null>(null);
export function useShellState(): ShellState {
  const state = useContext(ShellContext);
  if (!state) throw new Error('A view must be inside the application shell.');
  return state;
}

function readRoute(): Route {
  const fragment = window.location.hash.slice(1);
  if (fragment === 'workspace') return fragment;
  const section = settingsSections.find(item => fragment === `settings/${item.id}`);
  return section ? `settings/${section.id}` : 'workspace';
}
function normalizeRoute(): Route {
  const route = readRoute();
  if (window.location.hash !== `#${route}`) {
    const url = new URL(window.location.href);
    url.hash = route;
    // Preserve the complete launcher query and replace only the invalid fragment.
    window.history.replaceState(window.history.state, '', url);
  }
  return route;
}
function readTheme(): Theme {
  try {
    const theme = localStorage.getItem('floatie-theme');
    if (theme === 'light' || theme === 'dark') return theme;
  } catch { /* The in-memory preference remains usable when storage is unavailable. */ }
  return 'system';
}

export function Buoy({ large = false }: { large?: boolean }) {
  return <svg className={large ? 'buoy buoy-large' : 'buoy'} viewBox="0 0 32 32" aria-hidden="true" focusable="false">
    <circle cx="16" cy="16" r="11" fill="none" stroke="currentColor" strokeWidth="7" />
    <path d="M5 24 12 18 M20 14 27 8" fill="none" stroke="var(--buoy-gap, var(--surface))" strokeWidth="7" />
  </svg>;
}

export function App({ controller, children }: { controller: Controller; children?: ReactNode }) {
  const snapshot = useController(controller);
  const [route, setRoute] = useState<Route>(readRoute);
  const [theme, setTheme] = useState<Theme>(readTheme);
  const [workspace, setWorkspace] = useState<WorkspaceViewState>({
    search: '', filter: 'all', expanded: new Set(), vmDetails: false,
  });
  const [drafts, setDrafts] = useState<ShellState['drafts']>(null);
  const [refreshMessage, setRefreshMessage] = useState('');
  const heading = useRef<HTMLHeadingElement>(null);
  const focusNavigation = useRef(false);
  const routeRef = useRef(route);
  const refreshRequest = useRef(0);

  useEffect(() => {
    const state = snapshot.state;
    if (identityMatches(snapshot) && state?.config && state.configRevision) {
      const { config, configRevision } = state;
      setDrafts(previous => previous ? adoptObservation(previous, config, configRevision) : createDrafts(config, configRevision));
    }
    // Only a newly accepted config read may supersede a mutation response.
  }, [snapshot.configReadSequence]);

  useEffect(() => {
    if (!drafts || !Object.values(drafts).some(isDirty)) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [drafts]);

  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const apply = () => {
      document.documentElement.dataset.theme = theme === 'system' ? (media.matches ? 'dark' : 'light') : theme;
    };
    apply();
    media.addEventListener('change', apply);
    return () => media.removeEventListener('change', apply);
  }, [theme]);

  useEffect(() => {
    const syncRoute = () => {
      const next = normalizeRoute();
      if (routeRef.current === next) return;
      focusNavigation.current = true;
      routeRef.current = next;
      setRoute(next);
    };
    normalizeRoute();
    window.addEventListener('hashchange', syncRoute);
    window.addEventListener('popstate', syncRoute);
    return () => {
      window.removeEventListener('hashchange', syncRoute);
      window.removeEventListener('popstate', syncRoute);
    };
  }, []);

  useEffect(() => {
    const syncVisibility = () => controller.setContext(routeRef.current, !document.hidden);
    syncVisibility();
    controller.start();
    document.getElementById('bootstrap-failure')?.remove();
    document.addEventListener('visibilitychange', syncVisibility);
    return () => {
      refreshRequest.current++;
      document.removeEventListener('visibilitychange', syncVisibility);
      controller.dispose();
    };
  }, [controller]);

  useEffect(() => {
    controller.setContext(route, !document.hidden);
  }, [controller, route]);

  useLayoutEffect(() => {
    if (focusNavigation.current) heading.current?.focus();
    focusNavigation.current = false;
  }, [route]);

  const navigate = (next: Route, focus = true) => {
    if (next === routeRef.current) return;
    focusNavigation.current = focus;
    routeRef.current = next;
    const url = new URL(window.location.href);
    url.hash = next;
    window.history.pushState(null, '', url);
    setRoute(next);
  };
  const changeTheme = (value: string) => {
    if (value !== 'system' && value !== 'light' && value !== 'dark') return;
    setTheme(value);
    try {
      if (value === 'system') localStorage.removeItem('floatie-theme');
      else localStorage.setItem('floatie-theme', value);
    } catch { /* Theme changes still apply for this page. */ }
  };
  const refresh = async () => {
    const request = ++refreshRequest.current;
    setRefreshMessage('Refreshing…');
    await controller.refresh('explicit');
    if (request !== refreshRequest.current) return;
    const observed = controller.getSnapshot();
    setRefreshMessage(observed.stateError || (observed.route === 'workspace' && observed.agentError)
      || (observed.route === 'settings/network' && observed.baseDomainsError)
      ? 'Refresh incomplete. Inspect the observation details.' : 'Refreshed.');
  };
  const section = settingsSections.find(item => route === `settings/${item.id}`);
  const state = snapshot.state;
  const vmKnown = !!state && !snapshot.stateError && current(state.vm, snapshot.now);
  const connected = !!state && !snapshot.stateError;
  const busy = ['waiting', 'submitting', 'observing'].includes(snapshot.operation.phase);
  const vmSummary = vmKnown ? `VM ${state.vm.status.toLowerCase()}` : 'VM status unknown';

  return <ShellContext.Provider value={{ workspace, setWorkspace, drafts, setDrafts }}>
    <a className="skip-link" href="#main-content" onClick={event => {
      event.preventDefault();
      document.getElementById('main-content')?.focus();
    }}>Skip to content</a>
    <header className="masthead">
      <div className="brand"><Buoy /><span>floatie</span></div>
      <span className="workspace-identity" data-testid="ws-name">{state?.workspace.name ?? 'Workspace unavailable'}</span>
      <nav className="primary-nav" aria-label="Primary">
        <a href="#workspace" aria-current={!section ? 'page' : undefined}
          onClick={event => followRoute(event, 'workspace', navigate)}>Workspace</a>
        <a href="#settings/resources" aria-current={section ? 'page' : undefined}
          onClick={event => followRoute(event, 'settings/resources', navigate)}>Settings</a>
      </nav>
      <div className="header-tools">
        <span className="connection"><span className="status-dot" aria-hidden="true" />
          {snapshot.stateError ? 'Connection unavailable' : connected ? 'Connected' : 'Connecting…'}</span>
        <button type="button" onClick={() => void refresh()} disabled={snapshot.refreshing || busy}>Refresh</button>
        <div className="theme-control"><label htmlFor="theme">Theme</label><select id="theme" value={theme} onChange={event => changeTheme(event.target.value)}>
          <option value="system">System</option><option value="light">Light</option><option value="dark">Dark</option>
        </select></div>
      </div>
    </header>
    <main id="main-content" tabIndex={-1}>
      <div className="page-heading">
        <h1 ref={section ? undefined : heading} tabIndex={-1}>{section ? 'Settings' : 'Workspace'}</h1>
        {!section && <div className="vm-summary"><span>{vmSummary}</span>
          {vmKnown && <span>{state.vm.cpus ?? 'Unknown'} CPUs / {state.vm.memoryGiB ?? 'Unknown'} GiB allocated</span>}
          <button type="button" aria-label="VM details" aria-expanded={workspace.vmDetails} aria-controls="vm-details"
            onClick={() => setWorkspace(previous => ({ ...previous, vmDetails: !previous.vmDetails }))}>Details</button>
        </div>}
      </div>
      <div className="refresh-status" role="status" aria-label="Refresh status" aria-live="polite">{refreshMessage}</div>
      {snapshot.stateError && <Feedback scope="Workspace observation" message="Unavailable. Refresh to check again." output={snapshot.stateError} tone="error" />}
      {state && (!state.identity.matches || state.identity.error) && <Feedback scope="Workspace identity" message="Changed or unavailable. Reopen the UI for the intended workspace." output={state.identity.error} tone="error" />}
      {snapshot.route === 'workspace' && snapshot.agentError && <Feedback scope="Agent observation" message="Unavailable. Retained observations may be stale." output={snapshot.agentError} />}
      {children ?? <>
      {section && <div className="settings-layout">
        <SettingsNav route={route} onNavigate={navigate} />
        <section className="settings-panel" aria-labelledby="section-heading">
          <h2 id="section-heading" ref={heading} tabIndex={-1}>{section.label}</h2>
          <p>{section.purpose}</p>
          <Settings key={section.id} section={section.id} snapshot={snapshot} controller={controller} drafts={drafts} setDrafts={setDrafts} />
        </section>
      </div>}
      <div hidden={!!section}><Workspace active={!section} snapshot={snapshot} controller={controller} view={workspace} onViewChange={setWorkspace} /></div>
      </>}

    </main>
  </ShellContext.Provider>;
}
