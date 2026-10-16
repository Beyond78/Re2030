const { test } = require('node:test');
const assert = require('node:assert/strict');
const TopologyLayoutEditor = require('../sim2030/presentation/static/topology-layout-editor.js');

function memoryStorage(seed = {}) {
  const values = new Map(Object.entries(seed));
  return {
    getItem(key) { return values.get(key) ?? null; },
    setItem(key, value) { values.set(key, value); },
    removeItem(key) { values.delete(key); },
  };
}

test('loads and saves layouts independently per scenario', () => {
  const storage = memoryStorage();
  const editor = new TopologyLayoutEditor(storage);
  editor.configure('alpha', { a: { x: 1, y: 2 } });
  editor.positions.a = { x: 20, y: 30 };
  assert.equal(editor.save(), true);
  editor.configure('beta', { b: { x: 4, y: 5 } });
  assert.deepEqual(editor.snapshot(), { b: { x: 4, y: 5 } });
  editor.configure('alpha', { a: { x: 1, y: 2 } });
  assert.deepEqual(editor.snapshot(), { a: { x: 20, y: 30 } });
});

test('commits one drag as a single undo and redo operation', () => {
  const editor = new TopologyLayoutEditor(memoryStorage());
  editor.configure('alpha', { a: { x: 1, y: 2 }, b: { x: 3, y: 4 } });
  const before = editor.snapshot();
  editor.positions.a = { x: 10, y: 20 };
  assert.equal(editor.commitMove('a', editor.positions.a, before), true);
  assert.equal(editor.canUndo, true);
  assert.equal(editor.undo(), true);
  assert.deepEqual(editor.snapshot(), before);
  assert.equal(editor.redo(), true);
  assert.deepEqual(editor.get('a'), { x: 10, y: 20 });
});

test('reset restores initial coordinates and removes saved overrides', () => {
  const storage = memoryStorage();
  const editor = new TopologyLayoutEditor(storage);
  editor.configure('alpha', { a: { x: 1, y: 2 } });
  editor.positions.a = { x: 9, y: 8 };
  editor.save();
  assert.equal(editor.reset(), true);
  assert.deepEqual(editor.snapshot(), { a: { x: 1, y: 2 } });
  assert.equal(storage.getItem('sim2030.topology-layout.alpha'), null);
  assert.equal(editor.undo(), true);
  assert.deepEqual(editor.get('a'), { x: 9, y: 8 });
});

test('ignores malformed and invalid stored coordinates', () => {
  const storage = memoryStorage({
    'sim2030.topology-layout.alpha': JSON.stringify({ a: { x: 12, y: 'bad' }, extra: { x: 9, y: 9 } }),
    'sim2030.topology-layout.beta': '{invalid',
  });
  const editor = new TopologyLayoutEditor(storage);
  editor.configure('alpha', { a: { x: 1, y: 2 }, b: { x: 3, y: 4 } });
  assert.deepEqual(editor.snapshot(), { a: { x: 1, y: 2 }, b: { x: 3, y: 4 } });
  editor.configure('beta', { b: { x: 5, y: 6 } });
  assert.deepEqual(editor.snapshot(), { b: { x: 5, y: 6 } });
});

test('storage failures do not break layout editing', () => {
  const storage = {
    getItem() { throw new Error('blocked'); },
    setItem() { throw new Error('blocked'); },
    removeItem() { throw new Error('blocked'); },
  };
  const editor = new TopologyLayoutEditor(storage);
  editor.configure('alpha', { a: { x: 1, y: 2 } });
  assert.equal(editor.save(), false);
  editor.positions.a = { x: 2, y: 3 };
  assert.equal(editor.reset(), true);
  assert.deepEqual(editor.get('a'), { x: 1, y: 2 });
});

test('persists sizes and treats resize as one undoable layout change', () => {
  const storage = memoryStorage();
  const editor = new TopologyLayoutEditor(storage);
  editor.configure('alpha', { a: { x: 1, y: 2 } }, { a: { width: 90, height: 48 } });
  const before = editor.snapshotLayout();
  editor.sizes.a = { width: 160, height: 72 };
  assert.equal(editor.commitResize('a', editor.sizes.a, before), true);
  assert.equal(editor.undo(), true);
  assert.deepEqual(editor.getSize('a'), { width: 90, height: 48 });
  assert.equal(editor.redo(), true);
  assert.deepEqual(editor.getSize('a'), { width: 160, height: 72 });
  assert.equal(editor.save(), true);
  editor.configure('beta', { b: { x: 3, y: 4 } }, { b: { width: 80, height: 40 } });
  editor.configure('alpha', { a: { x: 1, y: 2 } }, { a: { width: 90, height: 48 } });
  assert.deepEqual(editor.getSize('a'), { width: 160, height: 72 });
});

test('persists editable layer background sizes independently from device sizes', () => {
  const storage = memoryStorage();
  const editor = new TopologyLayoutEditor(storage);
  editor.configure('alpha', { a: { x: 1, y: 2 } }, { a: { width: 90, height: 48 } }, {
    'layer:process': { width: 600, height: 300 },
  });
  const before = editor.snapshotLayout();
  editor.layerSizes['layer:process'] = { width: 820, height: 420 };
  assert.equal(editor.commitResize('layer:process', editor.layerSizes['layer:process'], before), true);
  assert.deepEqual(editor.getSize('layer:process'), { width: 820, height: 420 });
  assert.equal(editor.save(), true);
  const restored = new TopologyLayoutEditor(storage);
  restored.configure('alpha', { a: { x: 1, y: 2 } }, { a: { width: 90, height: 48 } }, {
    'layer:process': { width: 600, height: 300 },
  });
  assert.deepEqual(restored.getSize('layer:process'), { width: 820, height: 420 });
});
