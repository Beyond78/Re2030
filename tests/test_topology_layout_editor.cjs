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

test('restores a published layout when browser storage has no override', () => {
  const editor = new TopologyLayoutEditor(memoryStorage());
  editor.configure('published', { a: { x: 1, y: 2 } }, { a: { width: 90, height: 48 } }, {}, {
    positions: { a: { x: 24, y: 36 } },
    sizes: { a: { width: 140, height: 72 } },
    layerSizes: {},
    customModules: [{ id: 'copy-a', kind: 'sensor', name: '已发布副本', x: 120, y: 80, width: 110, height: 58 }],
    customLinks: [{ id: 'link-a', source: 'copy-a', target: 'a', type: 'measurement' }],
    nodeOverrides: { a: { name: '已发布节点' } },
  });
  assert.deepEqual(editor.get('a'), { x: 24, y: 36 });
  assert.deepEqual(editor.getSize('a'), { width: 140, height: 72 });
  assert.equal(editor.customModules[0].name, '已发布副本');
  assert.equal(editor.customLinks[0].type, 'measurement');
  assert.equal(editor.nodeOverrides.a.name, '已发布节点');
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

test('persists custom modules and links through undo, redo, and storage', () => {
  const storage = memoryStorage();
  const editor = new TopologyLayoutEditor(storage);
  editor.configure('alpha', { a: { x: 1, y: 2 } }, { a: { width: 90, height: 48 } });
  const before = editor.snapshotLayout();
  editor.customModules.push({ id: 'custom-a', kind: 'sensor', name: '传感器', x: 120, y: 80, width: 110, height: 58 });
  editor.positions['custom-a'] = { x: 120, y: 80 };
  editor.sizes['custom-a'] = { width: 110, height: 58 };
  editor.customLinks.push({ id: 'custom-link-a', source: 'custom-a', target: 'a' });
  assert.equal(editor.commitLayout(before), true);
  assert.equal(editor.undo(), true);
  assert.deepEqual(editor.customModules, []);
  assert.deepEqual(editor.customLinks, []);
  assert.equal(editor.redo(), true);
  assert.equal(editor.customModules[0].id, 'custom-a');
  assert.deepEqual(editor.customLinks, [{ id: 'custom-link-a', source: 'custom-a', target: 'a' }]);
  assert.equal(editor.save(), true);
  const restored = new TopologyLayoutEditor(storage);
  restored.configure('alpha', { a: { x: 1, y: 2 } }, { a: { width: 90, height: 48 } });
  assert.equal(restored.customModules[0].name, '传感器');
  assert.deepEqual(restored.customLinks[0], {
    id: 'custom-link-a', source: 'custom-a', target: 'a',
    type: 'wired',
  });
});

test('edits custom link relation type and keeps it undoable and persistent', () => {
  const storage = memoryStorage();
  const editor = new TopologyLayoutEditor(storage);
  editor.configure('alpha', { a: { x: 1, y: 2 }, b: { x: 3, y: 4 } });
  editor.customLinks.push({ id: 'custom-link-a', source: 'a', target: 'b', type: 'wired' });
  editor.commitLayout(editor.snapshotLayout());
  assert.equal(editor.updateCustomLink('custom-link-a', { type: 'hardwire' }), true);
  assert.deepEqual(editor.customLinks[0], {
    id: 'custom-link-a', source: 'a', target: 'b',
    type: 'hardwire',
  });
  assert.equal(editor.undo(), true);
  assert.equal(editor.customLinks[0].type, 'wired');
  assert.equal(editor.redo(), true);
  assert.equal(editor.save(), true);
  const restored = new TopologyLayoutEditor(storage);
  restored.configure('alpha', { a: { x: 1, y: 2 }, b: { x: 3, y: 4 } });
  assert.equal(restored.customLinks[0].type, 'hardwire');
});

test('edits and deletes custom topology items with cascading links', () => {
  const editor = new TopologyLayoutEditor(memoryStorage());
  editor.configure('alpha', { a: { x: 1, y: 2 } });
  editor.customModules.push({ id: 'custom-a', kind: 'sensor', name: '旧名称', x: 120, y: 80, width: 110, height: 58 });
  editor.positions['custom-a'] = { x: 120, y: 80 };
  editor.sizes['custom-a'] = { width: 110, height: 58 };
  assert.equal(editor.updateCustomModule('custom-a', { name: '新名称', kind: 'ids' }), true);
  assert.deepEqual(editor.customModules[0], { id: 'custom-a', kind: 'ids', name: '新名称', x: 120, y: 80, width: 110, height: 58 });
  editor.customLinks.push({ id: 'custom-link-a', source: 'custom-a', target: 'a' });
  const beforeDelete = editor.snapshotLayout();
  assert.equal(editor.removeCustomModule('custom-a'), true);
  assert.deepEqual(editor.customModules, []);
  assert.deepEqual(editor.customLinks, []);
  assert.equal(editor.undo(), true);
  assert.deepEqual(editor.snapshotLayout(), beforeDelete);
  assert.equal(editor.redo(), true);
  assert.deepEqual(editor.customModules, []);
});

test('preserves copied node source, appearance, and renamed label through undo and storage', () => {
  const storage = memoryStorage();
  const editor = new TopologyLayoutEditor(storage);
  editor.configure('alpha', { source: { x: 10, y: 20 } });
  const before = editor.snapshotLayout();
  editor.customModules.push({
    id: 'copy-source', sourceId: 'source', sourceName: '原始攻击节点',
    appearance: 'device-attack', kind: 'network_attack_source',
    name: '攻击节点副本', x: 150, y: 20, width: 110, height: 58,
  });
  editor.positions['copy-source'] = { x: 150, y: 20 };
  editor.sizes['copy-source'] = { width: 110, height: 58 };
  editor.commitLayout(before);
  assert.equal(editor.updateCustomModule('copy-source', { name: '演示攻击源' }), true);
  assert.equal(editor.undo(), true);
  assert.equal(editor.customModules[0].name, '攻击节点副本');
  assert.equal(editor.redo(), true);
  assert.equal(editor.customModules[0].name, '演示攻击源');
  assert.equal(editor.save(), true);
  const restored = new TopologyLayoutEditor(storage);
  restored.configure('alpha', { source: { x: 10, y: 20 } });
  assert.deepEqual(restored.customModules[0], {
    id: 'copy-source', sourceId: 'source', sourceName: '原始攻击节点',
    appearance: 'device-attack', kind: 'network_attack_source',
    name: '演示攻击源', x: 150, y: 20, width: 110, height: 58,
  });
});

test('persists native node display-name overrides and supports undo', () => {
  const editor = new TopologyLayoutEditor(memoryStorage());
  editor.configure('alpha', { a: { x: 1, y: 2 } });
  assert.equal(editor.setNodeOverride('a', '保护装置'), true);
  assert.deepEqual(editor.nodeOverrides, { a: { name: '保护装置' } });
  assert.equal(editor.undo(), true);
  assert.deepEqual(editor.nodeOverrides, {});
  assert.equal(editor.redo(), true);
  assert.deepEqual(editor.nodeOverrides, { a: { name: '保护装置' } });
  assert.equal(editor.save(), true);
  const restored = new TopologyLayoutEditor(editor.storage);
  restored.configure('alpha', { a: { x: 1, y: 2 } });
  assert.deepEqual(restored.nodeOverrides, { a: { name: '保护装置' } });
});

test('edits and hides native nodes and links without changing their identities', () => {
  const storage = memoryStorage();
  const editor = new TopologyLayoutEditor(storage);
  editor.configure('alpha', { a: { x: 1, y: 2 }, b: { x: 3, y: 4 } });
  assert.equal(editor.updateNativeLink('process:mirror', { type: 'mirror' }), true);
  assert.deepEqual(editor.linkStyles, { 'process:mirror': 'mirror' });
  assert.equal(editor.hideNativeNode('a'), true);
  assert.equal(editor.hideNativeLink('process:mirror'), true);
  assert.deepEqual(editor.hiddenNodes, ['a']);
  assert.deepEqual(editor.hiddenLinks, ['process:mirror']);
  assert.equal(editor.undo(), true);
  assert.deepEqual(editor.hiddenLinks, []);
  assert.equal(editor.undo(), true);
  assert.deepEqual(editor.hiddenNodes, []);
  assert.equal(editor.redo(), true);
  assert.equal(editor.redo(), true);
  assert.equal(editor.save(), true);

  const restored = new TopologyLayoutEditor(storage);
  restored.configure('alpha', { a: { x: 1, y: 2 }, b: { x: 3, y: 4 } });
  assert.deepEqual(restored.hiddenNodes, ['a']);
  assert.deepEqual(restored.hiddenLinks, ['process:mirror']);
  assert.deepEqual(restored.linkStyles, { 'process:mirror': 'mirror' });
  assert.deepEqual(restored.snapshot(), { a: { x: 1, y: 2 }, b: { x: 3, y: 4 } });
});
