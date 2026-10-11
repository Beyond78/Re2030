const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const topology = require('../sim2030/presentation/static/network-topology.js');
const scene = JSON.parse(fs.readFileSync(path.join(__dirname, '../scenarios/substation-live.json'), 'utf8'));
const coordinates = Object.fromEntries(scene.devices.map(d => [d.device_id, d.layout]));
const model = topology.build(scene, coordinates);

test('two isolated switches, two mirror feeds and independent reporting cables', () => {
  assert.equal(model.assets.filter(a => a.kind === 'switch').length, 2);
  assert.equal(model.cables.filter(c => c.kind === 'mirror').length, 2);
  assert.equal(model.cables.filter(c => c.kind === 'management').length, 2);
  assert.equal(model.cables.filter(c => c.kind === 'station').length, 3);
  assert.equal(model.cables.filter(c => c.kind === 'process').length, 6);
  assert.equal(model.routes.size, scene.links.length);
  assert.equal(model.cables.some(c => /gateway|bus/.test(c.id)), false);
  const rightEdge = Math.max(...scene.layout.layers.map(layer => layer.x + layer.width));
  const bands = Object.fromEntries(scene.layout.layers.map(layer => [layer.key, layer]));
  for (const network of model.networks) {
    assert.ok(network.switch.x > rightEdge, 'switch must sit outside all device layers');
    assert.ok(network.probe.x > network.switch.x + network.switch.width);
    const upper = bands[network.id === 'station' ? 'station' : 'bay'];
    const lower = bands[network.id === 'station' ? 'bay' : 'process'];
    for (const cable of model.cables.filter(c => c.kind === network.id)) {
      const [bend, port] = cable.points.slice(1);
      assert.equal(bend.y, port.y);
      assert.ok(bend.y > upper.y + upper.height && bend.y < lower.y,
        'horizontal access cables must stay between device layers');
    }
  }
});

test('physical measurements, timing and hardwire controls never enter either switch', () => {
  for (const id of ['L-ct-mu', 'L-vt-mu', 'L-oil-mu', 'L-time-mu', 'L-ctl-brk-down', 'L-brk-tap']) {
    assert.equal(model.routes.get(id).network_id, null, id);
  }
  assert.equal(model.cables.find(c => c.id === 'L-time-mu').kind, 'timing');
  const timing = model.routes.get('L-time-mu').points;
  assert.deepEqual(timing, ['time_svc', 'mu'].map(id => ({
    x: coordinates[id].x + 45, y: coordinates[id].y + 24,
  })), 'timing cable must connect directly without intermediate bends');
});

test('packets enter only the intended switch and reverse along exactly the same cable', () => {
  const request = { sender_id: 'station', receiver_id: 'mc', source_port: 'mc_breaker', target_port: 'up' };
  const points = topology.messageRoute(model, request);
  assert.deepEqual(points, model.routes.get('L-station-mc').points);
  const station = model.networks.find(n => n.id === 'station');
  assert.ok(points.some(p => p.x === station.switch.x + station.switch.width / 2));
  assert.ok(points.every(p => p.y < 410 && p.x < station.probe.x));
  const reverse = topology.messageRoute(model, { sender_id: 'mc', receiver_id: 'station', source_port: 'up', target_port: 'mc_breaker' });
  assert.deepEqual(reverse, [...points].reverse());
  assert.equal(topology.messageRoute(model, { ...request, source_port: 'invalid' }), null);
});

test('two interfaces of the same IED have distinct physical attachment points', () => {
  for (const id of ['prot', 'mc']) {
    const ports = model.interfaces.filter(p => p.device_id === id);
    assert.equal(ports.length, 2);
    assert.notDeepEqual(ports[0].anchor, ports[1].anchor);
    assert.notEqual(ports[0].interface_id, ports[1].interface_id);
  }
});

test('same endpoints on multiple networks resolve by their ports', () => {
  const a = { endpoint_a: ['one', 'st'], endpoint_b: ['two', 'st'] };
  const b = { endpoint_a: ['one', 'pr'], endpoint_b: ['two', 'pr'] };
  const alternate = { routes: new Map([['a', { link: a, points: [{x:1,y:1}] }], ['b', {link:b,points:[{x:2,y:2}]}]]) };
  assert.deepEqual(topology.messageRoute(alternate, {sender_id:'one',receiver_id:'two',source_port:'pr',target_port:'pr'}), [{x:2,y:2}]);
  assert.equal(topology.messageRoute(alternate, {sender_id:'one',receiver_id:'two'}), null);
});

test('constant-speed interpolation follows bends and tolerates zero length segments', () => {
  const points = [{x:0,y:0},{x:30,y:0},{x:30,y:0},{x:30,y:70}];
  assert.deepEqual(topology.pointAt(points, .5), {x:30,y:20});
  assert.deepEqual(topology.pointAt(points, 0), points[0]);
  assert.deepEqual(topology.pointAt(points, 1), points[3]);
  assert.deepEqual(topology.pointAt([{x:5,y:6},{x:5,y:6}], .4), {x:5,y:6});
});

const idsEvent = (interfaceId, type = 'flow') => ({source_type:'ids', provider_id:'provider', event_id:'e1',
  raw:{source_id:'ids-station',metadata:{interface_id:interfaceId},data:{event_type:type},data_origin:'synthetic'}});

test('IDS status comes only from events on the configured capture interface', () => {
  const probe = model.networks[0].probe;
  assert.equal(topology.summarize({management:{station:{effects:{attack:true}}}},probe).status,'unknown');
  const data = {observations:{events:[idsEvent('span-station'),idsEvent('span-process','alert')]}};
  assert.equal(topology.summarize(data,probe).status,'receiving');
  assert.equal(topology.summarize(data,model.networks[1].probe).alerts.length,1);
  assert.equal(topology.eventMatches(idsEvent('unknown','alert'),probe),false);
  assert.equal(topology.eventMatches({...idsEvent('span-station'),source_type:'business'},probe),false);
});

test('replay IDS events and optional provider binding remain isolated', () => {
  const probe = {...model.networks[0].probe,provider_id:'provider'};
  assert.equal(topology.eventMatches({...idsEvent('span-station'),provider_id:'other'},probe),false);
  assert.equal(topology.summarize({observations:{ids_events:[idsEvent('span-station','alert')]}},probe).status,'alert');
  assert.equal(topology.summarize({observations:{events:[]}},probe).status,'unknown');
});

test('old scenarios still render original physical links with no invented IDS', () => {
  const legacy = topology.build({...scene,layout:{}},coordinates);
  assert.equal(legacy.assets.length,0);
  assert.equal(legacy.cables.length,scene.links.length);
  assert.equal(legacy.routes.size,scene.links.length);
});
