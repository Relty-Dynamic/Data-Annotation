import { useEffect, useMemo, useRef, useState } from 'react';
import {authFetch, type Account} from './auth';
import {apiUrl, separateApiOrigin} from './apiOrigin.ts';
import AdminPanel from './AdminPanel';
import PasswordDialog from './PasswordDialog';
import HistoryPanel from './HistoryPanel';
import { Upload, FolderOpen, Download, Save, Play, Pause, SkipBack, SkipForward, Plus, X, Trash2, Check, Film, Layers3, ChevronDown, RotateCcw, RotateCw, Keyboard, Maximize, Minimize, Volume2, VolumeX, Circle, ArrowRight, LoaderCircle, Bookmark, ScanLine, Pencil, Settings, History } from 'lucide-react';
import Timeline from './Timeline';
import TimelineImportDialog from './TimelineImportDialog';
import type { ExternalTimeline } from './externalTimeline';
import './comparison.css';
import PlaybackSpeed from './PlaybackSpeed';
import DeleteProjectDialog from './DeleteProjectDialog';
import RenameProjectDialog from './RenameProjectDialog';
import SeekSettingsDialog from './SeekSettingsDialog';
import { readSeekSettings, writeSeekSettings, wheelSeekDelta } from './seekSettings';
import type { SeekSettings } from './seekSettings';
import { projectNameError, projectDownloadName } from './projectNames';
import { useWorkspaceGestures } from './useWorkspaceGestures';
import { usePreview, rememberPreparedPreviews, forgetProjectPreviews } from './usePreview';
import { usePreparation } from './usePreparation';
import { usePreloadNext } from './usePreloadNext';
import { useMediaBuffering } from './useMediaBuffering';
import PreparePanel from './PreparePanel';
import { sessionAssetUrl } from './sessionAssets';
import { readProjectPreference, readRestorableProjectPreference, writeProjectPreference } from './projectPreferences';
import { advanceVideoTime, playbackProfile, seekMediaTime, sourceTime } from './playback';
import { formatTime, parseTime, switchCoveredState, applyAnnotationRange, deleteAnnotation, uncoveredSpans, SCENE_LABELS, POSTURE_LABELS, CATEGORY_LABELS, TRACKS, projectTracks, customTrack, trackName, isEventTrack, isExclusiveTrack, locateVideo, annotationsEqual, normalizeStateSeams, anchorVideoTime, restoreVideoTime, isSegmentDraftDirty } from './domain';
import { POSTURE_SHORTCUTS, postureShortcutLabel } from './postureShortcuts';
import type { CustomTrack, Project, Segment, Track, VideoTimeAnchor } from './domain';
import CustomTrackDialog from './CustomTrackDialog';
import DeleteCustomTrackDialog from './DeleteCustomTrackDialog';
type ImportMode = 'new' | 'supplement' | 'relink';
type ComposerDraft = {label:string; start:string; end:string; kind:'point'|'interval'};
type SupplementResponse = {project:Project; added_count:number; skipped_names:string[]};
type RelinkResponse = {project:Project; relinked_count:number; removed_copies:number; warnings?:string[]};
type NasEntry = {name:string; path:string; kind:'directory'|'file'; size?:number};
type NasListing = {root:string; path:string; parent:string|null; entries:NasEntry[]; page:number; has_more:boolean};
const oldBackendMessage = '后台仍是旧版本，请关闭所有平台网页，等待约15秒后重新启动。';
type SupplementContext = {project:Project; cursor:VideoTimeAnchor|null; composer:ComposerDraft|null; start:VideoTimeAnchor|null; end:VideoTimeAnchor|null};
const sceneLabels = SCENE_LABELS;

const names = {scene:'场景',posture:'姿势',category:'大类',habit:'习惯'};
async function api<T>(url:string,init?:RequestInit):Promise<T>{const r=await authFetch(url,init);if(!r.ok){let s='操作失败，请重试';try{const j=await r.json();s=typeof j.detail==='string'?j.detail:JSON.stringify(j.detail);}catch{s=`请求失败 (${r.status})`;}throw new Error(s);}return r.json();}
function json(method:string,body:unknown){return {method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)};}
async function requireSourceCapabilities(supplement=false,nativePicker=false){
 const health=await api<{capabilities?:string[]}>('/api/health',{cache:'no-store'});
 const required=['source-local-cache','compact-local-playback','four-axis-annotations',...(supplement?['supplement-import']:[]),...(nativePicker?['native-file-picker']:[])];
 if(!required.every(capability=>health.capabilities?.includes(capability)))throw new Error(oldBackendMessage);
}
async function requireProjectNaming(){
 const health=await api<{capabilities?:string[]}>('/api/health',{cache:'no-store'});
 if(!health.capabilities?.includes('project-naming'))throw new Error(oldBackendMessage);
}
export default function App({user,onLogout:logoutNow}:{user:Account;onLogout:()=>void}){
 const sharedServer=window.location.protocol==='https:';
 const composerKey=(id:string)=>`datamark-user-${user.id}-composer-${id}`;
 const lastProjectKey=`datamark-user-${user.id}-last-project`;
 const previewsClearedKey=`datamark-user-${user.id}-previews-cleared`;
 useWorkspaceGestures();
 const [project,setProject]=useState<Project|null>(null), projectRef=useRef<Project|null>(null);
 const [adminOpen,setAdminOpen]=useState(false);
 const [passwordOpen,setPasswordOpen]=useState(false);
 const [auditOpen,setAuditOpen]=useState(false);
 const [projects,setProjects]=useState<Project[]>([]),[activeTrack,setActiveTrack]=useState<Track>('scene');
 const [customTrackOpen,setCustomTrackOpen]=useState(false);
 const [deleteCustomTrack,setDeleteCustomTrack]=useState<CustomTrack|null>(null),[deletingCustomTrack,setDeletingCustomTrack]=useState(false),[deleteCustomError,setDeleteCustomError]=useState('');
 const [trackRevealVersion,setTrackRevealVersion]=useState(0);
 const [time,setTime]=useState(0),timeRef=useRef(0),[videoId,setVideoId]=useState('');
 const [playing,setPlaying]=useState(false),wantsPlay=useRef(false),[rate,setRate]=useState(1),[muted,setMuted]=useState(true);
 const [mediaError,setMediaError]=useState(''),[mediaVersion,setMediaVersion]=useState(0);
 const [showPreviewWait,setShowPreviewWait]=useState(false),[scrubbing,setScrubbing]=useState(false);
 const scrubbingRef=useRef(false);
 const videoRef=useRef<HTMLVideoElement>(null), pendingSeek=useRef<number|null>(0);
 const [prepareOpen,setPrepareOpen]=useState(false),[sessionEntered,setSessionEntered]=useState(false);
 const enterAfterSkip=useRef<string|null>(null);
 const restoreOnReady=useRef<string|null>(null);
 const [importOpen,setImportOpen]=useState(false),[importMode,setImportMode]=useState<ImportMode>('new'),[importError,setImportError]=useState(''),[path,setPath]=useState(''),[busy,setBusy]=useState('');
 const [nasPickerKind,setNasPickerKind]=useState<'files'|'directory'|null>(null),[nasListing,setNasListing]=useState<NasListing|null>(null),[nasSelected,setNasSelected]=useState<string[]>([]),[nasLoading,setNasLoading]=useState(false);
 const busyRef=useRef(false),importDialogRef=useRef<HTMLElement>(null),importTriggerRef=useRef<HTMLElement|null>(null);
 const [newProjectName,setNewProjectName]=useState('');
 const [timelineImportOpen,setTimelineImportOpen]=useState(false);
 const [comparison,setComparison]=useState<ExternalTimeline|null>(null),[comparisonOpen,setComparisonOpen]=useState(false);
 const comparisonRef=useRef<HTMLElement>(null),comparisonTrigger=useRef<HTMLElement|null>(null);
 function closeComparison(){pausePlayback();setComparisonOpen(false);queueMicrotask(()=>comparisonTrigger.current?.focus());}
 const [settingsOpen,setSettingsOpen]=useState(false),[seekSettings,setSeekSettings]=useState<SeekSettings>(()=>readSeekSettings());
 useEffect(()=>{
  if(!comparisonOpen||timelineImportOpen||settingsOpen||prepareOpen)return;
  comparisonRef.current?.focus();
  const contain=(event:FocusEvent)=>{if(event.target instanceof Node&&!comparisonRef.current?.contains(event.target))comparisonRef.current?.focus();};
  const key=(event:KeyboardEvent)=>{
   if(event.key==='Escape'&&!scrubbingRef.current){event.preventDefault();event.stopPropagation();closeComparison();return;}
   if(event.key!=='Tab')return;
   const controls=Array.from(comparisonRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled),select:not(:disabled),[tabindex="0"]')??[]).filter(el=>el.getClientRects().length);
   const first=controls[0],last=controls.at(-1);if(!first||!last)return;
   if(event.shiftKey&&(document.activeElement===first||document.activeElement===comparisonRef.current)){event.preventDefault();last.focus();}
   else if(!event.shiftKey&&(document.activeElement===last||document.activeElement===comparisonRef.current)){event.preventDefault();first.focus();}
  };
  document.addEventListener('focusin',contain,true);document.addEventListener('keydown',key);
  return()=>{document.removeEventListener('focusin',contain,true);document.removeEventListener('keydown',key);};
 },[comparisonOpen,timelineImportOpen,settingsOpen,prepareOpen]);
 function saveSeekSettings(value:SeekSettings){setSeekSettings(value);const saved=writeSeekSettings(value);setSettingsOpen(false);setNotice(saved?'进度快捷键设置已保存': '设置已在本次页面生效，但浏览器无法保存；下次打开时可能恢复默认。');}
 const [renameProject,setRenameProject]=useState<Project|null>(null),[renaming,setRenaming]=useState(false),[renameError,setRenameError]=useState('');
 const [deleteProject,setDeleteProject]=useState<Project|null>(null),[deleting,setDeleting]=useState(false),[deleteError,setDeleteError]=useState('');
 const [clearedPreviewId,setClearedPreviewId]=useState('');
 const [error,setError]=useState(''),[notice,setNotice]=useState(''),[saveState,setSaveState]=useState('已保存到平台');
 const [saveFailed,setSaveFailed]=useState(false),saveFailedRef=useRef(false),pendingSaves=useRef(0),saveQueue=useRef<Promise<void>>(Promise.resolve()),serverRevision=useRef(0);
 const [selectedId,setSelectedId]=useState<string|null>(null),[label,setLabel]=useState(''),[startText,setStartText]=useState(''),[endText,setEndText]=useState(''),[manual,setManual]=useState(false);
 const [anchors,setAnchors]=useState<number[]>([]),[selectedRange,setSelectedRange]=useState<{start_ms:number;end_ms:number}|null>(null);
 const [categoryMode,setCategoryMode]=useState<'state'|'range'>('state');
 const [behaviorKind,setBehaviorKind]=useState<'point'|'interval'>('interval');
 const [history,setHistory]=useState<Project['annotations'][]>([]),[redoHistory,setRedoHistory]=useState<Project['annotations'][]>([]),[help,setHelp]=useState(false);
 const historyRef=useRef<Project['annotations'][]>([]),redoRef=useRef<Project['annotations'][]>([]);
 const videoIdRef=useRef(''),rateRef=useRef(rate),transportKeys=useRef(new Set<string>());
 rateRef.current=rate;
 async function toggleWorkspaceFullscreen(){
  try{
   if(document.fullscreenElement)await document.exitFullscreen();
   else if(document.fullscreenEnabled)await document.documentElement.requestFullscreen({navigationUI:'hide'});
   else throw new Error('unsupported');
  }catch{setError('浏览器未能进入工作台全屏，请检查浏览器权限或改用 F11。');}
 }
 function updateHistory(past:Project['annotations'][],future:Project['annotations'][]){historyRef.current=past;redoRef.current=future;setHistory(past);setRedoHistory(future);}
 function activateVideo(id:string){videoIdRef.current=id;setVideoId(id);}
 function pausePlayback(){wantsPlay.current=false;videoRef.current?.pause();syncNativeTime();setPlaying(false);}
 function playNative(v:HTMLVideoElement){v.play().catch(()=>{if(videoRef.current!==v||!wantsPlay.current)return;pausePlayback();setError('视频尚未就绪，请稍后点击播放。');});}

 const selected=project?.annotations[activeTrack]?.find(s=>s.id===selectedId);
 const eventTrack=!!project&&isEventTrack(project,activeTrack);
 const hasCategoryDraft=!manual&&activeTrack==='category'&&categoryMode==='range'&&Boolean(label.trim()||(!selectedRange&&(startText.trim()||endText.trim())));
 const hasEventDraft=!manual&&eventTrack&&activeTrack!=='habit'&&Boolean(label.trim()||(!selectedRange&&(startText.trim()||endText.trim())));
 const hasPendingEdit=hasCategoryDraft||hasEventDraft||(manual&&isSegmentDraftDirty(selected,{label,start:startText,end:endText,kind:behaviorKind}));
 const preparation=usePreparation(clearedPreviewId===project?.id?undefined:project?.id,project?.playback_generation??0);
 useEffect(()=>{
  if(enterAfterSkip.current && enterAfterSkip.current!==project?.id)enterAfterSkip.current=null;
  if(restoreOnReady.current && restoreOnReady.current!==project?.id)restoreOnReady.current=null;
  if(restoreOnReady.current===project?.id&&(preparation.error||['partial','paused'].includes(preparation.status?.state??''))){
   restoreOnReady.current=null;setPrepareOpen(true);
  }
  if((enterAfterSkip.current===project?.id||restoreOnReady.current===project?.id)&&preparation.status?.state==='ready'&&preparation.manifest&&!preparation.error){
   enterAfterSkip.current=null;restoreOnReady.current=null;setSessionEntered(true);setPrepareOpen(false);
  }
 },[project?.id,preparation.status?.state,preparation.manifest,preparation.error]);
 const playbackVideos=useMemo(()=>project?.videos.map(video=>{const ready=preparation.manifest?.videos.find(item=>item.id===video.id);return ready?{...video,url:ready.url,thumbnail_url:ready.thumbnail_url}:video;})??[],[project?.videos,preparation.manifest]);
 const playbackProject=useMemo(()=>project?{...project,videos:playbackVideos}:null,[project,playbackVideos]);
 const activeVideo=playbackVideos.find(video=>video.id===videoId);
 useEffect(()=>{if(!project||!preparation.manifest)return;rememberPreparedPreviews(project.id,preparation.manifest.videos);},[project?.id,preparation.manifest]);
 const profile=playbackProfile(rate,playing);
 const preview=usePreview(project?.id,videoId,mediaVersion,sessionEntered&&!prepareOpen&&!deleting&&!project?.deletion_pending,profile.fast,true);
 const nextVideo=playbackVideos[playbackVideos.findIndex(video=>video.id===videoId)+1];
 usePreloadNext(sessionEntered&&!prepareOpen&&preparation.status?.state==='ready'&&preview.state==='ready'&&nextVideo?preparation.manifest?.videos.find(video=>video.id===nextVideo.id)?.[profile.fast?'fast_url':'url']:undefined);
 const previewPending=!!activeVideo&&preview.state!=='ready'&&preview.state!=='error';
 const previewError=preview.state==='error'?preview.detail||'无法生成视频预览，请重试。':mediaError;
 useEffect(()=>{setShowPreviewWait(false);if(!previewPending)return;const timer=setTimeout(()=>setShowPreviewWait(true),350);return()=>clearTimeout(timer);},[project?.id,videoId,previewPending]);
 useEffect(()=>{if(preview.state==='error')pausePlayback();},[preview.state]);
 const showBuffering=useMediaBuffering(videoRef,wantsPlay,!!activeVideo&&!previewPending&&!previewError&&!scrubbing,`${project?.id}/${videoId}/${mediaVersion}/${profile.fast}`);
 const count=project?Object.values(project.annotations).reduce((n,a)=>n+a.length,0):0;
 const running=project?.annotations.habit.filter(s=>s.end_ms===null)??[];
 const spans=project?.recording_runs??project?.videos??[];
 const fixedLabels=activeTrack==='scene'?sceneLabels:activeTrack==='posture'?POSTURE_LABELS:activeTrack==='category'?CATEGORY_LABELS:project?customTrack(project,activeTrack)?.labels??[]:[];
 const coverageIssues=useMemo(()=>project?(['scene','posture'] as const).map(track=>({track,count:uncoveredSpans(project.annotations[track],project.recording_runs??project.videos).length})).filter(item=>item.count>0):[],[project]);
 const currentStates=project?.annotations[activeTrack]?.filter(s=>s.start_ms<=time&&(s.end_ms===null||time<s.end_ms))??[];
 const refresh=()=>api<Project[]>('/api/projects').then(setProjects).catch(e=>setError(e.message));
 useEffect(()=>{requireSourceCapabilities().then(()=>api<Project[]>('/api/projects')).then(ps=>{setProjects(ps);const last=readRestorableProjectPreference(lastProjectKey,previewsClearedKey);if(last&&ps.some(p=>p.id===last))api<Project>(`/api/projects/${last}`).then(p=>{if(!projectRef.current)adopt(p,true);}).catch(e=>setError(e.message));}).catch(e=>setError(e.message));},[]);
 useEffect(()=>{if(project&&activeTrack==='habit'&&!manual){try{localStorage.setItem(composerKey(project.id),JSON.stringify({label,start:startText,end:endText,kind:behaviorKind}));}catch{}}},[project?.id,activeTrack,manual,label,startText,endText,behaviorKind]);
 useEffect(()=>{if(notice&&!prepareOpen&&!importOpen&&!busy){const t=setTimeout(()=>setNotice(''),9000);return()=>clearTimeout(t);}},[notice,prepareOpen,importOpen,busy]);
 useEffect(()=>{const warn=(e:BeforeUnloadEvent)=>{if(pendingSaves.current||saveFailedRef.current||busyRef.current||hasPendingEdit){e.preventDefault();e.returnValue='';}};window.addEventListener('beforeunload',warn);return()=>window.removeEventListener('beforeunload',warn);},[hasPendingEdit]);
 function resetEditorFields(){setSelectedId(null);setLabel('');setStartText('');setEndText('');setManual(false);setBehaviorKind('interval');}
 function clearEditor(){resetEditorFields();if(selectedRange){setStartText(formatTime(selectedRange.start_ms));setEndText(formatTime(selectedRange.end_ms));}else if(activeTrack==='habit'&&project)restoreComposer(project.id);}
 function clearComposer(){if(project){try{localStorage.removeItem(composerKey(project.id));}catch{}}resetEditorFields();}
 function restoreComposer(id:string){try{const d=JSON.parse(localStorage.getItem(composerKey(id))??'null');if(d){setLabel(d.label??'');setStartText(d.start??'');setEndText(d.end??'');setBehaviorKind(d.kind==='point'?'point':'interval');}}catch{}}
 function selectTrack(t:Track){if(t===activeTrack){setTrackRevealVersion(value=>value+1);return;}if(hasPendingEdit){setError('请先保存或取消正在编辑的标注，再切换类型；当前输入已保留。');return;}setError('');setTrackRevealVersion(value=>value+1);setActiveTrack(t);resetEditorFields();if(selectedRange){setStartText(formatTime(selectedRange.start_ms));setEndText(formatTime(selectedRange.end_ms));if(t==='category')setCategoryMode('range');}else if(t==='habit'&&project)restoreComposer(project.id);}
 function setCurrent(ms:number){timeRef.current=ms;setTime(ms);}
 function seek(ms:number){
  const p=projectRef.current;if(!p)return;
  if(wantsPlay.current||videoRef.current?.paused===false)pausePlayback();
  const t=Math.max(0,Math.min(p.duration_ms,Math.round(ms)));
  const ending=p.videos.find(video=>video.end_ms===t);
  const v=ending&&!p.videos.some(video=>video.start_ms===t)?ending:locateVideo(p.videos,t);if(!v)return;
  const snapped=Math.max(v.start_ms,t);setCurrent(snapped);
  if(snapped!==t&&!scrubbingRef.current&&!p.continuity_bridges?.some(bridge=>t>=bridge.start_ms&&t<bridge.end_ms))setNotice('此处为录制间隔，已定位到下一段视频');
  const local=(snapped-v.start_ms)/1000;
  // Dragging publishes the cursor only; releasing commits one decoder seek.
  if(scrubbingRef.current)return;
  if(v.id!==videoIdRef.current){activateVideo(v.id);setMediaError('');}
  // Rapid direction changes can return to the mounted video before React replaces it.
  const player=videoRef.current;
  if(!player||player.dataset.videoId!==v.id||player.readyState===0){pendingSeek.current=local;}
  else{pendingSeek.current=null;seekMediaTime(player,local,Number(player.dataset.timeScale)||1);}
 }
 function beginScrub(){pausePlayback();scrubbingRef.current=true;setScrubbing(true);}
 function endScrub(_cancelled:boolean,targetMs:number){
  if(!scrubbingRef.current)return;
  scrubbingRef.current=false;setScrubbing(false);seek(targetMs);
 }
 function moveCursor(delta:number){const p=projectRef.current;if(!p||scrubbingRef.current)return;pausePlayback();seek(advanceVideoTime(p.videos,timeRef.current,delta));}
 function toggle(){
  const v=videoRef.current,p=projectRef.current;if(!v||!p||scrubbingRef.current)return;
  if(wantsPlay.current){pausePlayback();return;}
  if(timeRef.current>=p.duration_ms)seek(0);
  wantsPlay.current=true;setPlaying(true);
  if(rateRef.current>10)return; // Render the compressed-media player before starting.
  if(v.readyState>0&&v.dataset.videoId===videoIdRef.current)playNative(v);
 }
 function onReady(){
  const v=videoRef.current;if(prepareOpen||scrubbingRef.current||!v||v.readyState===0||v.dataset.videoId!==videoIdRef.current)return;
  setMediaError('');
  const scale=Number(v.dataset.timeScale)||1;
  const source=projectRef.current?.videos.find(item=>item.id===videoIdRef.current);if(!source)return;
  v.playbackRate=wantsPlay.current?rateRef.current/scale:1;
  const local=pendingSeek.current??Math.max(0,(timeRef.current-source.start_ms)/1000);
  pendingSeek.current=null;seekMediaTime(v,local,scale);
  
  if(wantsPlay.current)playNative(v);
 }
 function syncNativeTime(){
  const v=videoRef.current,p=projectRef.current;
  if(scrubbingRef.current||!v||!p||v.readyState===0||v.seeking||pendingSeek.current!==null||v.dataset.videoId!==videoIdRef.current)return;
  const source=p.videos.find(item=>item.id===videoIdRef.current);if(!source)return;
  const current=sourceTime(v.currentTime,Number(v.dataset.timeScale)||1,source);
  setCurrent(current);
  // Some AVI audio tracks outlast the video. Follow the actual video boundary.
  if(wantsPlay.current&&current>=source.end_ms)ended();
 }
 function ended(){
  if(!wantsPlay.current||scrubbingRef.current||!project||!activeVideo||pendingSeek.current!==null||videoRef.current?.dataset.videoId!==videoIdRef.current)return;
  const i=project.videos.findIndex(v=>v.id===videoIdRef.current);
  if(i<project.videos.length-1){const next=project.videos[i+1];pendingSeek.current=0;setCurrent(next.start_ms);activateVideo(next.id);}else{setCurrent(project.duration_ms);pausePlayback();}
 }
 useEffect(()=>{
  const v=videoRef.current;if(!v)return;
  v.playbackRate=profile.nativeRate;
  if(wantsPlay.current&&v.readyState>0)playNative(v);
 },[rate,profile.fast,playing]);
 useEffect(()=>{
  if(!playing)return;
  let frame=0,lastPublish=0;
  const tick=(now:number)=>{
   const v=videoRef.current;
   if(!wantsPlay.current)return;
   if(v&&!v.paused&&now-lastPublish>=33){syncNativeTime();lastPublish=now;}
   frame=requestAnimationFrame(tick);
  };
  frame=requestAnimationFrame(tick);return()=>cancelAnimationFrame(frame);
 },[playing,activeVideo,profile.fast]);
 function persist(p:Project,annotations:Project['annotations']){pendingSaves.current++;setSaveState('正在保存…');saveQueue.current=saveQueue.current.then(async()=>{if(saveFailedRef.current)throw new Error('草稿尚未保存，请先重试保存。');const saved=await api<Project>(`/api/projects/${p.id}/draft`,json('PUT',{annotations,expected_revision:serverRevision.current}));if(projectRef.current?.id===p.id){serverRevision.current=saved.revision;const current=projectRef.current;projectRef.current={...current,revision:saved.revision,updated_at:saved.updated_at,annotations:current.annotations===annotations?saved.annotations:current.annotations};setProject(projectRef.current);} }).catch((e:Error)=>{if(projectRef.current?.id===p.id){saveFailedRef.current=true;setSaveFailed(true);setError(e.message);}}).finally(()=>{pendingSaves.current--;if(projectRef.current?.id===p.id)setSaveState(saveFailedRef.current?'保存失败，改动保留在此页':pendingSaves.current?'正在保存…':'已保存到平台');});}
 function commit(annotations:Project['annotations'],record=true){const p=projectRef.current;if(!p||p.deletion_pending||busyRef.current)return false;annotations=normalizeStateSeams(annotations,p.continuity_bridges??[],p.videos,p.custom_tracks);if(annotationsEqual(p.annotations,annotations))return true;if(record)updateHistory([...historyRef.current.slice(-49),p.annotations],[]);const next={...p,annotations};projectRef.current=next;setProject(next);persist(next,annotations);return true;}
 async function flush(){await saveQueue.current;if(saveFailedRef.current)throw new Error('请先重试保存平台草稿，再继续操作。');}
 async function onLogout(){
  if(busyRef.current||hasPendingEdit){setError('请先完成或取消当前编辑，再退出登录。');return;}
  if(project&&activeTrack==='habit'&&!manual&&(label||startText||endText)){
   try{localStorage.setItem(composerKey(project.id),JSON.stringify({label,start:startText,end:endText,kind:behaviorKind}));}
   catch{setError('浏览器无法保存当前习惯输入，请先完成或清空后再退出。');return;}
  }
  try{await flush();logoutNow();}catch(error){setError(error instanceof Error?error.message:'草稿尚未保存，暂不能退出登录。');}
 }
 async function retrySave(){await doBusy('正在核对草稿保存状态…',async()=>{await saveQueue.current;const local=projectRef.current;if(!local)return;const remote=await api<Project>(`/api/projects/${local.id}`);if(annotationsEqual(normalizeStateSeams(remote.annotations,remote.continuity_bridges??[],remote.videos,remote.custom_tracks),normalizeStateSeams(local.annotations,remote.continuity_bridges??[],remote.videos,remote.custom_tracks))){serverRevision.current=remote.revision;projectRef.current={...local,name:remote.name,revision:remote.revision,updated_at:remote.updated_at,annotations:remote.annotations};setProject(projectRef.current);saveFailedRef.current=false;setSaveFailed(false);setSaveState('已保存到平台');setNotice('已确认当前标注保存成功');return;}if(remote.revision===serverRevision.current){saveFailedRef.current=false;setSaveFailed(false);persist(local,local.annotations);await saveQueue.current;return;}setSaveState('版本冲突，改动保留在此页');setError('草稿已在其他窗口更新。当前改动仍保留在此页；请点击「保存恢复副本并重新载入」，下载包含本页标注的恢复 JSON 后读取服务器草稿。');});}
 async function recoverAndReload(){await doBusy('正在生成恢复副本并重新载入…',async()=>{await saveQueue.current;const local=projectRef.current;if(!local)return;const remote=await api<Project>(`/api/projects/${local.id}`);const content=JSON.stringify(local,null,2);const blob=new Blob([content],{type:'application/json;charset=utf-8'});const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download=`${local.name.replace(/[<>:"/\\|?*]/g,'_')}-未保存草稿-恢复副本-${Date.now()}.json`;document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),10000);adopt(remote);setNotice('恢复副本已开始下载，已重新载入服务器草稿。原有未保存改动保留在恢复 JSON 中。');});}
 function adopt(p:Project,restore=false){setComparison(null);setComparisonOpen(false);setTimelineImportOpen(false);setClearedPreviewId('');p={...p,annotations:{...p.annotations,category:p.annotations.category??[]}};if(!projectTracks(p).includes(activeTrack))setActiveTrack('scene');setAnchors([]);setSelectedRange(null);setCategoryMode('state');restoreOnReady.current=restore?p.id:null;setSessionEntered(false);setPrepareOpen(!restore);if(!restore)preparation.refresh();scrubbingRef.current=false;setScrubbing(false);writeProjectPreference(previewsClearedKey,null);writeProjectPreference(lastProjectKey,p.id);setMediaVersion(v=>v+1);wantsPlay.current=false;setPlaying(false);videoRef.current?.pause();serverRevision.current=p.revision;projectRef.current=p;setProject(p);setCurrent(0);pendingSeek.current=0;activateVideo(p.videos[0]?.id??'');setMediaError('');resetEditorFields();updateHistory([],[]);saveFailedRef.current=false;setSaveFailed(false);setSaveState('已保存到平台');setError('');if(activeTrack==='habit')restoreComposer(p.id);if((p as Project & {warnings?:string[]}).warnings?.length)setNotice((p as Project & {warnings:string[]}).warnings.join('；'));}
 async function doBusy(message:string,fn:()=>Promise<void>,inImport=false){if(busyRef.current)return;busyRef.current=true;setBusy(message);setError('');if(inImport)setImportError('');try{await fn();}catch(e){if(inImport)setImportError((e as Error).message);else setError((e as Error).message);}finally{busyRef.current=false;setBusy('');}}
 async function finishImport(p:Project){const requestedName=newProjectName.trim();adopt(p);setImportOpen(false);if(requestedName&&p.name!==requestedName)setNotice(`这些素材已有项目，已打开「${p.name}」。可点击名称旁的铅笔重命名。`);await refresh();}
 async function openPreparation(){
  const p=projectRef.current;if(!p||deleting)return;pausePlayback();
  restoreOnReady.current=null;
  const current=p.videos.find(video=>video.id===videoIdRef.current);if(current)pendingSeek.current=Math.max(0,(timeRef.current-current.start_ms)/1000);
  try{
   const fresh=await api<Project>(`/api/projects/${p.id}`);if(projectRef.current?.id!==p.id)return;
   if(clearedPreviewId===p.id||(projectRef.current.playback_generation??0)!==(fresh.playback_generation??0)){
    const next={...projectRef.current,playback_generation:fresh.playback_generation};projectRef.current=next;setProject(next);setClearedPreviewId('');
   }else if(preparation.status?.state!=='ready'||preparation.error)void preparation.start(p.id,!!preparation.status?.failed);
  }catch(e){setError((e as Error).message);return;}
  setSessionEntered(false);setPrepareOpen(true);
 }
 async function beginImport(mode:ImportMode){
  if(busyRef.current)return;
  if(mode==='new'&&hasPendingEdit){setError('请先保存或取消正在编辑的标注，再新建项目；当前输入已保留。');return;}
  if(mode!=='new'&&(!projectRef.current||projectRef.current.deletion_pending))return;
  if(mode==='supplement'&&(manual||hasCategoryDraft)){setError('请先保存或取消右侧正在编辑的标注，再补导入视频；当前输入已保留。');return;}
  pausePlayback();importTriggerRef.current=document.activeElement as HTMLElement|null;
  await doBusy('正在检查导入功能…',async()=>{
   await requireSourceCapabilities(mode==='supplement');
   setImportMode(mode);setImportError('');setPath('');setNewProjectName('');setNasPickerKind(null);setNasListing(null);setNasSelected([]);setImportOpen(true);
  });
 }
 async function changeSkippedVideos(ids?:string[]){
  if(hasPendingEdit)throw new Error('请先保存或取消正在编辑的标注，再更改跳过的片段。');
  await flush();const p=projectRef.current;if(!p)throw new Error('请先打开项目。');
  const updated=await api<Project>(`/api/projects/${p.id}/session/${ids?'skip-failed':'restore-skipped'}`,json('POST',{
   confirmed:true,expected_revision:serverRevision.current,...(ids?{video_ids:ids}:{})
  }));
  if(projectRef.current?.id!==p.id)return;
  forgetProjectPreviews(p.id);adopt(updated);
  enterAfterSkip.current=ids?p.id:null;
  const first=updated.videos[0];if(first){setCurrent(first.start_ms);pendingSeek.current=0;activateVideo(first.id);}
  setNotice(ids?`已跳过 ${ids.length} 段，原视频保留，时间轴对应位置留空。`:'已恢复跳过的片段，正在重新检查播放素材。');
 }
 function captureSupplement():SupplementContext{
  const current=projectRef.current;if(!current)throw new Error('请先打开要补导入的项目。');
  let composer:ComposerDraft|null=null;
  if(activeTrack==='habit'&&!manual)composer={label,start:startText,end:endText,kind:behaviorKind};
  else{try{const stored=JSON.parse(localStorage.getItem(composerKey(current.id))??'null');if(stored)composer={label:stored.label??'',start:stored.start??'',end:stored.end??'',kind:stored.kind==='point'?'point':'interval'};}catch{throw new Error('无法读取当前行为输入草稿。请先打开行为面板，检查并保存输入后再补导入。');}}
  const capture=(value:string,edge:'start'|'end')=>{
   if(!value.trim())return null;
   const parsed=parseTime(value),anchor=parsed===null?null:anchorVideoTime(current.videos,parsed,edge);
   if(!anchor)throw new Error(`行为输入中尚未保存的${edge==='start'?'开始':'结束'}时间无效或位于录制空档。请先修正或清空该时间，再补导入；输入内容已保留。`);
   return anchor;
  };
  const active=current.videos.find(video=>video.id===videoIdRef.current);
  return {project:current,cursor:active?{videoId:active.id,offsetMs:timeRef.current-active.start_ms}:null,composer,start:composer?capture(composer.start,'start'):null,end:composer?capture(composer.end,'end'):null};
 }
 async function finishSupplement(result:SupplementResponse,context:SupplementContext){
  const p=result.project;
  if(p.id!==context.project.id)throw new Error('补导入返回了不同的项目，请重新打开当前项目核对。');
  if(projectRef.current?.id!==p.id)throw new Error('当前项目已关闭，请从项目列表重新打开以查看补导入结果。');
  let composerStorageWarning='';
  if(result.added_count>0){
   let composer=context.composer;
   if(composer){
    const restore=(anchor:VideoTimeAnchor|null,value:string)=>{if(!anchor)return value;const next=restoreVideoTime(p.videos,anchor);if(next===null)throw new Error('补导入后原视频不存在，请重新打开项目核对。');return formatTime(next);};
    composer={...composer,start:restore(context.start,composer.start),end:restore(context.end,composer.end)};
    try{localStorage.setItem(composerKey(p.id),JSON.stringify(composer));}catch{composerStorageWarning='行为输入已保留在本页，但浏览器无法保存输入草稿，请在关闭网页前保存这条标注。';}
   }
   adopt(p);
   if(composer&&activeTrack==='habit'){setLabel(composer.label);setStartText(composer.start);setEndText(composer.end);setBehaviorKind(composer.kind);}
   const nextTime=context.cursor?restoreVideoTime(p.videos,context.cursor):null;
   if(nextTime!==null){setCurrent(nextTime);pendingSeek.current=Math.max(0,context.cursor!.offsetMs)/1000;activateVideo(context.cursor!.videoId);}
  }
  setImportOpen(false);setImportError('');
  const warnings=(p as Project & {warnings?:string[]}).warnings??[];
  setNotice([`补导入完成：新增 ${result.added_count} 段，跳过 ${result.skipped_names.length} 段已有同名视频。${result.added_count>0?'原有标注和习惯输入已保留；锚点、选区和撤销记录已重置。':''}`,...warnings].join(' '));
  if(composerStorageWarning)setError(composerStorageWarning);
  setSessionEntered(false);setPrepareOpen(true);preparation.refresh();
  await refresh();
 }
 async function relinkSources(sourcePath:string){
  await flush();const current=projectRef.current;if(!current)throw new Error('请先打开要关联原目录的项目。');
  const result=await api<RelinkResponse>(`/api/projects/${current.id}/sources/relink`,json('POST',{path:sourcePath,expected_revision:serverRevision.current}));
  if(result.project.id!==current.id||projectRef.current?.id!==current.id)throw new Error('关联结果与当前项目不一致，请重新打开项目核对。');
  // Relinking changes storage only. Keep the cursor, editor fields and undo history.
  const p=result.project,active=p.videos.find(video=>video.id===videoIdRef.current);
  projectRef.current=p;setProject(p);serverRevision.current=p.revision;
  if(active)pendingSeek.current=Math.max(0,(timeRef.current-active.start_ms)/1000);
  forgetProjectPreviews(p.id);setMediaVersion(version=>version+1);setMediaError('');
  setImportOpen(false);setImportError('');setSessionEntered(false);setPrepareOpen(true);preparation.refresh();
  setNotice([`已关联 ${result.relinked_count} 段原视频，清理 ${result.removed_copies} 个项目内副本。标注、光标和输入保持不变；缓存存放在原目录的 .datamark-cache 中。`,...(result.warnings??[])].join(' '));
  await refresh();
 }
 async function checkNewProjectName(){
  if(importMode!=='new')return;
  const message=projectNameError(newProjectName,false);if(message)throw new Error(message);
  await requireProjectNaming();
 }
 async function importSourcePath(sourcePath:string){
  await checkNewProjectName();
  await requireSourceCapabilities(importMode==='supplement');
  if(importMode==='relink'){await relinkSources(sourcePath);return;}
  await flush();const context=importMode==='supplement'?captureSupplement():null;
  if(context)await finishSupplement(await api<SupplementResponse>(`/api/projects/${context.project.id}/videos/open`,json('POST',{path:sourcePath,expected_revision:serverRevision.current})),context);
  else await finishImport(await api<Project>('/api/projects/open',json('POST',{path:sourcePath,name:newProjectName.trim()})));
 }
 async function chooseLocalSources(kind:'files'|'directory'){
  pausePlayback();
  await doBusy(kind==='directory'?'请选择原视频目录…':'请选择原视频文件…',async()=>{
   await checkNewProjectName();
   await requireSourceCapabilities(importMode==='supplement',true);
   const chosen=await api<{paths:string[]}>('/api/local-files/pick',json('POST',{kind}));
   if(!Array.isArray(chosen.paths))throw new Error('未能读取所选路径，请重试或手动填写原目录。');
   if(!chosen.paths.length)return;
   if(kind==='directory'){setBusy(importMode==='relink'?'正在验证原视频并迁移缓存…':'正在读取原视频目录…');await importSourcePath(chosen.paths[0]);return;}
   await importSourceFiles(chosen.paths);
  },true);
 }
 async function importSourceFiles(paths:string[]){
  await requireSourceCapabilities(importMode==='supplement');
  setBusy(`正在读取 ${paths.length} 段原视频的时长…`);
  await flush();const context=importMode==='supplement'?captureSupplement():null;
  if(context)await finishSupplement(await api<SupplementResponse>(`/api/projects/${context.project.id}/videos/files`,json('POST',{paths,expected_revision:serverRevision.current})),context);
  else await finishImport(await api<Project>('/api/projects/files',json('POST',{paths,name:newProjectName.trim()})));
 }
 async function browseNas(kind:'files'|'directory', folder?:string, page=0){
  setNasLoading(true);setImportError('');
  try{
   const health=await api<{capabilities?:string[]}>('/api/health',{cache:'no-store'});
   if(!health.capabilities?.includes('nas-source-browser'))throw new Error(oldBackendMessage);
   const query=new URLSearchParams({page:String(page)});if(folder)query.set('path',folder);
   const listing=await api<NasListing>(`/api/sources/browse?${query}`);
   if(nasPickerKind!==kind||nasListing?.path!==listing.path)setNasSelected([]);
   setNasPickerKind(kind);setNasListing(listing);
  }catch(error){setImportError(error instanceof Error?error.message:String(error));}
  finally{setNasLoading(false);}
 }
 async function importNasSelection(){
  if(!nasListing||!nasPickerKind)return;
  pausePlayback();
  await doBusy(nasPickerKind==='directory'?'正在读取 NAS 原视频目录…':'正在读取 NAS 视频文件…',async()=>{
   await checkNewProjectName();
   if(nasPickerKind==='directory')await importSourcePath(nasListing.path);
   else if(nasSelected.length)await importSourceFiles(nasSelected);
  },true);
 }
 async function openPath(){
  if(!path.trim())return;pausePlayback();
  await doBusy(importMode==='relink'?'正在验证原视频并迁移缓存…':'正在读取原视频素材…',()=>importSourcePath(path.trim()),true);
 }
 useEffect(()=>{
  if(!importOpen)return;
  const dialog=importDialogRef.current;dialog?.focus();
  const key=(event:KeyboardEvent)=>{
   if(event.key==='Escape'){event.preventDefault();if(!busyRef.current)setImportOpen(false);return;}
   if(event.key!=='Tab'||!dialog)return;
   const controls=Array.from(dialog.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex="0"]'));
   const first=controls[0],last=controls[controls.length-1];
   if(!first){event.preventDefault();return;}
   if(event.shiftKey&&(document.activeElement===first||document.activeElement===dialog)){event.preventDefault();last.focus();}
   else if(!event.shiftKey&&(document.activeElement===last||document.activeElement===dialog)){event.preventDefault();first.focus();}
  };
  window.addEventListener('keydown',key,true);
  return()=>{window.removeEventListener('keydown',key,true);importTriggerRef.current?.focus();};
 },[importOpen]);
 function pauseClearedPreviews(id:string){
  forgetProjectPreviews(id);
  if(projectRef.current?.id!==id)return;
  pausePlayback();setClearedPreviewId(id);setSessionEntered(false);setPrepareOpen(false);setMediaError('');
 }
 function askRenameProject(){
  const p=projectRef.current;if(!p||p.deletion_pending||busyRef.current)return;
  pausePlayback();setRenameError('');setRenameProject(p);
 }
 async function confirmRenameProject(value:string){
  const target=renameProject;if(!target||busyRef.current)return;
  const name=value.trim(),validation=projectNameError(value);if(validation){setRenameError(validation);return;}
  busyRef.current=true;setRenaming(true);setRenameError('');
  try{
   await flush();await requireProjectNaming();
   if(projectRef.current?.id!==target.id)throw new Error('当前项目已关闭，请重新打开后再重命名。');
   const saved=await api<Project>(`/api/projects/${target.id}/name`,json('PATCH',{name,expected_revision:serverRevision.current}));
   const current=projectRef.current;
   if(!current||current.id!==saved.id)throw new Error('项目已重命名，请从项目列表重新打开。');
   // Merge metadata only, retaining the playback session, local editor and undo history.
   const next={...current,name:saved.name,revision:saved.revision,updated_at:saved.updated_at};
   serverRevision.current=saved.revision;projectRef.current=next;setProject(next);
   setProjects(items=>items.map(item=>item.id===saved.id?{...item,name:saved.name,revision:saved.revision,updated_at:saved.updated_at}:item));
   setRenameProject(null);setNotice(`项目已重命名为「${saved.name}」`);
  }catch(e){setRenameError((e as Error).message);}finally{busyRef.current=false;setRenaming(false);}
 }
 async function askDeleteProject(){
  const p=projectRef.current;if(!p)return;
  pausePlayback();setDeleteError('');
  try{await flush();const fresh=await api<Project>(`/api/projects/${p.id}`);setDeleteProject(fresh);}
  catch(e){if(p.deletion_pending)setDeleteProject(p);else setError((e as Error).message);}
 }
 async function confirmDeleteProject(){
  if(!deleteProject||deleting)return;
  const target=deleteProject;setDeleting(true);setDeleteError('');
  pauseClearedPreviews(target.id);
  try{
   await api(`/api/projects/${target.id}/preview-cache/clear`,json('POST',{confirmed:true,expected_revision:target.revision}));
   writeProjectPreference(previewsClearedKey,JSON.stringify({id:target.id,at:Date.now()}));
   if(readProjectPreference(lastProjectKey)===target.id)writeProjectPreference(lastProjectKey,null);
   setDeleteProject(null);
   setNotice(`已清理「${target.name}」的本机预览。重新打开页面将等待选择素材；项目、草稿、原视频和 NAS 缓存包均保留。`);
   await refresh();
  }catch(e){setDeleteError((e as Error).message);}finally{setDeleting(false);}
 }
 useEffect(()=>{
  const removed=(event:StorageEvent)=>{if(event.key!==previewsClearedKey||!event.newValue)return;try{const {id}=JSON.parse(event.newValue);pauseClearedPreviews(id);if(readProjectPreference(lastProjectKey)===id)writeProjectPreference(lastProjectKey,null);setDeleteProject(null);setNotice('其他窗口已清理本机预览，当前输入和标注草稿均保留。');}catch{}};
  window.addEventListener('storage',removed);return()=>window.removeEventListener('storage',removed);
 },[]);
 async function chooseProject(id:string){if(!id)return;if(hasPendingEdit){setError('请先保存或取消正在编辑的标注，再切换项目；当前输入已保留。');return;}await doBusy('正在打开草稿…',async()=>{await flush();adopt(await api<Project>(`/api/projects/${id}`));});}
 async function addCustomTrack(name:string,mode:'state'|'event',labels:string[]){
  const current=projectRef.current;if(!current)return;
  await flush();
  const updated=await api<Project>(`/api/projects/${current.id}/custom-tracks`,json('POST',{name,mode,labels,expected_revision:serverRevision.current}));
  serverRevision.current=updated.revision;projectRef.current={...(projectRef.current??current),custom_tracks:updated.custom_tracks,annotations:updated.annotations,revision:updated.revision,updated_at:updated.updated_at};
  setProject(projectRef.current);setActiveTrack(updated.custom_tracks!.at(-1)!.id);setTrackRevealVersion(value=>value+1);resetEditorFields();setSelectedRange(null);updateHistory([],[]);
  setCustomTrackOpen(false);setNotice(`已添加「${name}」时间轴`);
 }
 async function confirmDeleteCustomTrack(){
  const current=projectRef.current,track=deleteCustomTrack;
  if(!current||!track||deletingCustomTrack)return;
  setDeletingCustomTrack(true);setDeleteCustomError('');
  try{
   await flush();
   const updated=await api<Project>(`/api/projects/${current.id}/custom-tracks/${track.id}`,json('DELETE',{confirmed:true,expected_revision:serverRevision.current}));
   serverRevision.current=updated.revision;
   projectRef.current={...(projectRef.current??current),custom_tracks:updated.custom_tracks,annotations:updated.annotations,revision:updated.revision,updated_at:updated.updated_at};
   setProject(projectRef.current);setActiveTrack('scene');setTrackRevealVersion(value=>value+1);resetEditorFields();setSelectedRange(null);updateHistory([],[]);
   if(comparison?.track===track.id){setComparison(null);setComparisonOpen(false);}
   setDeleteCustomTrack(null);setNotice(`已删除「${track.name}」时间轴；下次写回将清理其 NAS JSON`);
  }catch(reason){setDeleteCustomError(reason instanceof Error?reason.message:'删除时间轴失败，请重试。');}
  finally{setDeletingCustomTrack(false);}
 }
 function selectSegment(track:Track,s:Segment,seekTime=s.start_ms){
  if(hasPendingEdit){if(track===activeTrack&&s.id===selectedId){seek(seekTime);return;}setError('请先保存或取消正在编辑的标注，再选择其他记录；当前输入已保留。');return;}
  setActiveTrack(track);setSelectedRange(null);setSelectedId(s.id);setLabel(s.label);setBehaviorKind(s.kind??'interval');setStartText(formatTime(s.start_ms));setEndText(s.end_ms===null?'':formatTime(s.end_ms));setManual(true);seek(seekTime);
 }
 function addAnchor(){
  if(!project)return;pausePlayback();const point=timeRef.current;
  setAnchors(previous=>{
   const existing=previous.indexOf(point);
   return existing>=0?previous.filter((_,index)=>index!==existing):[...previous,point].slice(-3);
  });setSelectedRange(null);
 }
 function selectAnchorRange(firstIndex:0|1){
  if(anchors.length<firstIndex+2)return;if(hasPendingEdit){setError('请先保存或取消正在编辑的标注，再选择锚点区间。');return;}
  const [start,end]=anchors.slice(firstIndex,firstIndex+2).sort((a,b)=>a-b);
  if(start===end||!spans.some(span=>Math.max(start,span.start_ms)<Math.min(end,span.end_ms))){setError('锚点之间没有可标注的视频。');return;}
  pausePlayback();const draftLabel=!manual&&(eventTrack||activeTrack==='category')?label:'';resetEditorFields();setLabel(draftLabel);setSelectedRange({start_ms:start,end_ms:end});setStartText(formatTime(start));setEndText(formatTime(end));if(activeTrack==='category')setCategoryMode('range');if(eventTrack)setBehaviorKind('interval');setNotice(`已选中锚点 ${firstIndex+1}–${firstIndex+2} 区间，请在标注面板选择内容。`);
 }
 function dismissTimelineRange(){setSelectedRange(null);if(!manual&&!label.trim()){setStartText('');setEndText('');}}
 function cancelRange(){setSelectedRange(null);if(!manual){setStartText('');setEndText('');}}
 function editStart(value:string){setSelectedRange(null);setStartText(value);}
 function editEnd(value:string){setSelectedRange(null);setEndText(value);}
 function clearAfterAnnotation(){
  // Consume the old range directly: clearEditor would refill it from this render.
  if(selectedRange){setSelectedRange(null);if(activeTrack==='habit')clearComposer();else resetEditorFields();}
  else if(activeTrack==='habit'&&!manual)clearComposer();
  else clearEditor();
 }
 function changePostureAtCursor(value:string){
  const p=projectRef.current;if(!p||scrubbingRef.current)return;
  if(hasPendingEdit){setError('请先保存或取消正在编辑的标注，再切换姿势；当前输入已保留。');return;}
  syncNativeTime();const point=timeRef.current,recordings=p.recording_runs??p.videos;
  if(point>=p.duration_ms){setError('已到视频末尾，请先定位到要切换姿势的位置。');return;}
  if(!recordings.some(span=>point>=span.start_ms&&point<span.end_ms)){setError('此处为录制间隔，请在有视频的位置标注姿势。');return;}
  const next=switchCoveredState(p.annotations.posture,point,value,p.duration_ms,recordings);
  if(!commit({...p.annotations,posture:next}))return;
  setActiveTrack('posture');setTrackRevealVersion(version=>version+1);setSelectedRange(null);resetEditorFields();
  const changed=next.find(segment=>segment.start_ms<=point&&point<(segment.end_ms??p.duration_ms));
  setNotice(p.annotations.posture.length?`已从 ${formatTime(point)} 向后标注「${value}」至 ${formatTime(changed?.end_ms??p.duration_ms)}`:`已用「${value}」覆盖全部已录制时间，可在变化处继续切换`);
 }
 function changeState(value:string){
  if(!project)return;if(activeTrack==='category'&&categoryMode==='range'&&!manual){setLabel(value);return;}
  if(hasPendingEdit){setError('请先保存或取消正在编辑的标注，再切换状态；当前输入已保留。');return;}
  if(!selectedRange&&time>=project.duration_ms){setError('已到视频末尾，请先定位到要切换状态的位置。');return;}
  const next=selectedRange?applyAnnotationRange(project.annotations[activeTrack],selectedRange.start_ms,selectedRange.end_ms,value,spans,activeTrack,undefined,isExclusiveTrack(project,activeTrack)):switchCoveredState(project.annotations[activeTrack],time,value,project.duration_ms,spans,activeTrack==='category');
  if(!commit({...project.annotations,[activeTrack]:next}))return;clearAfterAnnotation();setNotice(selectedRange?'已标注选中区间，选区已取消':'状态已更新');
 }
 function finishBehavior(s:Segment){if(!project)return;if(time<=s.start_ms){setError('结束时间必须晚于开始时间。');return;}const next=applyAnnotationRange(project.annotations.habit,s.start_ms,time,s.label,spans,'habit',s.id);commit({...project.annotations,habit:next});if(selectedId===s.id){clearEditor();}}
 function saveSegment(){
  if(!project)return;const point=eventTrack&&behaviorKind==='point';const a=parseTime(startText),b=point?a:(endText.trim()?parseTime(endText):null);
  if(a===null||a<0||a>project.duration_ms||(!point&&(b===null||b<=a||b>project.duration_ms))){setError(point?'请填写有效发生时间（秒数或 HH:MM:SS.mmm）。':'请填写有效时间：0 ≤ 开始 < 结束 ≤ 时间线总时长。');return;}
  if(!label.trim()){setError('请选择或填写标注内容。');return;}
  if(point&&!spans.some(v=>a>=v.start_ms&&a<=v.end_ms)){setError('这个时间位于录制间隔，请在有视频的位置标注。');return;}
  if(!point&&!spans.some(v=>Math.max(a,v.start_ms)<Math.min(b!,v.end_ms))){setError('这个时间段没有视频，请重新选择。');return;}
  const next=point?[...project.annotations[activeTrack].filter(x=>x.id!==selectedId),{id:selectedId??crypto.randomUUID(),label:label.trim(),start_ms:a,end_ms:a,kind:'point' as const}].sort((x,y)=>x.start_ms-y.start_ms):applyAnnotationRange(project.annotations[activeTrack],a,b!,label.trim(),spans,activeTrack,selectedId??undefined,isExclusiveTrack(project,activeTrack));
  if(!commit({...project.annotations,[activeTrack]:next}))return;
  clearAfterAnnotation();
  setNotice('标注已保存，录制断档保持留空');
 }
 function remove(){if(!project||!selectedId)return;commit({...project.annotations,[activeTrack]:deleteAnnotation(project.annotations[activeTrack],selectedId,spans,activeTrack,eventTrack)});clearEditor();}
 function undo(){if(busyRef.current)return;if(hasPendingEdit){setError('请先保存或取消正在编辑的标注，再撤销；当前输入已保留。');return;}const past=historyRef.current,p=projectRef.current;if(!past.length||!p)return;updateHistory(past.slice(0,-1),[...redoRef.current.slice(-49),p.annotations]);commit(past[past.length-1],false);clearEditor();}
 function redo(){if(busyRef.current)return;if(hasPendingEdit){setError('请先保存或取消正在编辑的标注，再恢复；当前输入已保留。');return;}const future=redoRef.current,p=projectRef.current;if(!future.length||!p)return;updateHistory([...historyRef.current.slice(-49),p.annotations],future.slice(0,-1));commit(future[future.length-1],false);clearEditor();}
 async function exportFiles(){if(!project)return;if(hasPendingEdit){setError('请先保存或取消正在编辑的标注，再导出；当前输入尚未写入结果。');return;}await doBusy('正在生成时间轴文件…',async()=>{await flush();const r=await authFetch(`/api/projects/${project.id}/export`);if(!r.ok){const j=await r.json();throw new Error(j.detail);}const blob=await r.blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=`${projectDownloadName(project.name)}-timelines.zip`;a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);setNotice(`已导出 ZIP，内含 ${projectTracks(project).length} 个独立时间轴 JSON`);});}
 async function writeback(){if(!project)return;if(hasPendingEdit){setError('请先保存或取消正在编辑的标注，再写回原目录；当前输入尚未写入结果。');return;}await doBusy('正在写回 timeline 文件夹…',async()=>{await flush();await api(`/api/projects/${project.id}/writeback`,{method:'POST'});setNotice(`${projectTracks(project).length} 个 JSON 已写回原采集目录的 timeline 文件夹`);});}
 useEffect(()=>{
  const key=(e:KeyboardEvent)=>{
   if(!projectRef.current||!sessionEntered||busy||importOpen||prepareOpen||deleteProject||deleting||deleteCustomTrack||renameProject||settingsOpen||timelineImportOpen||e.isComposing||e.keyCode===229)return;
   const target=e.target instanceof Element?e.target:null;
   if(comparisonOpen&&['Home','End'].includes(e.code)){e.preventDefault();e.stopImmediatePropagation();seek(e.code==='Home'?0:projectRef.current.duration_ms);return;}
   const posture=!comparisonOpen&&postureShortcutLabel(e,!!target?.closest('input,textarea,select,[contenteditable]:not([contenteditable="false"]),[role="textbox"]'));
   if(posture){e.preventDefault();e.stopImmediatePropagation();changePostureAtCursor(posture);return;}
   if(!comparisonOpen&&e.code==='Space'&&(e.ctrlKey||e.metaKey)&&!e.altKey&&!e.shiftKey){
    e.preventDefault();e.stopImmediatePropagation();if(!e.repeat)addAnchor();return;
   }
   if(!comparisonOpen&&e.key==='Delete'&&!e.ctrlKey&&!e.metaKey&&!e.altKey&&!e.shiftKey){
    const target=e.target instanceof Element?e.target:null;
    if(target?.closest('input,textarea,select,[contenteditable]:not([contenteditable="false"])'))return;
    if(selectedId){e.preventDefault();e.stopImmediatePropagation();if(!e.repeat)remove();}return;
   }
   if(comparisonOpen&&e.code==='Space'&&(e.ctrlKey||e.metaKey)){e.preventDefault();e.stopImmediatePropagation();return;}
   // Reserve transport keys before buttons, sliders, selects and text inputs
   // receive them. In particular, Space must never click a focused annotation.
   if(['Space','ArrowLeft','ArrowRight'].includes(e.code)){
    e.preventDefault();e.stopImmediatePropagation();transportKeys.current.add(e.code);
    if(e.code==='Space'){if(!e.repeat)toggle();}
    else moveCursor((e.code==='ArrowLeft'?-1:1)*(e.ctrlKey?seekSettings.ctrlArrowMs:e.shiftKey?seekSettings.shiftArrowMs:seekSettings.arrowMs));
    return;
   }
   if(!comparisonOpen&&(e.ctrlKey||e.metaKey)&&!e.altKey&&e.key.toLowerCase()==='z'){
    e.preventDefault();e.stopImmediatePropagation();if(!e.repeat){if(e.shiftKey)redo();else undo();}
   }
  };
  const wheel=(e:WheelEvent)=>{
   const delta=wheelSeekDelta(e,seekSettings);if(delta===null)return;
   // Capture before timeline zoom, horizontal browsing or native Shift scrolling.
   e.preventDefault();e.stopImmediatePropagation();
   if(!projectRef.current||!sessionEntered||busy||importOpen||prepareOpen||deleteProject||deleting||deleteCustomTrack||renameProject||settingsOpen||timelineImportOpen||scrubbingRef.current)return;
   if(delta!==0)moveCursor(delta);
  };
  const release=(e:KeyboardEvent)=>{if(transportKeys.current.delete(e.code)){e.preventDefault();e.stopImmediatePropagation();}};
  const blur=()=>transportKeys.current.clear();
  window.addEventListener('keydown',key,true);window.addEventListener('keyup',release,true);window.addEventListener('blur',blur);window.addEventListener('wheel',wheel,{capture:true,passive:false});
  return()=>{window.removeEventListener('keydown',key,true);window.removeEventListener('keyup',release,true);window.removeEventListener('blur',blur);window.removeEventListener('wheel',wheel,true);};
 });
 const latestAnchor=anchors.at(-1),pair12=anchors.slice(0,2),pair23=anchors.slice(1,3);
 const interval12=pair12.length===2?formatTime(Math.abs(pair12[1]-pair12[0])).split('.')[0]:'--:--:--',interval23=pair23.length===2?formatTime(Math.abs(pair23[1]-pair23[0])).split('.')[0]:'--:--:--';
 const anchorControls=<div className="anchor-toolbar" title={anchors.map((value,index)=>`锚点 ${index+1}：${formatTime(value)}`).join("；")}><button className="secondary" title="添加锚点；当前位置已有锚点时移除（Ctrl + 空格）" onClick={addAnchor}><Bookmark size={14}/>添加锚点</button><button className="secondary anchor-pair-button" aria-label={`选择锚点 1 到 2，间隔 ${interval12}`} disabled={pair12.length<2} onClick={()=>selectAnchorRange(0)}><ScanLine size={14}/><span>1–2</span><strong>{interval12}</strong></button><button className="secondary anchor-pair-button" aria-label={`选择锚点 2 到 3，间隔 ${interval23}`} disabled={pair23.length<2} onClick={()=>selectAnchorRange(1)}><ScanLine size={14}/><span>2–3</span><strong>{interval23}</strong></button>{anchors.length>0&&<div className="anchor-metrics"><output className="anchor-duration" aria-label="当前时间到前一锚点的间隔" title="当前时间到最近一个锚点的时长">距前锚点 {formatTime(Math.abs(time-(latestAnchor??time))).split('.')[0]}</output></div>}{anchors.length>0&&<button className="text-button anchor-clear" aria-label="清空全部锚点" title="一次清空全部锚点" onClick={()=>{setAnchors([]);cancelRange();}}><Trash2 size={14}/></button>}</div>;
 const workspaceTools=<section className={'workspace-tools'+(!project?' no-project':'')} aria-label="项目管理与输出">
  <div className="workspace-actions" aria-label="项目操作">
   {projects.length>0&&<div className="project-switcher"><div className="project-picker"><FolderOpen size={15}/><select aria-label="打开已保存项目" value={project?.id??''} disabled={!!busy} onChange={e=>chooseProject(e.target.value)}><option value="">最近项目</option>{projects.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select><ChevronDown size={13}/></div><button className="project-rename-button" aria-label="重命名当前项目" title="重命名当前项目" disabled={!project||!!busy||project.deletion_pending} onClick={askRenameProject}><Pencil size={15}/></button><button className="project-delete-button" aria-label="清理本机预览" title="仅清理本机预览，保留项目、草稿和 NAS 缓存" disabled={!project||!!busy||deleting} onClick={askDeleteProject}><Trash2 size={16}/></button></div>}
   <button className="secondary new-project-action" aria-label="新建项目" title="导入视频并新建项目" onClick={()=>beginImport('new')} disabled={!!busy}><Plus size={16}/><span>新建项目</span></button>
   {project&&<button className="secondary supplement-action" onClick={()=>beginImport('supplement')} disabled={!!busy||project.deletion_pending}><Upload size={16}/>补导入视频</button>}
   {project&&<button className="secondary prepare-action" title="检查全部精简播放缓存、封面和悬停图片" onClick={openPreparation} disabled={!!busy||preparation.busy}>{preparation.status?.state==='ready'?<Check size={15}/>:preparation.status?.state==='running'&&preparation.status.requested?<LoaderCircle className="spin" size={15}/>:<Film size={15}/>}<span>{preparation.status?.checking?'检查缓存中':preparation.status?.state==='ready'?'本机播放就绪':preparation.status?.state==='running'&&preparation.status.requested?`准备中 ${preparation.status.ready}/${preparation.status.total}`:'准备全部预览'}</span></button>}
   {project&&<div className="project-output-actions" aria-label="标注输出"><span>标注输出</span><button className="secondary" disabled={!sessionEntered||!!busy} onClick={event=>{pausePlayback();comparisonTrigger.current=event.currentTarget;if(comparison)setComparisonOpen(true);else setTimelineImportOpen(true);}}><ScanLine size={16}/>时间轴对照</button><button className="primary" disabled={!!busy} onClick={exportFiles}><Download size={16}/>导出 JSON</button>{project.source_dir&&<button className="secondary writeback-action" disabled={!!busy} title={project.source_dir} onClick={writeback}><Save size={14}/>写回原目录</button>}</div>}
  </div>
  {project&&(coverageIssues.length>0||running.length>0)&&<div className="output-status" role="status" title="导出需补齐场景和姿势；写回允许场景留空，但仍需补齐姿势并结束进行中的事件。大类及自定义轴可留空。">{coverageIssues.map(item=><span key={item.track}>{names[item.track]} {item.count}处待补（{item.track==='scene'?'导出前':'导出及写回前'}）</span>)}{running.length>0&&<span>{running.length} 项习惯未结束</span>}</div>}
 </section>;
 return <div className={'app-shell '+(user.role==='admin'?'is-admin':'is-annotator')}>
  {adminOpen&&<AdminPanel projects={projects} onClose={()=>setAdminOpen(false)}/>}
  {passwordOpen&&<PasswordDialog onClose={()=>setPasswordOpen(false)} onChanged={logoutNow}/>}
  {auditOpen&&project&&<HistoryPanel projectId={project.id} onClose={()=>setAuditOpen(false)}/>}
  {customTrackOpen&&project&&<CustomTrackDialog onCancel={()=>setCustomTrackOpen(false)} onCreate={addCustomTrack}/>}
  {deleteCustomTrack&&project&&<DeleteCustomTrackDialog track={deleteCustomTrack} count={project.annotations[deleteCustomTrack.id]?.length??0} deleting={deletingCustomTrack} error={deleteCustomError} onCancel={()=>{setDeleteCustomTrack(null);setDeleteCustomError('');}} onConfirm={confirmDeleteCustomTrack}/>}
  {timelineImportOpen&&project&&<TimelineImportDialog project={project} initialTrack={comparison?.track??activeTrack} onCancel={()=>{setTimelineImportOpen(false);if(!comparisonOpen)queueMicrotask(()=>comparisonTrigger.current?.focus());}} onOpen={value=>{setComparison(value);setTimelineImportOpen(false);setComparisonOpen(true);}}/>}
  {comparisonOpen&&<div className="comparison-backdrop"/>}
  {settingsOpen&&<SeekSettingsDialog value={seekSettings} onCancel={()=>setSettingsOpen(false)} onSave={saveSeekSettings}/>}
  {renameProject&&<RenameProjectDialog name={renameProject.name} saving={renaming} error={renameError} onCancel={()=>{setRenameProject(null);setRenameError('');}} onConfirm={confirmRenameProject}/>}
  {deleteProject&&<DeleteProjectDialog project={deleteProject} deleting={deleting} error={deleteError} onCancel={()=>{setDeleteProject(null);setDeleteError('');}} onConfirm={confirmDeleteProject}/>}
  <header className="topbar" inert={comparisonOpen||timelineImportOpen||importOpen||!!busy||!!deleteProject||!!renameProject||prepareOpen||settingsOpen||adminOpen||passwordOpen}><div className="brand"><img src="/favicon.svg" alt=""/><span>DataMark</span></div><h1 className="compact-project-name" title={project?`${project.name} · ${project.videos.length} 段视频 · 录制起点 ${project.recording_start??'未设置'}`:'视频标注工作台'}>{project?.name??'视频标注工作台'}</h1><div className="top-actions"><span className="save-indicator" role="status" title={project?saveState:'就绪'}><span className={saveFailed?'status-dot bad':'status-dot'}/>{project?saveState:'就绪'}</span><span>{user.display_name}</span>{user.role==='admin'&&<button className="quiet" onClick={()=>setAdminOpen(true)}>账号管理</button>}<button className="quiet" disabled={hasPendingEdit||pendingSaves.current>0||saveFailed||!!busy} title="请先保存或取消当前编辑" onClick={()=>setPasswordOpen(true)}>修改密码</button><button className="quiet" onClick={()=>void onLogout()}>退出登录</button><button className="quiet icon-button workspace-fullscreen-button" aria-label="切换工作台全屏" title="工作台全屏（全屏时按 Esc 退出）" onClick={toggleWorkspaceFullscreen}><Maximize className="workspace-fullscreen-enter" size={16}/><Minimize className="workspace-fullscreen-exit" size={16}/></button><button className="quiet icon-button" aria-label="操作帮助" title="操作帮助" onClick={()=>setHelp(!help)}><Keyboard size={16}/></button><button className="quiet icon-button" aria-label="进度快捷键设置" title="设置视频进度快捷键" aria-haspopup="dialog" onClick={()=>{pausePlayback();setHelp(false);setSettingsOpen(true);}}><Settings size={16}/></button></div></header>
  {error&&<div className="message error" role="alert"><span>{error}</span><button aria-label="关闭提示" onClick={()=>setError('')}><X size={16}/></button></div>}
  {notice&&<div className="message success" role="status"><Check size={16}/><span>{notice}</span><button aria-label="关闭提示" onClick={()=>setNotice('')}><X size={16}/></button></div>}
  {project?.needs_source_relink&&user.role==='admin'&&<div className="source-relink-notice" inert={importOpen||!!busy||!!deleteProject||!!renameProject||prepareOpen||settingsOpen}><FolderOpen size={16}/><span>此项目仍使用平台内的视频副本。关联原目录后，缓存会移到原视频旁，释放重复存储。</span><button className="secondary" disabled={!!busy||project.deletion_pending} onClick={()=>beginImport('relink')}>关联原目录</button></div>}
  {help&&<div className="help-panel"><Keyboard size={18}/><span>顶部全屏按钮 进入工作台全屏，Esc 退出</span><span>空格 播放／暂停</span><span>Ctrl + 空格 添加锚点</span><span>← → 前后 {seekSettings.arrowMs/1000} 秒</span><span>Shift + ← → 前后 {seekSettings.shiftArrowMs/1000} 秒</span><span>Ctrl + ← → 前后 {seekSettings.ctrlArrowMs/1000} 秒</span><span>Ctrl + Alt + 滚轮 上退下进，每次 {seekSettings.wheelMs/1000} 秒</span><span>Shift + 滚轮 上退下进，每次 {seekSettings.shiftWheelMs/1000} 秒</span><span>右上角设置按钮 可调整移动步长</span><span>调整视频进度会暂停，松开后保持暂停</span><span>Ctrl + 滚轮 缩放时间轴（最细每屏5分钟）</span><span>Ctrl + + / - / 0 已禁用浏览器缩放</span><span>轴的条带上滚轮 左右浏览时间轴</span><span>重叠轴内 Alt + 滚轮 查看其它层</span><span>拖动蓝色光标定位视频</span><span>Delete 删除选中色块</span><span>Z 动 / X 坐 / C 站 / V 躺：从当前播放位置向后切换姿势</span><span>Ctrl + Z 撤销</span><span>Ctrl + Shift + Z 恢复</span><span>点击轴名或右侧类型切换；点击色块可编辑。左右键和空格固定控制视频。</span></div>}
  <main ref={comparisonRef} tabIndex={comparisonOpen?-1:undefined} role={comparisonOpen?"dialog":undefined} aria-modal={comparisonOpen?true:undefined} aria-label={comparisonOpen?"时间轴对照查看":undefined} className={"editor-grid"+(comparisonOpen?" comparison-view":"")} inert={timelineImportOpen||importOpen||!!busy||!!deleteProject||!!renameProject||prepareOpen||settingsOpen}>
  {comparisonOpen&&comparison&&project&&<header className="comparison-heading"><div><h2>时间轴对照 · {trackName(project,comparison.track)}</h2><p title={[comparison.name,comparison.alignment,...comparison.warnings].join(' · ')}>{comparison.name} · {comparison.alignment}{comparison.warnings.length>0?' · '+comparison.warnings.join(' '):''}</p></div><button className="secondary" onClick={()=>{pausePlayback();setTimelineImportOpen(true);}}>更换时间轴</button><button className="icon-button" aria-label="进度快捷键设置" onClick={()=>{pausePlayback();setSettingsOpen(true);}}><Settings size={16}/></button><button className="secondary" onClick={closeComparison}><X size={16}/>关闭查看</button></header>}
  {project&&!sessionEntered?<section className="session-start-screen"><Film size={34}/><h2>{clearedPreviewId===project.id?'本机预览已清理':restoreOnReady.current===project.id?'正在载入项目':'预览尚未就绪'}</h2><p>{clearedPreviewId===project.id?'项目和标注草稿仍在。需要继续这个项目时，再主动准备预览；也可以从右侧新建项目选择新素材。':restoreOnReady.current===project.id?(preparation.status?.detail||'正在检查已有播放素材。'):'需要先准备视频、封面和悬停图片，才能进入标注工作台。'}</p><button className="primary" onClick={openPreparation}>{clearedPreviewId===project.id?'重新准备此项目预览':'查看素材准备'}<ArrowRight size={16}/></button></section>:<section className="left-column"><div className="preview-panel"><div className="panel-heading"><div><Film size={16}/><h2>视频预览</h2></div><span className="muted-text">{activeVideo?`${project!.videos.findIndex(v=>v.id===videoId)+1} / ${project!.videos.length} · ${activeVideo.name}`:'等待选择素材'}</span></div>
   <div className={'video-stage'+(!project?' empty':'')}>{project&&activeVideo?<><video key={`${project.id}-${videoId}-${mediaVersion}-${profile.fast}`} ref={videoRef} data-video-id={activeVideo.id} data-time-scale={profile.scale} crossOrigin={separateApiOrigin?'use-credentials':undefined} src={preview.state==='ready'&&preview.url?apiUrl(preview.url):undefined} poster={activeVideo.thumbnail_url?sessionAssetUrl(project.id,activeVideo.thumbnail_url):undefined} muted={muted||profile.fast} playsInline preload="auto" onLoadedMetadata={e=>{if(e.currentTarget===videoRef.current)onReady();}} onPlaying={e=>{if(e.currentTarget===videoRef.current&&wantsPlay.current)setPlaying(true);}} onPlay={e=>{if(e.currentTarget!==videoRef.current)return;if(!wantsPlay.current)e.currentTarget.pause();else setPlaying(true);}} onPause={e=>{if(e.currentTarget===videoRef.current&&!wantsPlay.current)setPlaying(false);}} onTimeUpdate={e=>{if(e.currentTarget===videoRef.current)syncNativeTime();}} onEnded={e=>{if(e.currentTarget===videoRef.current)ended();}} onError={e=>{if(e.currentTarget!==videoRef.current||preview.state!=='ready')return;pausePlayback();setMediaError('预览读取失败，请检查素材可读性后重试。');}} onClick={toggle}/><div className="video-tag"><span className="live-dot"/> {profile.fast?'高倍速预览 · 本机缓存':'流畅预览 · 270p'}</div><div className="video-clock">{formatTime(time)}</div>{previewPending&&(preview.state!=='idle'||showPreviewWait)&&!previewError&&<div className="media-cover preview-preparation" role="status"><LoaderCircle className="spin"/><p>{preview.state==='running'?(profile.fast?'正在准备流畅快进预览':'正在生成此片段的预览'):preview.state==='queued'?'此片段正在排队':'正在读取预览缓存'}</p>{preview.state==='running'&&preview.progress!==null&&<><progress aria-label="预览生成进度" max={100} value={preview.progress}/><strong>{Math.floor(preview.progress)}%</strong></>}<small>{preview.detail||(preview.state==='running'?'仅首次准备，完成后可直接复用缓存，并提前准备后续片段':'可以继续浏览时间轴和其他片段')}</small></div>}{showBuffering&&!previewPending&&!previewError&&<div className="media-buffering" role="status"><LoaderCircle className="spin" size={14}/>正在缓冲…</div>}{scrubbing&&<div className="media-scrubbing">拖动定位 · 松开后显示目标画面</div>}{previewError&&<div className="media-cover"><p>{previewError}</p><button className="secondary" onClick={()=>{pendingSeek.current=Math.max(0,(timeRef.current-activeVideo.start_ms)/1000);setMediaError('');forgetProjectPreviews(project.id);setMediaVersion(v=>v+1);setSessionEntered(false);setPrepareOpen(true);void preparation.start(project.id,true);}}>重新加载</button></div>}</>:<div className="empty-preview"><div className="empty-film"><Film size={34}/><span>＋</span></div><h2>选择素材，开始标注</h2><p>{sharedServer?'从已挂载的 NAS 选择视频文件或原视频目录。':'选择本机视频文件或原视频目录。'}<br/>原视频留在原位置，系统会准备标注用预览。</p><button className="primary" onClick={()=>beginImport('new')}><Upload size={17}/>新建项目并选择素材</button><span className="format-hint">支持 MP4 · AVI · MOV · MKV · WEBM</span></div>}</div>
   <div className="player-controls"><div className="play-controls"><button className="icon-button" aria-label="后退1秒" disabled={!project} onClick={()=>moveCursor(-1000)}><SkipBack size={17}/></button><button className="play-button" aria-label={playing?'暂停':'播放'} disabled={!project} onClick={toggle}>{playing?<Pause size={19}/>:<Play size={19} fill="currentColor"/>}</button><button className="icon-button" aria-label="前进1秒" disabled={!project} onClick={()=>moveCursor(1000)}><SkipForward size={17}/></button><span className="player-time">{formatTime(time)}<b>/</b><span>{formatTime(project?.duration_ms??0)}</span></span></div><div className="play-options"><PlaybackSpeed value={rate} onChange={setRate}/><button className="icon-button" aria-label={muted?'开启声音':'静音'} onClick={()=>setMuted(!muted)}>{muted?<VolumeX size={17}/>:<Volume2 size={17}/>}</button><button className="icon-button" aria-label="全屏视频" disabled={!project} onClick={()=>videoRef.current?.requestFullscreen()}><Maximize size={17}/></button></div></div>
  </div>
  {project?<Timeline comparison={comparisonOpen?comparison:null} project={playbackProject!} currentTime={time} activeTrack={activeTrack} trackRevealVersion={trackRevealVersion} selectedId={selectedId} anchors={comparisonOpen?[]:anchors} selectedRange={comparisonOpen?null:selectedRange} anchorControls={comparisonOpen?undefined:anchorControls} onRangeDismiss={dismissTimelineRange} onSeek={seek} onScrubStart={beginScrub} onScrubEnd={endScrub} onTrackSelect={selectTrack} onSegmentSelect={selectSegment}/>:<div className="empty-timeline"><div className="panel-heading"><div><Layers3 size={16}/><h2>标注时间线</h2></div><span>导入视频后开始标注</span></div>{TRACKS.map((t,i)=><div className={'placeholder-track '+t} key={t}><span><i/>{names[t]}</span><div><span>{['室内 / 室外','动 / 坐 / 站 / 躺','单点切换 / 区间叠加','瞬时 / 持续 · 支持重叠'][i]}</span></div></div>)}</div>}
  </section>}
  <aside className="workspace-sidebar" inert={comparisonOpen}>{workspaceTools}{sessionEntered&&project&&<section className="annotation-panel"><div className="panel-heading"><div><span className="heading-mark"/><h2>标注面板</h2></div><div className="panel-heading-actions"><span className="small-badge">{count} 条</span>{customTrack(project,activeTrack)&&<button className="icon-button delete-custom-track-button" aria-label="删除当前自定义时间轴" title="删除当前自定义时间轴" disabled={!!busy||hasPendingEdit||pendingSaves.current>0||saveFailed} onClick={()=>{pausePlayback();setDeleteCustomError('');setDeleteCustomTrack(customTrack(project,activeTrack)??null);}}><Trash2 size={15}/></button>}<button className="icon-button" aria-label="查看标注编辑历史" title="查看标注编辑历史" onClick={()=>setAuditOpen(true)}><History size={15}/></button></div></div><div className="track-tabs" role="tablist" aria-label="标注轨道">{projectTracks(project).map(t=><button key={t} role="tab" aria-selected={activeTrack===t} className={activeTrack===t?'active '+(t.startsWith('custom_')?'custom':t):''} onClick={()=>selectTrack(t)}><i/>{trackName(project,t)}</button>)}<button className="add-custom-track" aria-label="添加自定义时间轴" title="添加自定义时间轴" disabled={!!busy||hasPendingEdit||pendingSaves.current>0||saveFailed} onClick={()=>{pausePlayback();setCustomTrackOpen(true);}}><Plus size={14}/>添加</button></div>
   <div className="annotation-content"><div className="section-caption"><span>{trackName(project,activeTrack)}标注</span><span className="accent-text">{eventTrack?'可重叠事件':activeTrack==='category'?'可留空 · 可重叠':activeTrack.startsWith('custom_')?'可留空 · 互斥状态':'全覆盖 · 互斥状态'}</span></div><div className="cursor-card"><span>当前时间</span><strong>{formatTime(time)}</strong>{project&&<div className="cursor-progress"><i style={{width:`${100*time/project.duration_ms}%`}}/></div>}</div>
    {selectedRange&&<div className="selected-range-card"><span>选中区间</span><strong>{formatTime(selectedRange.start_ms)} → {formatTime(selectedRange.end_ms)}</strong><button className="text-button" onClick={cancelRange}>取消选区</button></div>}
    {!eventTrack?<>
     <p className="instruction">{selectedRange?'选择标签，标注选中区间。':project&&!project.annotations[activeTrack].length?'选择初始标签，覆盖全部已录制时间，再在变化处切换。':'在变化发生的位置，点击按钮切换状态。'}<br/>{activeTrack==='category'?'时间段标注叠加，允许同一时间有多个大类。':'选区标注覆盖区间内内容，区间外保持不变。'}</p>
     {activeTrack==='category'&&!manual&&<div className="kind-tabs" aria-label="大类标注方式"><button className={categoryMode==='state'?'active':''} onClick={()=>{if(hasCategoryDraft){setError('请先保存或清空时间段输入，再切换标注方式。');return;}setCategoryMode('state');setSelectedRange(null);setStartText('');setEndText('');}}>单点切换</button><button className={categoryMode==='range'?'active':''} onClick={()=>setCategoryMode('range')}>时间段叠加</button></div>}
     <div className={'state-buttons '+(activeTrack.startsWith('custom_')?'custom':activeTrack)}>{fixedLabels.map((s,i)=><button key={s} disabled={!project} title={activeTrack==='posture'?`${POSTURE_SHORTCUTS[i].key}：从当前播放位置向后标注${s}`:undefined} className={(activeTrack==='category'&&categoryMode==='range'?label===s:currentStates.some(state=>state.label===s))?'chosen':''} onClick={()=>changeState(s)}>{activeTrack==='scene'&&<span>{['⌂','☀'][i]}</span>}{activeTrack==='posture'&&<span>{s}</span>}<strong>{s}</strong>{activeTrack==='posture'&&<kbd>{POSTURE_SHORTCUTS[i].key}</kbd>}</button>)}</div>
     {activeTrack==='posture'&&<p className="input-hint">Z 动 · X 坐 · C 站 · V 躺：从当前播放位置改到下一姿势分界，无分界则到本段录制结束。快捷键不使用锚点选区，输入文字时不触发。</p>}
     <div className="current-label"><i/>{currentStates.length?`当前位置：${[...new Set(currentStates.map(state=>state.label))].join('＋')}`:'当前位置尚未标注'}</div>
     {activeTrack==='category'&&categoryMode==='range'&&!manual&&<><div className="capture-fields"><label>开始时间<input aria-label="大类开始时间" placeholder="00:00:00.000" value={startText} onChange={e=>editStart(e.target.value)}/><button className="secondary" disabled={!project} onClick={()=>editStart(formatTime(time))}>标记开始</button></label><label>结束时间<input aria-label="大类结束时间" placeholder="00:00:00.000" value={endText} onChange={e=>editEnd(e.target.value)}/><button className="secondary" disabled={!project} onClick={()=>editEnd(formatTime(time))}>标记结束</button></label></div><button className="primary wide" disabled={!project||!label||!startText||!endText} onClick={saveSegment}><Check size={15}/>保存大类标注</button><button className="text-button" onClick={()=>{setLabel('');setStartText('');setEndText('');setSelectedRange(null);setError('');}}>清空输入</button></>}
    </>:<>
     <p className="instruction">先输入内容或先标时间都可以。<br/>支持瞬时事件、持续区间和时间重叠。</p>
     <div className="kind-tabs" aria-label="事件记录方式"><button className={behaviorKind==='interval'?'active':''} onClick={()=>setBehaviorKind('interval')}>持续区间</button><button className={behaviorKind==='point'?'active':''} onClick={()=>{setBehaviorKind('point');setSelectedRange(null);}}>瞬时事件</button></div>
     {!manual&&<><label className="field-label" htmlFor="behavior-label">事件内容</label><input id="behavior-label" placeholder="例如：抽烟、喝咖啡、使用手机" value={label} maxLength={500} onChange={e=>setLabel(e.target.value)} disabled={!project}/><div className="capture-fields"><label>{behaviorKind==='point'?'发生时间':'开始时间'}<input aria-label="习惯开始时间" placeholder="00:00:00.000" value={startText} onChange={e=>editStart(e.target.value)}/><button className="secondary" disabled={!project} onClick={()=>editStart(formatTime(time))}>{behaviorKind==='point'?'标记发生时点':'标记开始'}</button></label>{behaviorKind==='interval'&&<label>结束时间<input aria-label="习惯结束时间" placeholder="00:00:00.000" value={endText} onChange={e=>editEnd(e.target.value)}/><button className="secondary" disabled={!project} onClick={()=>editEnd(formatTime(time))}>标记结束</button></label>}</div><button className="primary wide" disabled={!project||!label.trim()||!startText||(behaviorKind==='interval'&&!endText)} onClick={saveSegment}><Check size={15}/>保存标注</button><p className="input-hint">{behaviorKind==='point'?'瞬时事件的开始和结束为同一时刻。':'跨越录制间隔的标注会在断档处分段保存。'}</p></>}
     {activeTrack==='habit'&&running.length>0&&<div className="ongoing-list"><div className="field-label">正在标注 <span>{running.length}</span></div>{running.map(s=><div className="ongoing" key={s.id}><div><strong><i/>{s.label}</strong><small>{formatTime(s.start_ms)} 开始</small></div><button disabled={time<=s.start_ms} onClick={()=>finishBehavior(s)}>结束</button></div>)}</div>}
    </>}
    {selected&&<div className="annotation-author" role="status">创建人 {selected.created_by_name??'未知（旧标注）'}{selected.updated_by_name&&<> · 最近编辑 {selected.updated_by_name}</>}</div>}
    {manual&&<div className="segment-editor"><div className="section-caption"><span>编辑标注</span><button className="icon-button" aria-label="取消编辑" onClick={clearEditor}><X size={15}/></button></div><label className="field-label">标注内容</label>{eventTrack?<input aria-label="编辑标注内容" value={label} maxLength={500} onChange={e=>setLabel(e.target.value)}/>:<select aria-label="编辑状态内容" value={label} onChange={e=>setLabel(e.target.value)}>{!fixedLabels.includes(label)&&label&&<option value={label} disabled>{label}</option>}{fixedLabels.map(s=><option key={s}>{s}</option>)}</select>}<div className="time-fields"><label>开始时间<input aria-label="开始时间" value={startText} onChange={e=>editStart(e.target.value)}/><button onClick={()=>editStart(formatTime(time))}>使用当前时间</button></label><label>{behaviorKind==='point'&&eventTrack?'瞬时事件（同一时刻）':'结束时间'}<input disabled={behaviorKind==='point'&&eventTrack} aria-label="结束时间" placeholder="进行中" value={behaviorKind==='point'&&eventTrack?startText:endText} onChange={e=>editEnd(e.target.value)}/><button disabled={behaviorKind==='point'&&eventTrack} onClick={()=>editEnd(formatTime(time))}>使用当前时间</button></label></div><div className="edit-actions"><button className="primary" onClick={saveSegment}><Check size={15}/>保存标注</button>{selected&&<button className="danger-icon" aria-label="删除此标注" onClick={remove}><Trash2 size={16}/></button>}</div></div>}
   </div><div className="annotation-footer"><button className="secondary" disabled={!history.length||!!busy} onClick={undo} title="Ctrl + Z"><RotateCcw size={14}/>撤销</button><button className="secondary" disabled={!redoHistory.length||!!busy} onClick={redo} title="Ctrl + Shift + Z"><RotateCw size={14}/>恢复</button></div>
  </section>}</aside></main>
  {saveFailed&&<div className="save-recovery" role="alert" inert={importOpen||!!busy||!!deleteProject||!!renameProject||prepareOpen||settingsOpen}><span>{saveState}</span><button className="text-button" disabled={!!busy} onClick={retrySave}>重试保存</button><button className="text-button" disabled={!!busy} title="先下载包含本页未保存改动的恢复 JSON，再读取服务器草稿" onClick={recoverAndReload}>保存恢复副本并重新载入</button></div>}
  {importOpen&&<div className="modal-backdrop" onClick={()=>!busy&&setImportOpen(false)}><section ref={importDialogRef} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby="import-title" className={'import-modal'+(nasPickerKind?' nas-picker-open':'')} onClick={e=>e.stopPropagation()}>
   <button className="modal-close icon-button" aria-label="关闭导入" disabled={!!busy} onClick={()=>setImportOpen(false)}><X size={20}/></button><div className="modal-icon"><FolderOpen size={24}/></div>
   <h2 id="import-title">{importMode==='relink'?'关联原视频目录':importMode==='supplement'?'补导入视频':'新建采集项目'}</h2>
   {importMode==='new'&&<div className="project-name-field"><label className="field-label" htmlFor="new-project-name">项目名称</label><input id="new-project-name" className="project-name-input" value={newProjectName} disabled={!!busy} placeholder="留空则按导入时间命名" autoComplete="off" aria-describedby="new-project-name-hint" aria-invalid={!!projectNameError(newProjectName,false)} onChange={e=>setNewProjectName(e.target.value)}/><p id="new-project-name-hint" className={projectNameError(newProjectName,false)?'import-error':'input-hint'}>{projectNameError(newProjectName,false)||'最多 80 个字符'}</p></div>}
   {importMode!=='new'&&<div className="supplement-target"><span>当前项目</span><strong>{project?.name}</strong><small>已有 {project?.videos.length??0} 段视频</small></div>}
   <p>{importMode==='relink'?<>选择最初的原视频目录，验证匹配后迁移已有缓存并清理平台内的视频副本。<br/>标注、撤销记录和当前输入都会保留。</>:importMode==='supplement'?<>漏掉的视频会按录制时间插入当前时间轴。<br/>保留已有标注和预览缓存，同名视频自动跳过。</>:<>识别文件名中的录制时间，自动排序。<br/>保留视频之间的真实录制间隔。</>}</p>
   <div className="source-storage-note"><strong>原视频保留原目录，不复制</strong><span>预览缓存存放在原目录的 <code>.datamark-cache</code> 子文件夹，并附有 README.md 用途说明。标注草稿保存在平台内。</span><small>{sharedServer?'请保持 Ubuntu 的 NAS 挂载可访问。':'使用移动硬盘、SD 卡或共享目录时，请保持该位置可访问。'}</small></div>
   {importError&&<p className="import-error" role="alert">{importError}</p>}
   <div className="prepare-import-option"><Check size={17}/><span><strong>先准备全部播放素材，再开始标注</strong><small>复用原目录中已有的有效缓存，{sharedServer?'Ubuntu 服务器':'本机'}保存精简播放素材。</small></span></div>
   <div className={'source-pickers'+(importMode==='relink'?' single':'')}>
    {importMode!=='relink'&&<button className="upload-zone" disabled={!!busy||nasLoading} onClick={()=>sharedServer?void browseNas('files'):void chooseLocalSources('files')}><Film size={23}/><strong>选择视频文件</strong><span>{sharedServer?'从 NAS 多选，可导出 JSON':'支持一次多选，可导出 JSON'}</span></button>}
    <button className="upload-zone" disabled={!!busy||nasLoading} onClick={()=>sharedServer?void browseNas('directory'):void chooseLocalSources('directory')}><FolderOpen size={23}/><strong>选择原视频目录</strong><span>{sharedServer?'浏览 NAS，支持写回标注':importMode==='relink'?'匹配已有视频，迁移缓存':'直接读取目录，支持写回标注'}</span></button>
   </div>
   {sharedServer&&nasPickerKind&&nasListing&&<div className="nas-browser"><div className="nas-browser-heading"><strong>{nasPickerKind==='files'?'选择 NAS 视频文件':'选择 NAS 原视频目录'}</strong><button className="secondary" disabled={nasLoading||!!busy} onClick={()=>{setNasPickerKind(null);setNasListing(null);setNasSelected([]);}}>关闭</button></div><div className="nas-browser-path" title={nasListing.path}>{nasListing.path}</div><div className="nas-browser-list">{nasListing.parent&&<button disabled={nasLoading||!!busy} onClick={()=>void browseNas(nasPickerKind,nasListing.parent??undefined)}>↑ 上一级</button>}{nasListing.entries.map(entry=><div className="nas-browser-row" key={entry.path}>{entry.kind==='directory'?<button disabled={nasLoading||!!busy} onClick={()=>void browseNas(nasPickerKind,entry.path)}><FolderOpen size={15}/>{entry.name}</button>:nasPickerKind==='files'?<label><input type="checkbox" checked={nasSelected.includes(entry.path)} disabled={nasLoading||!!busy} onChange={event=>setNasSelected(previous=>event.target.checked?[...previous,entry.path]:previous.filter(value=>value!==entry.path))}/>{entry.name}</label>:<span>{entry.name}</span>}</div>)}{!nasListing.entries.length&&<p>此目录没有可选择的视频或子目录。</p>}</div>{nasPickerKind==='files'&&<p className="input-hint">单独选视频可预览和导出 JSON；需要写回 NAS 时请选完整原视频目录。</p>}<div className="nas-browser-actions">{nasListing.page>0&&<button className="secondary" disabled={nasLoading||!!busy} onClick={()=>void browseNas(nasPickerKind,nasListing.path,nasListing.page-1)}>上一页</button>}{nasListing.has_more&&<button className="secondary" disabled={nasLoading||!!busy} onClick={()=>void browseNas(nasPickerKind,nasListing.path,nasListing.page+1)}>下一页</button>}<button className="primary" disabled={nasLoading||!!busy||(nasPickerKind==='files'&&!nasSelected.length)} onClick={()=>void importNasSelection()}>{nasPickerKind==='directory'?'使用此目录':`导入已选 ${nasSelected.length} 个视频`}</button></div></div>}
   {!sharedServer&&<div className="divider"><span>或直接填写本机 / 内网路径</span></div>}
   <label className="field-label" htmlFor="source-path">{importMode==='relink'?'原视频目录路径':'视频文件或目录路径'}</label>
   <input id="source-path" disabled={!!busy} value={path} placeholder={sharedServer?'填写服务器上的 NAS 完整路径，例如 /mnt/nas/homes/…':'粘贴原视频所在目录的完整路径'} onChange={e=>setPath(e.target.value)} onKeyDown={e=>{if(e.key==='Enter')openPath();}}/>
   <p className="input-hint">{importMode==='relink'?'请提供包含这些原视频的目录；校验成功后才会清理对应副本。':importMode==='supplement'?'支持单个视频或整个目录；再次选择原目录可自动补齐遗漏视频。':sharedServer?'请输入服务器可访问的采集根目录或内网共享目录。':'可选择文件或目录，也可填写本机可访问的完整路径。'}</p>
   <button className="primary wide" disabled={!path.trim()||!!busy} onClick={openPath}><FolderOpen size={16}/>{importMode==='relink'?'关联并迁移缓存':importMode==='supplement'?'补入当前项目':'读取并新建项目'}</button>
   <p className="privacy-note"><span className="status-dot"/>直接读取所选路径，不上传或复制原视频</p>
  </section></div>}
  {prepareOpen&&project&&<PreparePanel projectName={project.name} status={preparation.status} error={preparation.error} busy={preparation.busy} skippedVideos={project.skipped_videos} onSkipFailed={ids=>changeSkippedVideos(ids)} onRestoreSkipped={()=>changeSkippedVideos()} onRetry={()=>void openPreparation()} onEnter={()=>{if(preparation.status?.state==='ready'&&preparation.manifest&&!preparation.error){setSessionEntered(true);setPrepareOpen(false);}}} onClose={()=>{enterAfterSkip.current=null;setPrepareOpen(false);}}/>}
  {busy&&<div className="busy-overlay" role="status"><LoaderCircle className="spin" size={28}/><strong>{busy}</strong><span>大文件或内网素材可能需要一些时间</span></div>}
 </div>;
}
