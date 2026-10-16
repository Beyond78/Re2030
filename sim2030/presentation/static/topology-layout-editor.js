"use strict";

class TopologyLayoutEditor {
  constructor(storage = globalThis.localStorage) {
    this.storage = storage;
    this.scenarioId = null;
    this.initial = {};
    this.positions = {};
    this.undoStack = [];
    this.redoStack = [];
  }

  configure(scenarioId, positions) {
    if (scenarioId === this.scenarioId) return false;
    this.scenarioId = scenarioId || "unknown";
    this.initial = this.copy(positions);
    this.positions = this.load() || this.copy(positions);
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

  move(id, position) {
    if (!this.positions[id] || !Number.isFinite(position.x) || !Number.isFinite(position.y)) return false;
    if (this.positions[id].x === position.x && this.positions[id].y === position.y) return false;
    this.undoStack.push(this.copy(this.positions));
    this.positions[id] = { x: Math.round(position.x), y: Math.round(position.y) };
    this.redoStack = [];
    return true;
  }

  commitMove(id, position, previous) {
    if (!this.positions[id] || !Number.isFinite(position.x) || !Number.isFinite(position.y)) return false;
    const before = this.copy(previous);
    if (JSON.stringify(before) === JSON.stringify(this.positions)) return false;
    this.undoStack.push(before);
    this.redoStack = [];
    this.positions[id] = { x: Math.round(position.x), y: Math.round(position.y) };
    return true;
  }

  undo() {
    if (!this.undoStack.length) return false;
    this.redoStack.push(this.copy(this.positions));
    this.positions = this.undoStack.pop();
    return true;
  }

  redo() {
    if (!this.redoStack.length) return false;
    this.undoStack.push(this.copy(this.positions));
    this.positions = this.redoStack.pop();
    return true;
  }

  reset() {
    if (JSON.stringify(this.positions) === JSON.stringify(this.initial)) return false;
    this.undoStack.push(this.copy(this.positions));
    this.positions = this.copy(this.initial);
    this.redoStack = [];
    try { this.storage?.removeItem(this.storageKey()); } catch (_) { /* storage can be unavailable */ }
    return true;
  }

  save() {
    try {
      this.storage?.setItem(this.storageKey(), JSON.stringify(this.positions));
      return true;
    } catch (_) {
      return false;
    }
  }

  get canUndo() { return this.undoStack.length > 0; }
  get canRedo() { return this.redoStack.length > 0; }

  storageKey() { return `sim2030.topology-layout.${this.scenarioId}`; }

  load() {
    try {
      const parsed = JSON.parse(this.storage?.getItem(this.storageKey()) || "null");
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
      return Object.fromEntries(Object.entries(this.initial)
        .map(([id, position]) => [id, parsed[id] && Number.isFinite(parsed[id].x) && Number.isFinite(parsed[id].y)
          ? { x: parsed[id].x, y: parsed[id].y } : position]));
    } catch (_) {
      return null;
    }
  }

  copy(value) {
    return Object.fromEntries(Object.entries(value || {}).map(([id, position]) => [id, { x: position.x, y: position.y }]));
  }
}

if (typeof module !== "undefined") module.exports = TopologyLayoutEditor;
