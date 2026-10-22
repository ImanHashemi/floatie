import { createRoot } from 'react-dom/client';
import { App } from './App.js';
import { createApi } from './api.js';
import { readToken } from './bootstrap.js';
import { createController } from './controller.js';
import './styles.css';

const token = readToken(new URL(window.location.href));

const root = document.getElementById("root");
if (!root) throw new Error("Frontend mount point unavailable. Reopen the UI.");
const reactRoot = createRoot(root);
if (token) {
  // The sole controller owns reads/timers; React owns only view state and drafts.
  const controller = createController(createApi(token, window.fetch.bind(window)), {
    now: () => Date.now(),
    setTimeout: (callback, ms) => window.setTimeout(callback, ms),
    clearTimeout: handle => window.clearTimeout(handle as number),
  });
  reactRoot.render(<App controller={controller} />);
} else {
  document.getElementById('bootstrap-failure')?.remove();
  reactRoot.render(<main><h1>Floatie</h1><p>Open this page using floatie ui.</p></main>);
}
