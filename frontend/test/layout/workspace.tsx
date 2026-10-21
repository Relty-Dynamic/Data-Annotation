// Isolated UI fixture: actual timeline/player components, no account or network data.
import {createRoot} from 'react-dom/client';
import {useEffect,useRef,useState} from 'react';
import VideoStage from '../../src/VideoStage';
import {revealInPanel} from '../../src/revealInPanel';
import Timeline from '../../src/Timeline';
import PlaybackSpeed from '../../src/PlaybackSpeed';
import {applyPlaybackRate, playbackProfile} from '../../src/playback';
import type {Project,Track} from '../../src/domain';
import '../../src/style.css';
import '../../src/layout.css';
import '../../src/tasks/workspace.css';

const pause = (ms:number) => new Promise(resolve => setTimeout(resolve, ms));
const sizes = [[1920,1080],[1920,1200],[1536,864],[1536,960],[1280,720],[1280,800],
  [1366,650],[1440,810],[1024,576],[1024,640],[900,600],[800,500],[600,480]];
const parameters = new URLSearchParams(location.search);
const clips=['16x9','4x3','9x16'];
const single=parameters.has('single');
const mainOnly=parameters.get('lanes')==='main';
const project:Project = {id:'layout-fixture',name:'隔离测试',source_dir:null,revision:1,updated_at:'',
  duration_ms:7_200_000,fixed_tracks:['scene','posture','category'],
  videos:clips.map((name,index)=>({id:name,name,relative_path:name+'.mp4',duration_ms:2_400_000,start_ms:index*2_400_000,end_ms:(index+1)*2_400_000,url:'',thumbnail_url:null})),
  annotations:Object.fromEntries(['scene','posture','category'].map(axis=>[axis,Array.from({length:mainOnly?1:12},(_,lane)=>({id:axis+lane,label:'测试',start_ms:0,end_ms:3_600_000,lane}))]))};

function Fixture(){
  const [time,setTime]=useState(0),[speed,setSpeed]=useState(1),[clip,setClip]=useState(0),[selected,setSelected]=useState<string|null>(null),[track,setTrack]=useState<Track>('posture');
  const side=useRef<HTMLElement>(null),editor=useRef<HTMLDivElement>(null),video=useRef<HTMLVideoElement>(null);
  useEffect(()=>{if(selected)revealInPanel(side.current,editor.current);},[selected]);
  function seek(value:number){video.current?.pause();setTime(value);setClip(Math.min(2,Math.floor(value/2_400_000)));}
  function next(){seek(((clip+1)%3)*2_400_000);}
  return <div className="app-shell video-workspace" data-task-review={parameters.get('review')??'false'}>
    <header className="topbar"><strong>DataMark</strong><h1 className="compact-project-name">隔离布局测试 · 长项目名称不会撑开工作台</h1></header>
    <main className="editor-grid"><section className="left-column">
      <div className="preview-panel"><div className="panel-heading"><h2>视频预览</h2></div>
        <VideoStage><video ref={video} key={`${clip}-${!single&&speed>10}`} src={`./.tmp/${clips[clip]}.mp4?fast=${!single&&speed>10}`} onLoadedMetadata={event=>applyPlaybackRate(event.currentTarget,playbackProfile(speed,true,!single).nativeRate)} muted playsInline preload="auto" aria-label="隔离测试视频"/></VideoStage>
        <div className="player-controls"><div className="play-controls"><button onClick={()=>seek(((clip+2)%3)*2_400_000)}>上一段</button><button className="play-button" onClick={()=>void video.current?.play()}>▶</button><button data-next onClick={next}>下一段</button><span className="player-time">00:00:00.000 / 02:00:00.000</span></div>
          <div className="play-options"><PlaybackSpeed value={speed} maximum={single?10:100} onChange={value=>{video.current?.pause();setSpeed(value);if(video.current)applyPlaybackRate(video.current,playbackProfile(value,true,!single).nativeRate);}}/><button className="icon-button">♫</button><button className="icon-button">⛶</button></div></div>
      </div>
      <Timeline project={project} currentTime={time} activeTrack={track} selectedId={selected} onSeek={seek} onTrackSelect={setTrack} onSegmentSelect={(axis,segment,value)=>{setTrack(axis);setSelected(segment.id);seek(value??segment.start_ms);}}
        anchorControls={<><button>添加锚点</button><button>选中区间 00:00:00</button><button>清空</button></>}/>
    </section><aside className="workspace-sidebar" ref={side}>
      <section className="sidebar-module"><div className="panel-heading">项目管理</div><div className="module-content">隔离测试项目</div></section>
      <section className="sidebar-module"><div className="panel-heading">长内容及内部滚动测试</div><div className="module-content output-actions">{Array.from({length:40},(_,i)=><button className="secondary" key={i}>操作 {i+1} · 不得撑大视频所在行</button>)}</div></section>
      {parameters.get('review')!=='true'&&<section className="sidebar-module annotation-editor"><div className="panel-heading">编辑标注</div>{selected&&<div ref={editor} className="module-content segment-editor"><label>编辑已选区间<input defaultValue="测试标注"/></label><p>只能滚动右侧面板，视频和时间轴位置不变</p></div>}</section>}
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
  check(Math.abs(timeline.bottom-rect('.editor-grid').bottom)<1,'timeline does not reach workspace bottom');
  check(timeline.top-preview.bottom<=12,'blank between preview and timeline');
  check(video.height>=65 && video.width>=35,'video collapsed');
  const media=get('video') as HTMLVideoElement,frame=rect('.video-frame'),slot=rect('.video-slot');
  check(media.readyState>=2&&media.videoWidth>0,'real video not decoded');
  check(Math.abs(frame.width/frame.height-media.videoWidth/media.videoHeight)<0.01,'preview frame does not match video aspect');
  check(Math.abs(frame.width-video.width)<1&&Math.abs(frame.height-video.height)<1,'video does not fill frame');
  check(frame.width<=slot.width+1&&frame.height<=slot.height+1,'frame exceeds available slot');
  check(Math.abs(frame.width-slot.width)<1||Math.abs(frame.height-slot.height)<1,'video is smaller than available space');
  check(controls.bottom<=preview.bottom+1 && controls.top>=video.bottom-1,'player controls overlap video or clipped');
  for(const button of get('.player-controls').querySelectorAll('button')){
    const r=button.getBoundingClientRect();
    check(r.left>=controls.left-1 && r.right<=controls.right+1 && r.bottom<=controls.bottom+1,'clipped player button');
  }
  const contentHeight=rect('.tl-tracks-content').height;
  check(tracks.clientHeight>=34,'annotation lanes collapsed');
  if(mainOnly)check(tracks.clientHeight<=Math.ceil(contentHeight)+1,'unused timeline height steals video space');
  else check(tracks.scrollHeight>tracks.clientHeight,'annotation lanes not scrollable');
  const top=rect('.tl-ruler').top;
  tracks.scrollTop=80;side.scrollTop=100;
  check((contentHeight<=tracks.clientHeight+1 || tracks.scrollTop>0) && side.scrollTop>0,'panel cannot scroll');
  check(rect('.tl-ruler').top===top && app.scrollTop===0,'panel scroll moved ruler or whole page');
  tracks.scrollTop=0;side.scrollTop=0;
  const horizontal=get('.tl-scroll');horizontal.scrollLeft=200;
  check(horizontal.scrollLeft>0,'timeline cannot scroll horizontally');
  horizontal.scrollLeft=0;
  return {viewport:[innerWidth,innerHeight],review:parameters.get('review'),lanes:mainOnly?'main':'many',gap:timeline.top-preview.bottom,videoHeight:video.height,timelineHeight:timeline.height,errors};
}

async function interactions(){
  const get=(selector:string)=>document.querySelector<HTMLElement>(selector)!;
  const waitVideo=async()=>{
    for(let i=0;i<100;i++){
      const video=get('video') as HTMLVideoElement;
      if(video.readyState>=2){await pause(40);return;}
      await pause(30);
    }
    throw new Error('Video fixture failed to decode');
  };
  await waitVideo();
  const result=measure();
  const top=get('.preview-panel').getBoundingClientRect().top;
  const ruler=get('.tl-ruler').getBoundingClientRect().top;
  // Exercise real timeline selection and the same bounded reveal used by App.
  const segment=get('.tl-segment:not(.tl-segment-selected)');
  segment.click();await pause(50);
  if(segment.getAttribute('aria-pressed')!=='true')result.errors.push('timeline selection not applied');
  if(parameters.get('review')==='true'){
    // App.tsx does not render the annotation editor in review mode.
    if(get('.annotation-editor')||get('.workspace-sidebar').scrollTop!==0)result.errors.push('review selection opened editor or scrolled sidebar');
  }else if(!get('.segment-editor')||get('.workspace-sidebar').scrollTop<=0)result.errors.push('selection did not reveal editor');
  if(get('.preview-panel').getBoundingClientRect().top!==top||get('.tl-ruler').getBoundingClientRect().top!==ruler||get('.app-shell').scrollTop!==0||get('.editor-grid').scrollTop!==0)result.errors.push('selection moved workspace');
  for(let i=0;i<3;i++){
    get('[data-next]').click();await pause(30);await waitVideo();
    const video=get('video') as HTMLVideoElement;
    await video.play();await pause(30);video.pause();video.currentTime=0.5;await waitVideo();
    result.errors.push(...measure().errors.map(error=>clips[(i+1)%3]+': '+error));
  }
  const speed=Array.from(document.querySelectorAll<HTMLButtonElement>('.playback-speed button')).find(button=>button.textContent==='50×');
  if(!speed)throw new Error('Missing fast-preview control');
  speed.click();await pause(30);await waitVideo();
  result.errors.push(...measure().errors.map(error=>'fast preview: '+error));
  return result;
}

if(parameters.has('fixture')){
  createRoot(document.getElementById('root')!).render(<Fixture/>);
  Object.assign(window,{measureLayout:measure,exerciseLayout:interactions});
}else{
  const results:ReturnType<typeof measure>[]=[];
  const run=async()=>{
    const frame=document.createElement('iframe');frame.style.border='0';document.body.append(frame);
    // Reuse each mounted component while resizing to exercise ResizeObserver.
    for(const review of ['false','true']){
      for(const lanes of ['main','many']){
      frame.src=`?fixture=1&review=${review}&lanes=${lanes}`;
      await new Promise<void>(resolve=>frame.onload=()=>resolve());await pause(200);
      for(const [width,height] of sizes){
        frame.style.width=width+'px';frame.style.height=height+'px';await pause(80);
        results.push(await (frame.contentWindow as unknown as {exerciseLayout:()=>Promise<ReturnType<typeof measure>>}).exerciseLayout());
      }
      }
    }
    frame.remove();
    const summary=document.createElement('h1');summary.textContent=`${results.every(r=>!r.errors.length)?'PASS':'FAIL'}: ${results.length} 布局场景；${results.filter(r=>r.errors.length).length} 失败`;document.body.append(summary);
    const output=document.createElement('pre');output.id='layout-results';output.textContent=JSON.stringify(results);document.body.append(output);
  };
  run().catch(error=>{document.body.dataset.layoutError=String(error);});
}
