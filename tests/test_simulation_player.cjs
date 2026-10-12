const { test } = require('node:test');
const assert = require('node:assert/strict');
const Player = require('../sim2030/presentation/static/simulation-player.js');

function fixture(before = async () => {}) {
  let counter = 0, status = 'ready', time = 0;
  const jobs = new Map(), calls = [], errors = [];
  const clock = {
    time: 0,
    now() { return clock.time; },
    setTimeout(fn, delay) { const id = ++counter; jobs.set(id, {fn, delay, at:clock.time + delay}); return id; },
    clearTimeout(id) { jobs.delete(id); },
    next() {
      const [id, job] = [...jobs].sort((a,b)=>a[1].at-b[1].at)[0] || [];
      if (!job) return Promise.resolve();
      jobs.delete(id); clock.time = job.at;
      return job.fn();
    },
  };
  const player = new Player({ clock, onError:error=>errors.push(error), send:async(action, runId)=>{
    calls.push({action,runId});
    await before(action);
    if (action === 'start') status = 'running';
    if (action === 'pause') status = 'paused';
    if (action === 'stop') status = 'finished';
    if (action === 'step' || action === 'step_once') time += 100000;
    if (action === 'step_once') status = 'paused';
    return {status:'accepted',summary:{status,time_us:time}};
  }});
  player.bind('run-1');
  return {player,clock,jobs,calls,errors};
}
const actions = f => f.calls.map(c=>c.action);
const flush = async () => { for(let i=0;i<5;i++) await Promise.resolve(); };

test('start automatically schedules steps; no independent automatic switch', async()=>{
  const f=fixture();
  await f.player.command('start');
  assert.equal(f.player.running,true);
  assert.equal(f.jobs.size,1);
  await f.clock.next();
  assert.deepEqual(actions(f),['start','step']);
  assert.equal([...f.jobs.values()][0].delay,8000);
});

test('speed changes replace a pending timer instead of creating parallel loops', async()=>{
  const f=fixture(); await f.player.command('start');
  for(const rate of [.125,.25,.5,1,2,5]) {
    f.player.setRate(rate);
    assert.equal(f.jobs.size,1);
    assert.equal([...f.jobs.values()][0].delay,1000/rate);
  }
  await f.clock.next();
  assert.deepEqual(actions(f),['start','step']);
});

test('pause drains the already sent step and prevents all subsequent automatic steps', async()=>{
  let release;
  const gate=new Promise(resolve=>release=resolve);
  const f=fixture(action=>action==='step'?gate:Promise.resolve());
  await f.player.command('start');
  const tick=f.clock.next(); await flush();
  const pause=f.player.command('pause'); await flush();
  assert.deepEqual(actions(f),['start','step']);
  assert.equal(f.player.running,false);
  assert.equal(f.player.busy,true);
  release(); await tick; await pause;
  assert.deepEqual(actions(f),['start','step','pause']);
  assert.equal(f.player.status,'paused'); assert.equal(f.jobs.size,0);
  await f.clock.next(); assert.equal(f.calls.length,3);
});

test('changing speed during a slow step waits for that request to finish', async()=>{
  let release;
  const gate=new Promise(resolve=>release=resolve);
  const f=fixture(action=>action==='step'?gate:Promise.resolve());
  await f.player.command('start');
  const tick=f.clock.next(); await flush();
  f.player.setRate(1);
  assert.equal(f.jobs.size,0); assert.equal(f.calls.length,2);
  release(); await tick;
  assert.equal(f.jobs.size,1); assert.equal([...f.jobs.values()][0].delay,1000);
});

test('slow transport never queues catch-up steps', async()=>{
  const f=fixture(action=>{if(action==='step') f.clock.time+=1500;});
  f.player.setRate(1); await f.player.command('start'); await f.clock.next();
  assert.deepEqual(actions(f),['start','step']);
  assert.equal(f.jobs.size,1); assert.equal([...f.jobs.values()][0].delay,0);
});

test('rapid double clicks cannot duplicate start requests', async()=>{
  let release;
  const gate=new Promise(resolve=>release=resolve);
  const f=fixture(action=>action==='start'?gate:Promise.resolve());
  const first=f.player.command('start'); await flush();
  assert.equal(await f.player.command('start'),false);
  release(); await first;
  assert.deepEqual(actions(f),['start']); assert.equal(f.jobs.size,1);
});

test('a manual step stays paused and never starts a timer', async()=>{
  const f=fixture(); await f.player.command('step_once');
  assert.deepEqual(actions(f),['step_once']); assert.equal(f.player.status,'paused');
  assert.equal(f.jobs.size,0); assert.equal(f.player.running,false);
  await f.player.command('start');
  assert.equal(await f.player.command('step_once'),false);
});

test('resume starts exactly one loop and stop permanently ends this run', async()=>{
  const f=fixture(); await f.player.command('start'); await f.clock.next();
  await f.player.command('pause'); await f.player.command('start'); await f.clock.next();
  assert.equal(f.jobs.size,1); await f.player.command('stop');
  assert.equal(f.jobs.size,0); assert.equal(f.player.status,'finished');
  assert.equal(await f.player.command('start'),false);
});

test('transport failure halts scheduling and can be retried explicitly', async()=>{
  let fail=true;
  const f=fixture(action=>{if(action==='step' && fail) throw new Error('offline');});
  await f.player.command('start'); await f.clock.next();
  assert.equal(f.errors.length,1); assert.equal(f.jobs.size,0); assert.equal(f.player.running,false);
  fail=false; await f.player.command('start'); await f.clock.next();
  assert.equal(f.player.error,null); assert.equal(f.jobs.size,1);
});

test('rejected server operation is not shown as successful', async()=>{
  const f=fixture(); f.player.send=async()=>({status:'rejected',reason:'unavailable'});
  assert.equal(await f.player.command('start'),false);
  assert.equal(f.jobs.size,0); assert.match(f.errors[0].message,/unavailable/);
});

test('finished steps stop the driver without scheduling a new request', async()=>{
  const f=fixture();
  f.player.send=async action=>({status:'accepted',summary:{status:action==='step'?'finished':'running'}});
  await f.player.command('start'); await f.clock.next();
  assert.equal(f.player.status,'finished'); assert.equal(f.jobs.size,0);
});

test('stale run observations are ignored; new runs retain selected speed but stay ready', async()=>{
  const f=fixture(); f.player.setRate(.5);
  f.player.bind('run-2');
  f.player.observe({run_id:'run-1',status:'finished',dt_us:300000});
  assert.equal(f.player.status,'ready'); assert.equal(f.player.dtUs,100000);
  f.player.observe({run_id:'run-2',status:'ready',dt_us:200000});
  assert.equal(f.player.dtUs,200000); assert.equal(f.player.rate,.5); assert.equal(f.jobs.size,0);
});

test('run replacement cannot proceed while a step is in flight', async()=>{
  let release; const gate=new Promise(resolve=>release=resolve);
  const f=fixture(action=>action==='step'?gate:Promise.resolve());
  await f.player.command('start'); const tick=f.clock.next(); await flush();
  assert.throws(()=>f.player.bind('run-2'),/等待/);
  const pause=f.player.command('pause'); release(); await tick; await pause;
  f.player.bind('run-2'); assert.equal(f.jobs.size,0);
  assert.ok(f.calls.every(c=>c.runId==='run-1'));
});

test('only the supported multipliers are accepted and the default is 0.125x', ()=>{
  const f=fixture();
  assert.equal(f.player.rate,.125);
  for(const rate of [.125,.25,.5,1,2,5]) f.player.setRate(rate);
  for(const rate of [0,.1,.3,1.5,3,10]) assert.throws(()=>f.player.setRate(rate));
});
