import './playback-speed.css';

const speeds = [0.5, 1, 2, 5, 10, 50, 100];

export default function PlaybackSpeed({ value, onChange, maximum = 100 }: {
  value: number;
  maximum?: number;
  onChange: (value: number) => void;
}) {
  return <div className="playback-speed">
    <span className="playback-speed-label">倍速</span>
    <div className="playback-speed-segments" role="group" aria-label="播放倍速">
      <span className="playback-speed-highlight" aria-hidden="true"
        style={{ transform: `translateX(${speeds.indexOf(value) * 100}%)` }}/>
      {speeds.map(speed => <button type="button" key={speed}
        aria-label={`${speed} 倍速`} aria-pressed={value === speed} disabled={speed > maximum}
        title={speed > maximum ? "当前素材共用普通视频缓存，暂支持最高 10 倍速" : undefined}
        onClick={() => onChange(speed)}>{speed}×</button>)}
    </div>
  </div>;
}
