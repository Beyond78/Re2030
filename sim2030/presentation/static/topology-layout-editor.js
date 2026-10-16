"use strict";

class TopologyLayoutEditor {
  constructor(storage = globalThis.localStorage) {
    this.storage = storage;
    this.scenarioId = null;
    this.initial = {};
    this.positions = {};
    this.initialSizes = {};
    this.sizes = {};
    this.initialLayerSizes = {};
    this.layerSizes = {};
    this.customModules = [];
    this.customLinks = [];
    this.nodeOverrides = {};
    this.hiddenNodes = [];
    this.hiddenLinks = [];
    this.linkStyles = {};
    this.initialCustomModules = [];
    this.initialCustomLinks = [];
    this.undoStack = [];
    this.redoStack = [];
  }

  configure(scenarioId, positions, sizes = {}, layerSizes = {}, persistedLayout = null) {
    if (scenarioId === this.scenarioId) return false;
    this.scenarioId = scenarioId || "unknown";
    this.initial = this.copy(positions);
    this.initialSizes = this.copy(sizes);
    this.initialLayerSizes = this.copy(layerSizes);
    this.initialCustomModules = [];
    this.initialCustomLinks = [];
    const saved = this.load() || this.load(persistedLayout);
    this.positions = saved?.positions || this.copy(positions);
    this.sizes = saved?.sizes || this.copy(sizes);
    this.layerSizes = saved?.layerSizes || this.copy(layerSizes);
    this.customModules = saved?.customModules || [];
    this.customLinks = (saved?.customLinks || []).map(link => this.normalizeLink(link));
    this.nodeOverrides = saved?.nodeOverrides || {};
    this.hiddenNodes = saved?.hiddenNodes || [];
    this.hiddenLinks = saved?.hiddenLinks || [];
    this.linkStyles = saved?.linkStyles || {};
    this.undoStack = [];
    this.redoStack = [];
    return true;
  }

  get(id) {
    return this.positions[id] ? { ...this.positions[id] } : null;
  }

  snapshot() {
    return this.copy(this.positions);
  }

  snapshotLayout() {
    return {
      positions: this.copy(this.positions), sizes: this.copy(this.sizes), layerSizes: this.copy(this.layerSizes),
      customModules: this.copy(this.customModules), customLinks: this.copy(this.customLinks),
      nodeOverrides: this.copy(this.nodeOverrides), hiddenNodes: [...this.hiddenNodes], hiddenLinks: [...this.hiddenLinks],
      linkStyles: { ...this.linkStyles },
    };
  }

  getSize(id) {
    const size = this.sizes[id] || this.layerSizes[id];
    return size ? { ...size } : null;
  }

  move(id, position) {
    if (!this.positions[id] || !Number.isFinite(position.x) || !Number.isFinite(position.y)) return false;
    if (this.positions[id].x === position.x && this.positions[id].y === position.y) return false;
    this.undoStack.push(this.snapshotLayout());
    this.positions[id] = { x: Math.round(position.x), y: Math.round(position.y) };
    this.redoStack = [];
    return true;
  }

  commitMove(id, position, previous) {
    if (!this.positions[id] || !Number.isFinite(position.x) || !Number.isFinite(position.y)) return false;
    this.positions[id] = { x: Math.round(position.x), y: Math.round(position.y) };
    return this.commitLayout({ positions: this.copy(previous), sizes: this.copy(this.sizes) });
  }

  commitResize(id, size, previous) {
    if (!size || !Number.isFinite(size.width) || !Number.isFinite(size.height)) return false;
    if (this.positions[id]) this.sizes[id] = { width: Math.round(size.width), height: Math.round(size.height) };
    else if (this.layerSizes[id]) this.layerSizes[id] = { width: Math.round(size.width), height: Math.round(size.height) };
    else return false;
    return this.commitLayout(previous);
  }

  updateCustomModule(id, patch = {}) {
    const module = this.customModules.find(item => item.id === id);
    if (!module) return false;
    const before = this.snapshotLayout();
    if (typeof patch.name === "string" && patch.name.trim()) module.name = patch.name.trim().slice(0, 40);
    if (typeof patch.kind === "string" && patch.kind.trim()) module.kind = patch.kind.trim();
    return this.commitLayout(before);
  }

  removeCustomModule(id) {
    if (!this.customModules.some(module => module.id === id)) return false;
    const before = this.snapshotLayout();
    this.customModules = this.customModules.filter(module => module.id !== id);
    this.customLinks = this.customLinks.filter(link => link.source !== id && link.target !== id);
    delete this.positions[id];
    delete this.sizes[id];
    return this.commitLayout(before);
  }

  removeCustomLink(id) {
    if (!this.customLinks.some(link => link.id === id)) return false;
    const before = this.snapshotLayout();
    this.customLinks = this.customLinks.filter(link => link.id !== id);
    return this.commitLayout(before);
  }

  updateCustomLink(id, patch = {}) {
    const link = this.customLinks.find(item => item.id === id);
    if (!link) return false;
    const before = this.snapshotLayout();
    if (TopologyLayoutEditor.linkTypes.has(patch.type)) link.type = patch.type;
    for (const field of ["sourcePort", "targetPort"]) {
      if (patch[field] === undefined) continue;
      const value = String(patch[field] || "").trim().slice(0, 120);
      if (value) link[field] = value;
      else delete link[field];
    }
    return this.commitLayout(before);
  }

  updateNativeLink(id, patch = {}) {
    if (!id || !TopologyLayoutEditor.linkTypes.has(patch.type)) return false;
    const before = this.snapshotLayout();
    this.linkStyles[id] = patch.type;
    return this.commitLayout(before);
  }

  hideNativeNode(id) {
    if (!id || !this.initial[id] || this.hiddenNodes.includes(id)) return false;
    const before = this.snapshotLayout();
    this.hiddenNodes = [...this.hiddenNodes, id];
    return this.commitLayout(before);
  }

  hideNativeLink(id) {
    if (!id || this.hiddenLinks.includes(id)) return false;
    const before = this.snapshotLayout();
    this.hiddenLinks = [...this.hiddenLinks, id];
    return this.commitLayout(before);
  }

  normalizeLink(link) {
    const legacyType = "wired";
    const normalized = { ...link, type: TopologyLayoutEditor.linkTypes.has(link?.type) ? link.type : legacyType };
    delete normalized.color;
    delete normalized.lineStyle;
    delete normalized.width;
    delete normalized.arrow;
    return normalized;
  }

  setNodeOverride(id, name) {
    if (!id) return false;
    const before = this.snapshotLayout();
    const value = String(name || "").trim().slice(0, 40);
    if (value) this.nodeOverrides[id] = { name: value };
    else delete this.nodeOverrides[id];
    return this.commitLayout(before);
  }

  commitLayout(previous) {
    const before = {
      positions: this.copy(previous?.positions || previous),
      sizes: this.copy(previous?.sizes || this.sizes),
      layerSizes: this.copy(previous?.layerSizes || this.layerSizes),
      customModules: this.copy(previous?.customModules || this.customModules),
      customLinks: this.copy(previous?.customLinks || this.customLinks),
      nodeOverrides: this.copy(previous?.nodeOverrides || this.nodeOverrides),
      hiddenNodes: [...(previous?.hiddenNodes || this.hiddenNodes)],
      hiddenLinks: [...(previous?.hiddenLinks || this.hiddenLinks)],
      linkStyles: { ...(previous?.linkStyles || this.linkStyles) },
    };
    const current = this.snapshotLayout();
    if (JSON.stringify(before) === JSON.stringify(current)) return false;
    this.undoStack.push(before);
    this.redoStack = [];
    return true;
  }

  undo() {
    if (!this.undoStack.length) return false;
    this.redoStack.push(this.snapshotLayout());
    this.restore(this.undoStack.pop());
    return true;
  }

  redo() {
    if (!this.redoStack.length) return false;
    this.undoStack.push(this.snapshotLayout());
    this.restore(this.redoStack.pop());
    return true;
  }

  reset() {
    if (JSON.stringify(this.snapshotLayout()) === JSON.stringify({ positions: this.initial, sizes: this.initialSizes, layerSizes: this.initialLayerSizes, customModules: [], customLinks: [], nodeOverrides: {}, hiddenNodes: [], hiddenLinks: [], linkStyles: {} })) return false;
    this.undoStack.push(this.snapshotLayout());
    this.positions = this.copy(this.initial);
    this.sizes = this.copy(this.initialSizes);
    this.layerSizes = this.copy(this.initialLayerSizes);
    this.customModules = [];
    this.customLinks = [];
    this.nodeOverrides = {};
    this.hiddenNodes = [];
    this.hiddenLinks = [];
    this.linkStyles = {};
    this.redoStack = [];
    try { this.storage?.removeItem(this.storageKey()); } catch (_) { /* storage can be unavailable */ }
    return true;
  }

  save() {
    try {
      this.storage?.setItem(this.storageKey(), JSON.stringify(this.snapshotLayout()));
      return true;
    } catch (_) {
      return false;
    }
  }

  get canUndo() { return this.undoStack.length > 0; }
  get canRedo() { return this.redoStack.length > 0; }

  storageKey() { return `sim2030.topology-layout.${this.scenarioId}`; }

  load(source = undefined) {
    try {
      const parsed = source === undefined
        ? JSON.parse(this.storage?.getItem(this.storageKey()) || "null")
        : source;
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
      const storedPositions = parsed.positions && typeof parsed.positions === "object" ? parsed.positions : parsed;
      const storedSizes = parsed.sizes && typeof parsed.sizes === "object" ? parsed.sizes : {};
      const storedLayerSizes = parsed.layerSizes && typeof parsed.layerSizes === "object" ? parsed.layerSizes : {};
      const customModules = Array.isArray(parsed.customModules) ? parsed.customModules.filter(module => module && typeof module.id === "string") : [];
      const customLinks = Array.isArray(parsed.customLinks) ? parsed.customLinks
        .filter(link => link && typeof link.id === "string" && typeof link.source === "string" && typeof link.target === "string")
        .map(link => this.normalizeLink(link)) : [];
      const nodeOverrides = parsed.nodeOverrides && typeof parsed.nodeOverrides === "object" ? Object.fromEntries(
        Object.entries(parsed.nodeOverrides).filter(([id, value]) => typeof id === "string" && value && typeof value.name === "string" && value.name.trim())
          .map(([id, value]) => [id, { name: value.name.trim().slice(0, 40) }])
      ) : {};
      const hiddenNodes = Array.isArray(parsed.hiddenNodes) ? parsed.hiddenNodes.filter(id => typeof id === "string") : [];
      const hiddenLinks = Array.isArray(parsed.hiddenLinks) ? parsed.hiddenLinks.filter(id => typeof id === "string") : [];
      const linkStyles = parsed.linkStyles && typeof parsed.linkStyles === "object" ? Object.fromEntries(
        Object.entries(parsed.linkStyles).filter(([id, type]) => typeof id === "string" && TopologyLayoutEditor.linkTypes.has(type))
      ) : {};
      return {
        positions: { ...Object.fromEntries(Object.entries(this.initial)
          .map(([id, position]) => [id, storedPositions[id] && Number.isFinite(storedPositions[id].x) && Number.isFinite(storedPositions[id].y)
            ? { x: storedPositions[id].x, y: storedPositions[id].y } : position])),
          ...Object.fromEntries(customModules.map(module => [module.id, storedPositions[module.id] && Number.isFinite(storedPositions[module.id].x) && Number.isFinite(storedPositions[module.id].y)
            ? { x: storedPositions[module.id].x, y: storedPositions[module.id].y } : { x: Number(module.x) || 100, y: Number(module.y) || 100 }])) },
        sizes: { ...Object.fromEntries(Object.entries(this.initialSizes)
          .map(([id, size]) => [id, storedSizes[id] && Number.isFinite(storedSizes[id].width) && Number.isFinite(storedSizes[id].height)
            ? { width: storedSizes[id].width, height: storedSizes[id].height } : size])),
          ...Object.fromEntries(customModules.map(module => [module.id, storedSizes[module.id] && Number.isFinite(storedSizes[module.id].width) && Number.isFinite(storedSizes[module.id].height)
            ? { width: storedSizes[module.id].width, height: storedSizes[module.id].height } : { width: Number(module.width) || 110, height: Number(module.height) || 58 }])) },
        layerSizes: Object.fromEntries(Object.entries(this.initialLayerSizes)
          .map(([id, size]) => [id, storedLayerSizes[id] && Number.isFinite(storedLayerSizes[id].width) && Number.isFinite(storedLayerSizes[id].height)
            ? { width: storedLayerSizes[id].width, height: storedLayerSizes[id].height } : size])),
        customModules: this.copy(customModules),
        customLinks: this.copy(customLinks),
        nodeOverrides: this.copy(nodeOverrides),
        hiddenNodes: [...new Set(hiddenNodes)], hiddenLinks: [...new Set(hiddenLinks)], linkStyles,
      };
    } catch (_) {
      return null;
    }
  }

  restore(layout) {
    this.positions = this.copy(layout?.positions);
    this.sizes = this.copy(layout?.sizes);
    this.layerSizes = this.copy(layout?.layerSizes);
    this.customModules = this.copy(layout?.customModules);
    this.customLinks = this.copy(layout?.customLinks);
    this.nodeOverrides = this.copy(layout?.nodeOverrides);
    this.hiddenNodes = [...(layout?.hiddenNodes || [])];
    this.hiddenLinks = [...(layout?.hiddenLinks || [])];
    this.linkStyles = { ...(layout?.linkStyles || {}) };
  }

  copy(value) {
    if (Array.isArray(value)) return value.map(item => (item && typeof item === "object" ? { ...item } : item));
    return Object.fromEntries(Object.entries(value || {}).map(([id, value]) => [id, { ...value }]));
  }
}

TopologyLayoutEditor.linkTypes = new Set([
  "wired", "station", "process", "mirror", "management", "measurement", "timing",
  "wireless", "interference", "attack-access", "hardwire", "electrical", "physical",
]);

if (typeof module !== "undefined") module.exports = TopologyLayoutEditor;
