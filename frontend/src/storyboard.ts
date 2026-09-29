export interface StoryboardManifest {
  state: 'ready';
  interval_ms: number;
  tile_width: number;
  tile_height: number;
  columns: number;
  rows: number;
  frame_count: number;
  sheets: string[];
}

export type StoryboardStatus = StoryboardManifest | {
  state: 'idle' | 'queued' | 'running' | 'error';
  detail?: string | null;
};

export interface StoryboardFrame {
  index: number;
  localTime: number;
  url: string;
  column: number;
  row: number;
  columns: number;
  rows: number;
  aspectRatio: number;
}

export function isStoryboardManifest(value: unknown): value is StoryboardManifest {
  if (!value || typeof value !== 'object') return false;
  const item = value as StoryboardManifest;
  return item.state === 'ready' &&
    [item.interval_ms, item.tile_width, item.tile_height, item.columns, item.rows, item.frame_count]
      .every((number) => Number.isSafeInteger(number) && number > 0) &&
    Array.isArray(item.sheets) && item.sheets.length >= Math.ceil(item.frame_count / (item.columns * item.rows)) &&
    item.sheets.every((url) => typeof url === 'string' && url.length > 0);
}

/** Keep preview sampling separate from the exact annotation cursor. */
export function storyboardFrame(manifest: StoryboardManifest, localTime: number): StoryboardFrame {
  const index = Math.min(manifest.frame_count - 1, Math.max(0,
    Math.floor((Number.isFinite(localTime) ? localTime : 0) / manifest.interval_ms)));
  const perSheet = manifest.columns * manifest.rows;
  const cell = index % perSheet;
  return {
    index,
    localTime: index * manifest.interval_ms,
    url: manifest.sheets[Math.floor(index / perSheet)],
    column: cell % manifest.columns,
    row: Math.floor(cell / manifest.columns),
    columns: manifest.columns,
    rows: manifest.rows,
    aspectRatio: manifest.tile_width / manifest.tile_height,
  };
}

export function spritePosition(frame: StoryboardFrame): string {
  return `${frame.columns > 1 ? frame.column / (frame.columns - 1) * 100 : 0}% ${frame.rows > 1 ? frame.row / (frame.rows - 1) * 100 : 0}%`;
}
