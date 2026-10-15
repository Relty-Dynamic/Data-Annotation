import assert from 'node:assert/strict';
import {after, before, test} from 'node:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {createServer} from 'vite';

let server;
let Timeline;
before(async () => {
  server = await createServer({server: {middlewareMode: true}, appType: 'custom', optimizeDeps: {noDiscovery: true, include: []}});
  ({default: Timeline} = await server.ssrLoadModule('/src/Timeline.tsx'));
});
after(async () => { await server?.close(); });

function fixture() {
  const tracks = ['scene', 'posture', 'category', 'habit', 'custom_state', 'custom_event'];
  return {
    id: 'timeline-render', duration_ms: 10000, videos: [], fixed_tracks: tracks.slice(0, 4),
    custom_tracks: [{id: 'custom_state', name: '自定义状态', mode: 'state'}, {id: 'custom_event', name: '自定义事件', mode: 'event'}],
    annotations: Object.fromEntries(tracks.map(track => [track, [
      {id: `${track}-main`, label: '主状态', start_ms: 0, end_ms: 8000, lane: 0},
      {id: `${track}-child`, label: '并行状态', start_ms: 1000, end_ms: 6000, lane: 500},
    ]])),
  };
}

function render(project, extra = {}) {
  return renderToStaticMarkup(React.createElement(Timeline, {
    project, currentTime: 0, activeTrack: 'scene', selectedId: 'scene-child',
    onSeek() {}, onTrackSelect() {}, onSegmentSelect() {}, ...extra,
  }));
}

test('available track kinds display saved rows while the retired habit axis stays hidden', () => {
  const html = render(fixture());
  assert.equal((html.match(/class="tl-row tl-track-row /g) ?? []).length, 10);
  assert.equal((html.match(/data-lane="0"/g) ?? []).length, 5);
  assert.equal((html.match(/data-lane="500"/g) ?? []).length, 5);
  assert.ok(!html.includes('习惯'));
  assert.ok(html.includes('自定义事件'));
  assert.ok(!html.includes('data-lane="499"'));
  assert.ok(html.includes('场景子轴 500：并行状态'));
  assert.ok(html.includes('主轴及 1 条子轴'));
});

test('empty tracks retain a main row', () => {
  const project = fixture();
  project.annotations.scene = [];
  const empty = render({...project, selected_tracks: ['scene']});
  assert.equal((empty.match(/data-lane="0"/g) ?? []).length, 1);
  assert.ok(!empty.includes('data-lane="500"'));
});

test('only two most recent anchors render as unnumbered markers without an automatic range', () => {
  const html = render(fixture(), {anchors: [1000, 2000, 3000]});
  assert.equal((html.match(/class="tl-anchor"/g) ?? []).length, 2);
  assert.ok(html.includes('aria-label="锚点，00:00:02.000"'));
  assert.ok(html.includes('aria-label="锚点，00:00:03.000"'));
  assert.ok(!html.includes('aria-label="锚点，00:00:01.000"'));
  assert.ok(!html.includes('tl-anchor-label'));
  assert.ok(!html.includes('tl-range-overlay'));
  const selected = render(fixture(), {anchors: [3000, 2000], selectedRange: {start_ms: 2000, end_ms: 3000}});
  assert.ok(selected.includes('tl-range-overlay'));
});

test('boundary controls only appear on the selected block and expose the active edge', () => {
  const project = fixture();
  const onBoundarySelect = () => true;
  assert.ok(!render(project, {selectedId: null, onBoundarySelect}).includes('data-boundary='));
  const selected = render(project, {selectedBoundary: 'end', onBoundarySelect});
  assert.equal((selected.match(/data-boundary=/g) ?? []).length, 2);
  assert.match(selected, /class="[^"]*tl-boundary-end[^"]*tl-boundary-selected[^"]*"[^>]*aria-pressed="true"/);
  assert.match(selected, /class="[^"]*tl-boundary-start[^"]*"[^>]*aria-pressed="false"/);
  assert.ok(selected.includes('并行状态：开始边界，00:00:01.000'));
  assert.ok(selected.includes('并行状态：结束边界，00:00:06.000'));
  assert.ok(selected.includes('data-boundary-keep="true"'));
  assert.ok(!render(project, {activeTrack: 'posture', onBoundarySelect}).includes('data-boundary='));
});

test('open intervals expose their start only and historical zero-length annotations remain movable', () => {
  const project = fixture();
  project.annotations.scene = [{id: 'open', label: '记录中', start_ms: 1000, end_ms: null, lane: 0}];
  const open = render(project, {selectedId: 'open', onBoundarySelect: () => true});
  assert.equal((open.match(/data-boundary=/g) ?? []).length, 1);
  assert.ok(open.includes('data-boundary="start"'));
  project.annotations.scene = [{id: 'old', label: '旧标注', start_ms: 2000, end_ms: 2000, kind: 'point', lane: 0}];
  const legacy = render(project, {selectedId: 'old', onBoundarySelect: () => true});
  assert.equal((legacy.match(/data-boundary=/g) ?? []).length, 1);
  assert.ok(legacy.includes('旧标注：标注位置，00:00:02.000'));
  assert.ok(!legacy.includes('时点'));
  assert.ok(!legacy.includes('瞬时'));
});

test('very short annotations keep distinct boundary hit targets', () => {
  const project = fixture();
  project.videos = [{id: 'video', start_ms: 0, end_ms: 10000}];
  project.annotations.scene = [{id: 'short', label: '短标注', start_ms: 0, end_ms: 1, lane: 0}];
  const html = render(project, {selectedId: 'short', onBoundarySelect: () => true});
  assert.match(html, /class="tl-boundary-handle tl-boundary-start tl-boundary-narrow" style="left:0%;top:5px"/);
  assert.match(html, /class="tl-boundary-handle tl-boundary-end tl-boundary-narrow" style="left:0\.01%;top:23px"/);
});
