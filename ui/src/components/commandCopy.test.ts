import assert from 'node:assert/strict';
import test from 'node:test';
import { createCopyHandler } from './commandCopy.js';
import type { CopyResult, CopyState } from './commandCopy.js';

const command = "cd -- '/host/literal $workspace' && floatie attach 'alpha'";

test('retained_copy_callback_rechecks_eligibility_before_writing', async () => {
  for (const blocked of [null, { value: null, disabledReason: null },
    { value: command, disabledReason: 'Workspace identity changed' }]) {
    let current: CopyState | null = { value: command, disabledReason: null };
    const writes: string[] = [], results: CopyResult[] = [];
    const callback = createCopyHandler(() => current, async value => { writes.push(value); }, result => results.push(result));
    // Invoke the actual callback once while eligible, then retain it across a
    // change. There is no DOM disabled button to suppress the second call.
    await callback();
    assert.deepEqual(writes, [command]);
    assert.deepEqual(results, [{ value: command, failed: false }]);
    current = blocked;
    await callback();
    assert.deepEqual(writes, [command]);
    assert.deepEqual(results, [{ value: command, failed: false }]);
  }
});

for (const failure of [false, true]) {
  test(`copy_callback_reports_current_${failure ? 'failure' : 'success'}`, async () => {
    const writes: string[] = [], results: CopyResult[] = [];
    const callback = createCopyHandler(() => ({ value: command, disabledReason: null }), async value => {
      writes.push(value);
      if (failure) throw Error('Clipboard denied');
    }, result => results.push(result));
    await callback();
    assert.deepEqual(writes, [command]);
    assert.deepEqual(results, [{ value: command, failed: failure }]);
  });

  test(`copy_callback_ignores_late_${failure ? 'failure' : 'success'}_after_invalidation`, async () => {
    for (const invalid of [null, { value: 'different command', disabledReason: null },
      { value: command, disabledReason: 'Workspace identity changed' }]) {
      let current: CopyState | null = { value: command, disabledReason: null };
      let finish!: () => void;
      const writes: string[] = [], results: CopyResult[] = [];
      const callback = createCopyHandler(() => current, value => {
        writes.push(value);
        return new Promise<void>((resolve, reject) => { finish = () => failure ? reject(Error('Clipboard denied')) : resolve(); });
      }, result => results.push(result));
      const pending = callback();
      assert.deepEqual(writes, [command]);
      current = invalid;
      finish();
      await pending;
      assert.deepEqual(results, []);
    }
  });
}
