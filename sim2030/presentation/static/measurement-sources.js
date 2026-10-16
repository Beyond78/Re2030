/* 量测来源归属与双套数值比对（纯数据逻辑，无 DOM 依赖）。
   演示平面用它如实展示“来自哪一套采集单元”的量测值，并在两套读数偏差超出容差时提示不一致。
   这里只做展示层的数值比较，不构成攻击判定：攻击判定只在识别平面完成。 */
"use strict";

const MeasurementSources = (() => {
  /* 展示用偏差容差（相对值）：仅决定是否标出“不一致”，不是检测阈值。 */
  const DEFAULT_TOLERANCE = 0.01;

  /* 取出单份量测：需要含非空 samples，兼容 {sample:{...}} 包装。 */
  function extractSample(payload) {
    if (!payload || typeof payload !== "object") return null;
    const sample = payload.sample || payload;
    if (sample && typeof sample === "object" && sample.samples && Object.keys(sample.samples).length) {
      return sample;
    }
    return null;
  }

  /* 取出按来源分组的量测：新格式 {sources:{...}}，同时兼容旧的单值 {sample:{...}}。 */
  function extract(payload) {
    if (!payload || typeof payload !== "object") return {};
    const sources = payload.sources;
    if (sources && typeof sources === "object" && !Array.isArray(sources)) {
      const entries = Object.entries(sources).filter(([, value]) => value && typeof value === "object");
      if (entries.length) return Object.fromEntries(entries);
    }
    const single = extractSample(payload);
    if (single) return { [single.acquisition_id || "unknown"]: single };
    return {};
  }

  /* 逐通道比较各来源数值，返回 {inconsistent, divergent:Set<通道名>}。
     通道只在至少两个来源都给出有限数值时才比较。 */
  function compare(sources, tolerance = DEFAULT_TOLERANCE) {
    const result = { inconsistent: false, divergent: new Set() };
    const names = Object.keys(sources || {});
    if (names.length < 2) return result;
    const channels = new Set();
    names.forEach((name) => Object.keys(sources[name].samples || {}).forEach((channel) => channels.add(channel)));
    channels.forEach((channel) => {
      const values = names
        .map((name) => Number((sources[name].samples || {})[channel]))
        .filter((value) => Number.isFinite(value));
      if (values.length < 2) return;
      const span = Math.max(...values) - Math.min(...values);
      const limit = Math.max(1e-9, tolerance * Math.max(...values.map((value) => Math.abs(value))));
      if (span > limit) {
        result.inconsistent = true;
        result.divergent.add(channel);
      }
    });
    return result;
  }

  return { DEFAULT_TOLERANCE, extractSample, extract, compare };
})();

if (typeof module !== "undefined") module.exports = MeasurementSources;
