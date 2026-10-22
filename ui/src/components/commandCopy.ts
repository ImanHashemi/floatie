export interface CopyState { value: string | null; disabledReason: string | null }
export interface CopyResult { value: string; failed: boolean }

// The callback reads current eligibility at invocation and after the OS request.
// A null current state means its owning component has unmounted.
export function createCopyHandler(readCurrent: () => CopyState | null,
  writeText: (value: string) => Promise<void>, onResult: (result: CopyResult) => void) {
  return async () => {
    const captured = readCurrent();
    if (!captured?.value || captured.disabledReason) return;
    let failed = false;
    try { await writeText(captured.value); } catch { failed = true; }
    const current = readCurrent();
    if (!current || current.value !== captured.value || current.disabledReason) return;
    onResult({ value: captured.value, failed });
  };
}
