import test from 'node:test';
import assert from 'node:assert/strict';
import { canSkipFailed, measuredPercent, preparationPresentation, preparationItemLabel, preparationRetryAction } from '../src/preparationPresentation.ts';

test('skip only offers a complete partial batch with some ready clips', () => {
  const partial = {state: 'partial', ready: 2, failed: 1, running: 0, queued: 0, total: 3,
    items: [{state: 'ready'}, {state: 'error'}, {state: 'ready'}]};
  assert.equal(canSkipFailed(partial), true);
  for (const change of [{state: 'running'}, {running: 1}, {queued: 1}, {ready: 0}, {failed: 0}, {items: []}]) {
    assert.equal(canSkipFailed({...partial, ...change}), false);
  }
  assert.equal(canSkipFailed(partial, 'Disconnected'), false);
  assert.equal(canSkipFailed(null), false);
});

const base = {state:'running', stage:'cache', progress:12.5};

test('individual failures leave active clips progressing without offering premature retry', () => {
  const status = {...base, operation: 'download', failed: 2};
  const view = preparationPresentation(status);
  assert.equal(view.title, '下载到本机');
  assert.equal(view.progress, 12.5);
  assert.match(view.description, /2 段准备失败，其余片段继续准备/);
  assert.deepEqual(preparationRetryAction(status), {visible: false, label: '重新获取进度'});
});

test('transport errors can refresh active progress but terminal partial retries failed clips', () => {
  const running = {...base, failed: 2};
  assert.deepEqual(preparationRetryAction(running, 'Connection interrupted'), {visible: true, label: '重新获取进度'});
  const partial = {...running, state: 'partial'};
  assert.deepEqual(preparationRetryAction(partial), {visible: true, label: '重试失败项'});
  assert.equal(preparationPresentation(partial).title, '部分素材准备失败');
  assert.match(preparationPresentation(partial).description, /已完成的缓存保留/);
  assert.equal(preparationRetryAction({...running, state: 'ready', failed: 0}).visible, false);
});

test('NAS cache lookup does not claim the source is being transcoded', () => {
  const view = preparationPresentation({...base, operation:'nas'});
  assert.equal(view.title, '检查 NAS 缓存');
  assert.equal(view.phase, 'prepare');
  assert.equal(preparationItemLabel('running','nas'), '检查 NAS 缓存');
  assert.equal(preparationItemLabel('ready','nas'), null);
  assert.equal(preparationPresentation({...base,operation:'nas',state:'partial'}).title, '部分素材准备失败');
});

test('legacy source encoding never gets mislabeled as merely checking a cache', () => {
  assert.equal(preparationPresentation(base).title, '正在准备播放缓存');
  assert.equal(preparationPresentation({...base,stage:'compact'}).title, '正在准备播放缓存');
});

test('backend operation explicitly distinguishes first generation, reuse and asset preparation', () => {
  assert.match(preparationPresentation({...base,operation:'generate'}).title, /首次生成播放缓存/);
  assert.match(preparationPresentation({...base,operation:'reuse'}).title, /复用已有缓存/);
  assert.match(preparationPresentation({...base,operation:'check'}).title, /检查本机缓存/);
  assert.match(preparationPresentation({...base,operation:'assets'}).title, /快进和缩略图/);
});

test('remote processing and download identify where preparation is happening', () => {
  const remote = preparationPresentation({...base,stage:'compact',operation:'remote'});
  assert.equal(remote.title,'服务器处理中');
  assert.equal(remote.activity,'服务器处理中');
  assert.equal(remote.phase,'prepare');
  assert.equal(remote.progress,12.5);
  const download = preparationPresentation({...base,stage:'local',operation:'download'});
  assert.equal(download.title,'下载到本机');
  assert.equal(download.activity,'下载到本机');
  assert.equal(download.phase,'prepare');
  assert.match(download.description,/校验/);
});

test('remote completion still waits for browser images and failed preparation remains retryable', () => {
  assert.equal(preparationPresentation({...base,stage:'browser',operation:'download'}).title,'正在载入预览图片');
  assert.equal(preparationPresentation({...base,state:'partial',operation:'remote'}).title,'部分素材准备失败');
  assert.equal(preparationPresentation({...base,operation:'download'},'服务器连接中断').title,'素材准备需要重试');
  assert.equal(preparationItemLabel('running','remote'),'服务器处理中');
  assert.equal(preparationItemLabel('running','download'),'下载到本机');
  assert.equal(preparationItemLabel('error','remote'),null);
  assert.equal(preparationItemLabel('ready','download'),null);
});

test('reconnection stays in preparation and preserves the measured progress', () => {
  for (const stage of ['compact', 'local']) {
    const status = {...base, stage, operation:'reconnect', progress:86.5};
    const view = preparationPresentation(status);
    assert.equal(view.title,'重新连接服务器');
    assert.equal(view.activity,'重新连接服务器');
    assert.equal(view.phase,'prepare');
    assert.equal(view.progress,86.5);
    assert.match(view.description,/自动重试/);
    assert.match(view.description,/下载进度会保留/);
    assert.equal(status.state,'running');
  }
  assert.equal(preparationItemLabel('running','reconnect'),'重新连接服务器');
  assert.equal(preparationPresentation({...base,operation:'reconnect',progress:null}).progress,null);
});

test('reconnection status does not hide terminal failures or persist after recovery', () => {
  assert.equal(preparationPresentation({...base,operation:'reconnect',state:'partial'}).title,'部分素材准备失败');
  assert.equal(preparationPresentation({...base,operation:'reconnect'},'自动重试已结束').title,'素材准备需要重试');
  assert.equal(preparationPresentation({...base,operation:'reconnect',state:'idle'}).title,'等待继续准备素材');
  assert.equal(preparationItemLabel('error','reconnect'),null);
  assert.equal(preparationItemLabel('ready','reconnect'),null);
  assert.equal(preparationPresentation({...base,operation:'download',progress:86.5}).title,'下载到本机');
  assert.equal(preparationPresentation({...base,operation:'remote'}).title,'服务器处理中');
  assert.equal(preparationPresentation({...base,operation:'reconnect',state:'ready',progress:100}).title,'标注素材已就绪');
});

test('per-file cache stages stay in one stable overall preparation phase', () => {
  for (const stage of ['cache','compact','local','cache']) {
    const view=preparationPresentation({...base,stage});
    assert.equal(view.phase,'prepare');
    assert.match(view.progressLabel,/全部视频缓存准备/);
    assert.equal(view.progress,12.5);
  }
});

test('browser image zero is labeled as a new measured phase rather than overall progress resetting', () => {
  const view=preparationPresentation({...base,stage:'browser',operation:'reuse',progress:0});
  assert.equal(view.phase,'browser');
  assert.equal(view.title,'正在载入预览图片');
  assert.match(view.progressLabel,/当前阶段.*图片载入/);
  assert.equal(view.progress,0);
});

test('unknown progress remains indeterminate and known values are only bounded', () => {
  for (const value of [null,undefined,NaN,Infinity,'40']) assert.equal(measuredPercent(value),null);
  assert.equal(measuredPercent(31.25),31.25);
  assert.equal(measuredPercent(-2),0);
  assert.equal(measuredPercent(110),100);
  assert.equal(preparationPresentation({...base,checking:true,progress:0}).progress,null);
});

test('one hundred percent cannot itself mark material ready or enable the final phase', () => {
  assert.equal(preparationPresentation({...base,progress:100}).phase,'prepare');
  assert.equal(preparationPresentation({...base,state:'ready',progress:100}).phase,'ready');
  assert.notEqual(preparationPresentation({...base,state:'ready',progress:100},'图片读取失败').phase,'ready');
});

test('per-file reuse labels only claim reuse when backend confirms it', () => {
  assert.equal(preparationItemLabel('ready','reuse'),'已复用');
  assert.equal(preparationItemLabel('ready'),null);
  assert.equal(preparationItemLabel('running','generate'),'首次生成');
  assert.equal(preparationItemLabel('running','assets'),'准备快进和图片');
  assert.equal(preparationItemLabel('error','generate'),null);
});
