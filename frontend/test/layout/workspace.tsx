// Isolated UI fixture: actual timeline/player components, no account or network data.
import {createRoot} from 'react-dom/client';
import {useState} from 'react';
import Timeline from '../../src/Timeline';
import PlaybackSpeed from '../../src/PlaybackSpeed';
import type {Project} from '../../src/domain';
import '../../src/style.css';
import '../../src/layout.css';
import '../../src/tasks/workspace.css';

const pause = (ms:number) => new Promise(resolve => setTimeout(resolve, ms));
const sizes = [[1920,1080],[1920,1200],[1536,864],[1536,960],[1280,720],[1280,800],
  [1366,650],[1440,810],[1024,576],[1024,640],[900,600],[800,500],[600,480]];
const parameters = new URLSearchParams(location.search);
const poster='data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="900" viewBox="0 0 1600 900"><rect width="1600" height="900" fill="#264b58"/><rect x="4" y="4" width="1592" height="892" fill="none" stroke="#8bd9cd" stroke-width="8"/><path d="M0 0L1600 900M1600 0L0 900" stroke="#416f7c" stroke-width="3"/><text x="800" y="450" text-anchor="middle" fill="#d3f0e8" font-size="72">16:9 layout fixture</text></svg>');
const project:Project = {id:'layout-fixture',name:'隔离测试',source_dir:null,revision:1,updated_at:'',
  duration_ms:7_200_000,fixed_tracks:['scene','posture','category'],
  videos:[{id:'v0001',name:'测试视频',relative_path:'fixture.mp4',duration_ms:7_200_000,start_ms:0,end_ms:7_200_000,url:'',thumbnail_url:null}],
  annotations:Object.fromEntries(['scene','posture','category'].map(axis=>[axis,Array.from({length:12},(_,lane)=>({id:axis+lane,label:'测试',start_ms:0,end_ms:3_600_000,lane}))]))};

function Fixture(){
  const [time,setTime]=useState(0),[speed,setSpeed]=useState(1);
  return <div className="app-shell video-workspace" data-task-review={parameters.get('review')??'false'}>
    <header className="topbar"><strong>DataMark</strong><h1 className="compact-project-name">隔离布局测试 · 长项目名称不会撑开工作台</h1></header>
    <main className="editor-grid"><section className="left-column">
      <div className="preview-panel"><div className="panel-heading"><h2>视频预览</h2></div>
        <div className="video-stage"><video muted playsInline poster={poster} aria-label="隔离测试视频"/></div>
        <div className="player-controls"><div className="play-controls"><button>上一段</button><button className="play-button">▶</button><button>下一段</button><span className="player-time">00:00:00.000 / 02:00:00.000</span></div>
          <div className="play-options"><PlaybackSpeed value={speed} onChange={setSpeed}/><button className="icon-button">♫</button><button className="icon-button">⛶</button></div></div>
      </div>
      <Timeline project={project} currentTime={time} activeTrack="posture" selectedId={null} onSeek={setTime} onTrackSelect={()=>{}} onSegmentSelect={()=>{}}
        anchorControls={<><button>添加锚点</button><button>选中区间 00:00:00</button><button>清空</button></>}/>
    </section><aside className="workspace-sidebar">
      <section className="sidebar-module"><div className="panel-heading">项目管理</div><div className="module-content">隔离测试项目</div></section>
      <section className="sidebar-module"><div className="panel-heading">长内容及内部滚动测试</div><div className="module-content output-actions">{Array.from({length:40},(_,i)=><button className="secondary" key={i}>操作 {i+1} · 不得撑大视频所在行</button>)}</div></section>
    </aside></main><footer className="statusbar">已保存 · 布局测试</footer>
  </div>;
}

function measure(){
  const get=(selector:string)=>document.querySelector<HTMLElement>(selector)!;
  const rect=(selector:string)=>get(selector).getBoundingClientRect();
  const app=get('.app-shell'),side=get('.workspace-sidebar'),tracks=get('.tl-tracks-viewport');
  const preview=rect('.preview-panel'),timeline=rect('.tl-root'),video=rect('video'),controls=rect('.player-controls');
  const errors:string[]=[];
  const check=(ok:boolean,label:string)=>{if(!ok)errors.push(label);};
  check(app.scrollHeight<=app.clientHeight+1 && document.documentElement.scrollHeight<=innerHeight+1,'whole-page overflow');
  check(app.scrollWidth<=app.clientWidth+1,'horizontal page overflow');
  check(timeline.bottom<=innerHeight && timeline.top>=preview.bottom,'timeline outside viewport or overlapping video');
  check(timeline.top-preview.bottom<=12,'blank between preview and timeline');
  check(video.height>=65 && video.width>=100,'video collapsed');
  check(controls.bottom<=preview.bottom+1 && controls.top>=video.bottom-1,'player controls overlap video or clipped');
  for(const button of get('.player-controls').querySelectorAll('button')){
    const r=button.getBoundingClientRect();
    check(r.left>=controls.left-1 && r.right<=controls.right+1 && r.bottom<=controls.bottom+1,'clipped player button');
  }
  check(tracks.clientHeight>=34 && tracks.scrollHeight>tracks.clientHeight,'annotation lanes not scrollable');
  const top=rect('.tl-ruler').top;
  tracks.scrollTop=80;side.scrollTop=100;
  check(tracks.scrollTop>0 && side.scrollTop>0,'panel cannot scroll');
  check(rect('.tl-ruler').top===top && app.scrollTop===0,'panel scroll moved ruler or whole page');
  tracks.scrollTop=0;side.scrollTop=0;
  const horizontal=get('.tl-scroll');horizontal.scrollLeft=200;
  check(horizontal.scrollLeft>0,'timeline cannot scroll horizontally');
  horizontal.scrollLeft=0;
  return {viewport:[innerWidth,innerHeight],review:parameters.get('review'),gap:timeline.top-preview.bottom,videoHeight:video.height,errors};
}

if(parameters.has('fixture')){
  createRoot(document.getElementById('root')!).render(<Fixture/>);
  Object.assign(window,{measureLayout:measure});
}else{
  const results:ReturnType<typeof measure>[]=[];
  const run=async()=>{
    const frame=document.createElement('iframe');frame.style.border='0';document.body.append(frame);
    // Reuse each mounted component while resizing to exercise ResizeObserver.
    for(const review of ['false','true']){
      frame.src=`?fixture=1&review=${review}`;
      await new Promise<void>(resolve=>frame.onload=()=>resolve());await pause(200);
      for(const [width,height] of sizes){
        frame.style.width=width+'px';frame.style.height=height+'px';await pause(80);
        results.push((frame.contentWindow as unknown as {measureLayout:()=>ReturnType<typeof measure>}).measureLayout());
      }
    }
    frame.remove();
    const summary=document.createElement('h1');summary.textContent=`${results.every(r=>!r.errors.length)?'PASS':'FAIL'}: ${results.length} 布局场景；${results.filter(r=>r.errors.length).length} 失败`;document.body.append(summary);
    const output=document.createElement('pre');output.id='layout-results';output.textContent=JSON.stringify(results);document.body.append(output);
  };
  run().catch(error=>{document.body.dataset.layoutError=String(error);});
}
