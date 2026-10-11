const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const topology = require('../sim2030/presentation/static/network-topology.js');
const scene = JSON.parse(fs.readFileSync(path.join(__dirname,'../scenarios/substation-network-attack.json'),'utf8'));
const coords = Object.fromEntries(scene.devices.map(d=>[d.device_id,d.layout]));
const network = topology.build(scene,coords);
const data = {run_id:'demo-run',topology:scene,management:{brk:{state:{position:'closed'}}},observations:{events:[]},recognition:[]};

test('presentation-only stages do not mutate business state or fabricate IDS observations',()=>{
  const before=JSON.stringify(data);
  const demo = new topology.CyberDemo(); demo.sync(data);
  for(let i=0;i<demo.config.stages.length;i++) { demo.select(i); demo.plan(network,coords); }
  assert.equal(JSON.stringify(data),before);
  assert.equal(data.management.brk.state.position,'closed');
  assert.deepEqual(data.observations.events,[]);
  assert.deepEqual(data.recognition,[]);
});

test('the unauthorized command follows the existing station and process routes',()=>{
  const demo = new topology.CyberDemo(); demo.sync(data); demo.select(3);
  const plan = demo.plan(network,coords);
  assert.deepEqual(plan.paths.map(p=>p.id),['L-station-mc','L-mc-brk']);
  for(const p of plan.paths) assert.deepEqual(p.points,network.routes.get(p.id).points);
  assert.equal(network.routes.get('L-station-mc').network_id,'station');
  assert.equal(network.routes.get('L-mc-brk').network_id,'process');
});

test('the last control hop remains hardwired and only carries a risk annotation',()=>{
  const demo=new topology.CyberDemo(); demo.sync(data); demo.select(4);
  const plan=demo.plan(network,coords);
  assert.equal(plan.paths.at(-1).id,'L-ctl-brk-down');
  assert.equal(network.routes.get('L-ctl-brk-down').link.link_type,'hardwire');
  assert.equal(plan.marks[0].id,'brk'); assert.equal(plan.marks[0].role,'risk');
});

test('monitoring and reporting stages use only their own passive cables',()=>{
  const demo=new topology.CyberDemo(); demo.sync(data); demo.select(5);
  assert.deepEqual(demo.plan(network,coords).paths.map(p=>p.id),['station:mirror','process:mirror']);
  demo.select(6);
  assert.deepEqual(demo.plan(network,coords).paths.map(p=>p.id),['station:management','process:management']);
});

test('all configured stages resolve to real device, network and cable references',()=>{
  const demo=new topology.CyberDemo(); demo.sync(data);
  demo.config.stages.forEach((stage,i)=>{
    demo.select(i); const plan=demo.plan(network,coords);
    assert.equal(plan.paths.length,stage.routes.length+stage.cables.length);
    assert.equal(plan.marks.length,stage.marks.length);
  });
});

test('polling preserves the selected stage; switching runs or to the baseline clears it',()=>{
  const demo=new topology.CyberDemo(); demo.sync(data); demo.select(4);
  demo.sync(JSON.parse(JSON.stringify(data))); assert.equal(demo.phase,4);
  demo.sync({...data,run_id:'other-run'}); assert.equal(demo.phase,0);
  demo.sync({run_id:'normal',topology:{layout:{}}}); assert.equal(demo.active,false);
  assert.deepEqual(demo.plan(network,coords),{paths:[],marks:[]});
});

test('stage navigation clamps to valid bounds and cannot enable a non-demo scenario',()=>{
  const demo=new topology.CyberDemo(); demo.select(3); assert.equal(demo.active,false);
  demo.sync(data); demo.select(-3); assert.equal(demo.phase,0);
  demo.select(999); assert.equal(demo.phase,6);
  demo.select(NaN); assert.equal(demo.phase,6);
});
