const { test } = require('node:test');
const assert = require('node:assert/strict');
const MeasurementSources = require('../sim2030/presentation/static/measurement-sources.js');

const sample = (acquisitionId, values, units = { ct_current: 'A' }) => ({
  acquisition_id: acquisitionId, samples: values, units, sample_time_us: 1000000,
});

test('量测按来源分组解析，保留每一套采集单元的读数', () => {
  const payload = {
    measurement_id: 'mc',
    sources: { mu: sample('mu', { ct_current: 115.5 }), mu_2: sample('mu_2', { ct_current: 115.5 }) },
  };
  const sources = MeasurementSources.extract(payload);
  assert.deepEqual(Object.keys(sources), ['mu', 'mu_2']);
  assert.equal(sources.mu_2.samples.ct_current, 115.5);
});

test('兼容旧的单值 {sample:{...}} 载荷，并按 acquisition_id 标注来源', () => {
  const sources = MeasurementSources.extract({ measurement_id: 'mc', sample: sample('mu', { ct_current: 115.5 }) });
  assert.deepEqual(Object.keys(sources), ['mu']);
  assert.equal(sources.mu.samples.ct_current, 115.5);
});

test('保护事件载荷（sample + source）也能标出来源', () => {
  const sources = MeasurementSources.extract({ action: 'trip', source: 'mu_2', sample: sample('mu_2', { ct_current: 315.5 }) });
  assert.deepEqual(Object.keys(sources), ['mu_2']);
});

test('无 samples 的载荷不产生来源', () => {
  assert.deepEqual(MeasurementSources.extract(null), {});
  assert.deepEqual(MeasurementSources.extract({ peer_id: 'peer_station', grid_state: 'normal' }), {});
  assert.deepEqual(MeasurementSources.extract({ sources: {} }), {});
});

test('两套读数一致时不提示不一致', () => {
  const sources = { mu: sample('mu', { ct_current: 115.47 }), mu_2: sample('mu_2', { ct_current: 115.47 }) };
  const result = MeasurementSources.compare(sources);
  assert.equal(result.inconsistent, false);
  assert.equal(result.divergent.size, 0);
});

test('同一通道偏差超出容差时提示不一致，并只标出该通道', () => {
  const sources = {
    mu: sample('mu', { ct_current: 115.47, vt_voltage: 10.0 }),
    mu_2: sample('mu_2', { ct_current: 315.47, vt_voltage: 10.0 }),
  };
  const result = MeasurementSources.compare(sources);
  assert.equal(result.inconsistent, true);
  assert.deepEqual([...result.divergent], ['ct_current']);
});

test('容差按相对值判定，较小偏差不提示', () => {
  const sources = { mu: sample('mu', { ct_current: 100 }), mu_2: sample('mu_2', { ct_current: 100.5 }) };
  assert.equal(MeasurementSources.compare(sources).inconsistent, false);
  assert.equal(MeasurementSources.compare(sources, 0.001).inconsistent, true);
});

test('单一来源或非数值通道不参与比较', () => {
  assert.equal(MeasurementSources.compare({ mu: sample('mu', { ct_current: 115 }) }).inconsistent, false);
  const sources = {
    mu: sample('mu', { ct_current: 115 }),
    mu_2: sample('mu_2', {}),
  };
  assert.equal(MeasurementSources.compare(sources).inconsistent, false);
});

test('只有部分来源给出的通道不比较，避免误报', () => {
  const sources = {
    mu: sample('mu', { ct_current: 115, vt_voltage: 10 }),
    mu_2: sample('mu_2', { ct_current: 115 }),
  };
  const result = MeasurementSources.compare(sources);
  assert.equal(result.inconsistent, false);
  assert.equal(result.divergent.size, 0);
});
