import { useSyncExternalStore } from 'react';
import type { AppSnapshot, Controller } from './controller.js';

export function useController(controller: Controller): AppSnapshot {
  return useSyncExternalStore(controller.subscribe, controller.getSnapshot);
}
