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

test('all track kinds display a main row and their saved child row without empty intermediate rows', () => {
  const html = render(fixture());
  assert.equal((html.match(/class="tl-row tl-track-row /g) ?? []).length, 12);
  assert.equal((html.match(/data-lane="0"/g) ?? []).length, 6);
  assert.equal((html.match(/data-lane="500"/g) ?? []).length, 6);
  assert.ok(!html.includes('data-lane="499"'));
  assert.ok(html.includes('场景子轴 500：并行状态'));
  assert.ok(html.includes('主轴及 1 条子轴'));
});

test('empty tracks retain a main row and comparisons keep both hierarchies', () => {
  const project = fixture();
  project.annotations.scene = [];
  const empty = render({...project, selected_tracks: ['scene']});
  assert.equal((empty.match(/data-lane="0"/g) ?? []).length, 1);
  assert.ok(!empty.includes('data-lane="500"'));
  const comparison = render(project, {comparison: {track: 'posture', segments: project.annotations.posture}});
  assert.ok(comparison.includes('外部 · 姿势'));
  assert.ok(comparison.includes('项目 · 姿势'));
  assert.equal((comparison.match(/data-lane="500"/g) ?? []).length, 2);
});
