/* 单请求步进调度器：倍速以原有每秒一步为基准，耗时过长时不积压补发。 */
"use strict";

class SimulationPlayer {
  constructor({ send, onChange = () => {}, onError = () => {}, onUpdate = async () => {}, clock = {} }) {
    this.send = send;
    this.onChange = onChange;
    this.onError = onError;
    this.onUpdate = onUpdate;
    this.clock = {
      setTimeout: clock.setTimeout || ((fn, ms) => setTimeout(fn, ms)),
      clearTimeout: clock.clearTimeout || (id => clearTimeout(id)),
      now: clock.now || (() => performance.now()),
    };
    this.runId = null;
    this.status = "idle";
    this.dtUs = 100000;
    this.rate = 0.125;
    this.busy = false;
    this.enabled = false;
    this.timer = null;
    this.inFlight = null;
    this.revision = 0;
    this.error = null;
  }

  get running() { return this.enabled && this.status === "running"; }
  get terminal() { return ["finished", "stopped", "error"].includes(this.status); }

  _notify() { this.onChange(this); }
  _clearTimer() {
    if (this.timer !== null) this.clock.clearTimeout(this.timer);
    this.timer = null;
  }
  _halt() {
    this.enabled = false;
    this._clearTimer();
    this.revision++;
  }

  bind(runId, data = {}) {
    if (this.inFlight || this.busy) throw new Error("请等待当前操作完成");
    this._halt();
    this.runId = runId;
    this.status = data.status || "ready";
    this.dtUs = data.dt_us || 100000;
    this.error = null;
    this._notify();
  }

  observe(data) {
    if (data.run_id !== this.runId || this.busy) return;
    if (Number.isFinite(data.dt_us) && data.dt_us > 0) this.dtUs = data.dt_us;
    this.status = data.status || this.status;
    if (this.status !== "running") this._halt();
    this._notify();
  }

  setRate(rate) {
    if (![0.125, 0.25, 0.5, 1].includes(rate)) throw new Error("不支持的推演速度");
    this.rate = rate;
    this._clearTimer();
    if (this.running && !this.inFlight && !this.busy) this._schedule(1000 / rate);
    this._notify();
  }

  async _request(action) {
    const result = await this.send(action, this.runId);
    if (result?.error || result?.status !== "accepted") {
      throw new Error(result?.error || result?.reason || "操作未被受理");
    }
    if (result.summary?.status) this.status = result.summary.status;
    else if (action === "start") this.status = "running";
    else if (action === "pause" || action === "step_once") this.status = "paused";
    else if (action === "stop") this.status = "finished";
    if (result.summary?.dt_us) this.dtUs = result.summary.dt_us;
    this.revision++;
    if (this.terminal) this._halt();
    return result;
  }

  _fail(error) {
    this._halt();
    this.error = error;
    this.onError(error);
  }

  _schedule(delay) {
    this._clearTimer();
    if (!this.running || this.busy || this.inFlight) return;
    this.timer = this.clock.setTimeout(() => {
      this.timer = null;
      return this._tick();
    }, delay);
  }

  async _tick() {
    if (!this.running || this.busy || this.inFlight) return;
    const started = this.clock.now();
    // 延迟到微任务中发送，使 inFlight 在任何回调执行前完成赋值。
    this.inFlight = Promise.resolve().then(async () => {
      try {
        await this._request("step");
        await this.onUpdate();
      } catch (error) {
        this._fail(error);
      }
    });
    await this.inFlight;
    this.inFlight = null;
    this._notify();
    if (this.running && !this.busy) {
      this._schedule(Math.max(0, 1000 / this.rate - (this.clock.now() - started)));
    }
  }

  async command(action) {
    if (!this.runId || this.busy || this.terminal) return false;
    if (action === "step_once" && this.status === "running") return false;
    if (!["start", "pause", "stop", "step_once"].includes(action)) return false;
    this._halt();
    this.busy = true;
    this.error = null;
    this._notify();
    let succeeded = false;
    try {
      // 暂停、停止、单步不会与已发出的自动步进交错执行。
      if (this.inFlight) await this.inFlight;
      if (!this.terminal) await this._request(action);
      succeeded = true;
      await this.onUpdate();
    } catch (error) {
      this._fail(error);
    } finally {
      this.busy = false;
      this.enabled = succeeded && action === "start" && this.status === "running";
      this._notify();
      if (this.running) this._schedule(0);
    }
    return succeeded;
  }
}

if (typeof module !== "undefined") module.exports = SimulationPlayer;
