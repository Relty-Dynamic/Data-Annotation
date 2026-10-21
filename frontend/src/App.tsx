import TaskPanel from './tasks/TaskPanel';
import TaskOutputSync from './tasks/TaskOutputSync';
import TaskFields from './tasks/TaskFields';
import TaskWorkbenchReview from './tasks/TaskWorkbenchReview';
import {canUseLegacyTrackTools} from './tasks/permissions';
import type {TaskVersion} from './tasks/types';
import {taskFromSearch} from './tasks/transport.ts';
import {saveBeforeNavigation} from './tasks/saveBeforeNavigation';
import { useEffect, useMemo, useRef, useState } from 'react';
import {authFetch, type Account} from './auth';
import {apiUrl, separateApiOrigin} from './apiOrigin.ts';
import {browserVideoUrl, clearBrowserVideoCache} from './browserVideoCache.ts';
import AdminPanel from './AdminPanel';
import TaskAccounts from './tasks/TaskAccounts';
import MockUploadPanel from './MockUploadPanel';
import S3UploadEntry from './S3UploadEntry';
import PasswordDialog from './PasswordDialog';
import HistoryPanel from './HistoryPanel';
import { Upload, FolderOpen, Download, Save, Play, Pause, SkipBack, SkipForward, X, Trash2, Check, Film, Layers3, Keyboard, Maximize, Minimize, Volume2, VolumeX, Circle, ArrowRight, LoaderCircle, Bookmark, ScanLine, Settings, History } from 'lucide-react';
import Timeline from './Timeline';
import { parseRecordingOrigin, recordingDateTime } from './recordingTime';
import './tasks/workspace.css';
import { importPostureTimeline, type PostureImport } from './postureImport';
import PlaybackSpeed from './PlaybackSpeed';
import VideoStage from './VideoStage';
import {revealInPanel} from './revealInPanel';
import DeleteProjectDialog from './DeleteProjectDialog';
import RenameProjectDialog from './RenameProjectDialog';
import ProjectPicker from './ProjectPicker';
import TaskPicker from './tasks/TaskPicker';
import type {TaskChoice} from './tasks/taskChoices';
import {chooseJsonExportDirectory, saveJsonDocuments, type TimelineDocument} from './localJsonExport';
import SubmitProjectDialog from './SubmitProjectDialog';
import ResetAnnotationsDialog from './ResetAnnotationsDialog';
import ClaimProjectDialog, {type ClaimInspection, type ClaimMode} from './ClaimProjectDialog';
import CloudAccountDialog from './CloudAccountDialog';
import SeekSettingsDialog from './SeekSettingsDialog';
import { readSeekSettings, writeSeekSettings, wheelSeekDelta } from './seekSettings';
import type { SeekSettings } from './seekSettings';
import { projectNameError } from './projectNames';
import { useWorkspaceGestures } from './useWorkspaceGestures';
import { usePreview, rememberPreparedPreviews, forgetProjectPreviews } from './usePreview';
import { usePreparation } from './usePreparation';
import { useEditingPresence } from './useEditingPresence';
import { usePreloadNext } from './usePreloadNext';
import { useMediaBuffering } from './useMediaBuffering';
import PreparePanel from './PreparePanel';
import { sessionAssetUrl } from './sessionAssets';
import { readProjectPreference, readRestorableProjectPreference, writeProjectPreference, readRemovedProjects, updateRemovedProjects } from './projectPreferences';
import { advanceVideoTime, applyPlaybackRate, playbackProfile, seekMediaTime, sourceTime } from './playback';
import { formatTime, parseTime, switchCoveredState, applyAnnotationRange, assignSegmentLanes, deleteAnnotation, TRACKS, projectTracks, projectTrackLabels, projectAnnotationLabels, customTrack, trackName, isEventTrack, locateVideo, annotationsEqual, normalizeStateSeams, isSegmentDraftDirty, splitAnnotationLabel, annotationLabel } from './domain';
import { createBoundaryEdit, moveAnnotationBoundary, type AnnotationBoundary, type BoundaryEdit } from './annotationBoundary';
import type { FixedTrack, Project, Segment, Track } from './domain';
import { anchorRange, toggleAnchor } from './anchors';
import TrackLibraryDialog from './TrackLibraryDialog';
import TrackSelectionDialog from './TrackSelectionDialog';
import CacheLocationDialog from './CacheLocationDialog';
type ImportMode = 'new' | 'relink';
type BoundarySelection = {track:Track;id:string;edge:AnnotationBoundary};
type BoundaryInput = 'pointer' | 'key' | 'wheel' | 'single';
type BoundaryGesture = {projectId:string;edit:BoundaryEdit;before:Project['annotations'];input:BoundaryInput};
type RelinkResponse = {project:Project; relinked_count:number; removed_copies:number; warnings?:string[]};
type NasEntry = {name:string; path:string; kind:'directory'|'file'; size?:number};
type NasListing = {root:string; path:string; parent:string|null; entries:NasEntry[]; page:number; has_more:boolean; can_open?:boolean;
 capture?:{collector_name?:string|null; uploader_name?:string|null; uploaded_by_username?:string|null; note?:string|null; capture_date?:string|null}|null};
const oldBackendMessage = '后台仍是旧版本，请关闭所有平台网页，等待约15秒后重新启动。';
const names = {scene:'场景',posture:'姿势',category:'大类'};
async function api<T>(url:string,init?:RequestInit):Promise<T>{const r=await authFetch(url,init);if(!r.ok){let s='操作失败，请重试';try{const j=await r.json();s=typeof j.detail==='string'?j.detail:JSON.stringify(j.detail);}catch{s=`请求失败 (${r.status})`;}throw new Error(s);}return r.json();}
function json(method:string,body:unknown){return {method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)};}
async function requireSourceCapabilities(nativePicker=false){
 const health=await api<{capabilities?:string[]}>('/api/health',{cache:'no-store'});
 const required=['source-local-cache','compact-local-playback','four-axis-annotations','subaxis-annotations',...(nativePicker?['native-file-picker']:[])];
 if(!required.every(capability=>health.capabilities?.includes(capability)))throw new Error(oldBackendMessage);
}
async function requireProjectNaming(){
 const health=await api<{capabilities?:string[]}>('/api/health',{cache:'no-store'});
 if(!health.capabilities?.includes('project-naming'))throw new Error(oldBackendMessage);
}
export default function App({user,onLogout:logoutNow}:{user:Account;onLogout:()=>void}){
 const sharedServer=window.location.protocol==='https:';
 const taskId=taskFromSearch(window.location.search);
 const reviewMode=!!taskId&&new URLSearchParams(window.location.search).get('review')==='1';
 const reviewDirty=useRef(false),reviewBusy=useRef(false);
 const [tasksAvailable,setTasksAvailable]=useState(false),[tasksOpen,setTasksOpen]=useState(new URLSearchParams(window.location.search).get('tasks')==='1');
 const [tasksChecked,setTasksChecked]=useState(false);
 const assignedTaskMode=tasksAvailable&&user.role!=='admin';
 const legacyTrackTools=(user.role==='admin'||tasksChecked)&&canUseLegacyTrackTools(user,tasksAvailable)&&!taskId;
 const [taskErrors,setTaskErrors]=useState<{axis?:string;segment_id?:string;reason?:string}[]>([]);
 const lastProjectKey=`datamark-user-${user.id}-last-project`;
 const removedProjectsKey=`datamark-user-${user.id}-removed-projects`;
 const [removedProjects,setRemovedProjects]=useState<string[]>(()=>readRemovedProjects(removedProjectsKey));
 const previewsClearedKey=`datamark-user-${user.id}-previews-cleared`;
 useWorkspaceGestures();
 const [project,setProject]=useState<Project|null>(null), projectRef=useRef<Project|null>(null);
 const taskReadOnly=reviewMode||!!(project as (Project & {task_readonly?:boolean})|null)?.task_readonly;
 const [adminOpen,setAdminOpen]=useState(false);
 const [passwordOpen,setPasswordOpen]=useState(false);
 const [auditOpen,setAuditOpen]=useState(false);
 const [projects,setProjects]=useState<Project[]>([]),[activeTrack,setActiveTrack]=useState<Track>('scene');
 const [libraryOpen,setLibraryOpen]=useState(false),[selectionOpen,setSelectionOpen]=useState(false);
 const [cacheLocationOpen,setCacheLocationOpen]=useState(false);
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
 const [pendingClaim,setPendingClaim]=useState<ClaimInspection|null>(null),[claimMode,setClaimMode]=useState<ClaimMode>('keep'),[claimStep,setClaimStep]=useState<1|2>(1);
 const [nasPickerKind,setNasPickerKind]=useState<'files'|'directory'|null>(null),[nasListing,setNasListing]=useState<NasListing|null>(null),[nasSelected,setNasSelected]=useState<string[]>([]),[nasLoading,setNasLoading]=useState(false);
 const busyRef=useRef(false),importDialogRef=useRef<HTMLElement>(null),importTriggerRef=useRef<HTMLElement|null>(null);
 const [newProjectName,setNewProjectName]=useState('');
 const postureImportInput=useRef<HTMLInputElement>(null);
 const [settingsOpen,setSettingsOpen]=useState(false),[seekSettings,setSeekSettings]=useState<SeekSettings>(()=>readSeekSettings());
 function saveSeekSettings(value:SeekSettings){setSeekSettings(value);const saved=writeSeekSettings(value);setSettingsOpen(false);setNotice(saved?'进度快捷键设置已保存': '设置已在本次页面生效，但浏览器无法保存；下次打开时可能恢复默认。');}
 const [renameProject,setRenameProject]=useState<Project|null>(null),[renaming,setRenaming]=useState(false),[renameError,setRenameError]=useState('');
 const [deleteProject,setDeleteProject]=useState<Project|null>(null),[deleting,setDeleting]=useState(false),[deleteError,setDeleteError]=useState('');
 const [submitOpen,setSubmitOpen]=useState(false);
 const [resetAnnotationsOpen,setResetAnnotationsOpen]=useState(false);
 const [cloudDialogOpen,setCloudDialogOpen]=useState(false),[cloudDialogError,setCloudDialogError]=useState('');
 const [cloudConnected,setCloudConnected]=useState(false),[cloudSyncError,setCloudSyncError]=useState('');
 const [mockMode,setMockMode]=useState(false),[mockCatalogAvailable,setMockCatalogAvailable]=useState(false),[mockNasAvailable,setMockNasAvailable]=useState(false);
 const [activeStorage,setActiveStorage]=useState<'s3-mock'|'nas'|'s3'>('nas');
 const [mockListing,setMockListing]=useState<NasListing|null>(null),[mockLoading,setMockLoading]=useState(false);
 const [mockUploadOpen,setMockUploadOpen]=useState(false);
 const [mockUploading,setMockUploading]=useState(false);
 const cloudQueue=useRef<Promise<void>>(Promise.resolve());
 const [clearedPreviewId,setClearedPreviewId]=useState('');
 const [error,setError]=useState(''),[notice,setNotice]=useState(''),[saveState,setSaveState]=useState('已保存到平台');
 const [saveFailed,setSaveFailed]=useState(false),saveFailedRef=useRef(false),pendingSaves=useRef(0),saveQueue=useRef<Promise<void>>(Promise.resolve()),serverRevision=useRef(0);
 useEffect(()=>{if(!sharedServer)void api<{connected:boolean}>('/api/cloud/status').then(status=>setCloudConnected(status.connected)).catch(()=>setCloudConnected(false));},[sharedServer]);
 useEffect(()=>{void api<{capabilities:string[];storage_backend?:'s3-mock'|'nas'|'s3'}>('/api/health').then(health=>{setTasksAvailable(health.capabilities.includes('task-management'));setTasksChecked(true);setMockMode(health.capabilities.includes('s3-mock'));setMockCatalogAvailable(health.capabilities.includes('s3-mock-catalog'));setMockNasAvailable(health.capabilities.includes('s3-mock')&&health.capabilities.includes('nas-source-browser'));setActiveStorage(health.storage_backend??(health.capabilities.includes('s3-mock')?'s3-mock':'nas'));}).catch(()=>{});},[]);
 const [selectedId,setSelectedId]=useState<string|null>(null),[label,setLabel]=useState(''),[startText,setStartText]=useState(''),[endText,setEndText]=useState(''),[manual,setManual]=useState(false);
 const [selectedBoundary,setSelectedBoundary]=useState<AnnotationBoundary|null>(null);
 const boundaryRef=useRef<BoundarySelection|null>(null),boundaryGesture=useRef<BoundaryGesture|null>(null);
 const boundaryTimer=useRef<ReturnType<typeof setTimeout>|null>(null);
 const selectedIdRef=useRef<string|null>(null);
 function selectId(value:string|null){selectedIdRef.current=value;setSelectedId(value);}
 const segmentEditorRef=useRef<HTMLDivElement>(null),sidebarRef=useRef<HTMLElement>(null);
 useEffect(()=>{if(manual&&selectedId)revealInPanel(sidebarRef.current,segmentEditorRef.current);},[manual,selectedId]);
 const [anchors,setAnchors]=useState<number[]>([]),[selectedRange,setSelectedRange]=useState<{start_ms:number;end_ms:number}|null>(null);
 const [help,setHelp]=useState(false);
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
 function updateHistory(past:Project['annotations'][],future:Project['annotations'][]){historyRef.current=past;redoRef.current=future;}
 function activateVideo(id:string){videoIdRef.current=id;setVideoId(id);}
 function pausePlayback(){wantsPlay.current=false;videoRef.current?.pause();syncNativeTime();setPlaying(false);}
 function applyPlayerRate(v:HTMLVideoElement){
  const desired=playbackProfile(rateRef.current,wantsPlay.current);
  if(v!==videoRef.current||(Number(v.dataset.timeScale)||1)!==desired.scale)return false;
  applyPlaybackRate(v,desired.nativeRate);return true;
 }
 function playNative(v:HTMLVideoElement){if(!applyPlayerRate(v))return;v.play().catch(()=>{if(videoRef.current!==v||!wantsPlay.current)return;pausePlayback();setError('视频尚未就绪，请稍后点击播放。');});}
 function changeRate(value:number){rateRef.current=value;setRate(value);const v=videoRef.current;if(v)applyPlayerRate(v);}

 const activeSegments=useMemo(()=>assignSegmentLanes(project?.annotations[activeTrack]??[],project?.duration_ms??0),[project?.annotations,project?.duration_ms,activeTrack]);
 const selected=activeSegments.find(s=>s.id===selectedId);
 const eventTrack=!!project&&isEventTrack(project,activeTrack);
 const hasEventDraft=!manual&&eventTrack&&Boolean(label.trim()||(!selectedRange&&(startText.trim()||endText.trim())));
 const hasPendingEdit=hasEventDraft||(manual&&isSegmentDraftDirty(selected,{label,start:startText,end:endText,kind:selected?.kind??'interval'}));
 const preparation=usePreparation(clearedPreviewId===project?.id?undefined:project?.id,project?.playback_generation??0,sharedServer||mockMode);
 const editingPresence=useEditingPresence(project?.id);
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
 const recordingOrigin=useMemo(()=>parseRecordingOrigin(project?.recording_start),[project?.recording_start]);
 const annotationTime=(ms:number)=>recordingDateTime(recordingOrigin,ms)?.slice(11)??formatTime(ms);
 const activeVideo=playbackVideos.find(video=>video.id===videoId);
 useEffect(()=>{if(!project||!preparation.manifest)return;rememberPreparedPreviews(project.id,preparation.manifest.videos);},[project?.id,preparation.manifest]);
 const profile=playbackProfile(rate,playing);
 const preview=usePreview(project?.id,videoId,mediaVersion,sessionEntered&&!prepareOpen&&!deleting&&!project?.deletion_pending,profile.fast,true);
 const playableUrl=project&&preview.state==='ready'&&preview.url?browserVideoUrl(project.id,preview.url):undefined;
 const nextVideo=playbackVideos[playbackVideos.findIndex(video=>video.id===videoId)+1];
 const nextPreviewUrl=sessionEntered&&!prepareOpen&&preparation.status?.state==='ready'&&preview.state==='ready'&&nextVideo?preparation.manifest?.videos.find(video=>video.id===nextVideo.id)?.[profile.fast?'fast_url':'url']:undefined;
 usePreloadNext(project&&nextPreviewUrl?browserVideoUrl(project.id,nextPreviewUrl):undefined);
 const previewPending=!!activeVideo&&preview.state!=='ready'&&preview.state!=='error';
 const previewError=preview.state==='error'?preview.detail||'无法生成视频预览，请重试。':mediaError;
 useEffect(()=>{setShowPreviewWait(false);if(!previewPending)return;const timer=setTimeout(()=>setShowPreviewWait(true),350);return()=>clearTimeout(timer);},[project?.id,videoId,previewPending]);
 useEffect(()=>{if(preview.state==='error')pausePlayback();},[preview.state]);
 const showBuffering=useMediaBuffering(videoRef,wantsPlay,!!activeVideo&&!previewPending&&!previewError&&!scrubbing,`${project?.id}/${videoId}/${mediaVersion}/${profile.fast}`);
 const outputAnnotations=project?Object.entries(project.annotations).filter(([axis])=>axis!=='habit').flatMap(([,segments])=>segments):[];
 const count=outputAnnotations.length;
 const unfinishedCount=outputAnnotations.filter(s=>s.end_ms===null).length;
 const spans=project?.recording_runs??project?.videos??[];
 const fixedLabels=project?projectTrackLabels(project,activeTrack):[];
 const editorLabelOptions=project&&!eventTrack?projectAnnotationLabels(project,activeTrack):[];
 const currentStates=activeSegments.filter(s=>s.start_ms<=time&&(s.end_ms===null||time<s.end_ms));
 const refresh=()=>api<Project[]>('/api/projects').then(setProjects).catch(e=>setError(e.message));
 useEffect(()=>{api<{capabilities:string[]}>('/api/health').then(health=>{if(!taskId&&user.role!=='admin'&&health.capabilities.includes('task-management'))return [];return (taskId?Promise.resolve():requireSourceCapabilities()).then(()=>api<Project[]>('/api/projects'));}).then(ps=>{setProjects(ps);if(taskId&&ps[0]){adopt(ps[0],true);return;}const last=readRestorableProjectPreference(lastProjectKey,previewsClearedKey);if(last&&!readRemovedProjects(removedProjectsKey).includes(last)&&ps.some(p=>p.id===last))api<Project>(`/api/projects/${last}`).then(p=>{if(!projectRef.current&&!readRemovedProjects(removedProjectsKey).includes(p.id)&&readRestorableProjectPreference(lastProjectKey,previewsClearedKey)===p.id)adopt(p,true);}).catch(e=>setError(e.message));}).catch(e=>setError(e.message));},[]);
 useEffect(()=>{if(notice&&!prepareOpen&&!importOpen&&!busy){const t=setTimeout(()=>setNotice(''),9000);return()=>clearTimeout(t);}},[notice,prepareOpen,importOpen,busy]);
 useEffect(()=>{const warn=(e:BeforeUnloadEvent)=>{if(boundaryGesture.current||pendingSaves.current||saveFailedRef.current||busyRef.current||hasPendingEdit||reviewDirty.current||reviewBusy.current){e.preventDefault();e.returnValue='';}};window.addEventListener('beforeunload',warn);return()=>window.removeEventListener('beforeunload',warn);},[hasPendingEdit]);
 function resetEditorFields(){exitBoundary();selectId(null);setLabel('');setStartText('');setEndText('');setManual(false);}
 function clearEditor(){resetEditorFields();if(selectedRange){setStartText(formatTime(selectedRange.start_ms));setEndText(formatTime(selectedRange.end_ms));}}
 function selectTrack(t:Track){if(t===activeTrack){setTrackRevealVersion(value=>value+1);return;}if(hasPendingEdit){setError('请先保存或取消正在编辑的标注，再切换类型；当前输入已保留。');return;}setError('');setTrackRevealVersion(value=>value+1);setActiveTrack(t);resetEditorFields();if(selectedRange){setStartText(formatTime(selectedRange.start_ms));setEndText(formatTime(selectedRange.end_ms));}}
 function setCurrent(ms:number){timeRef.current=ms;setTime(ms);}
 function seekVideo(ms:number){
  const p=projectRef.current;if(!p)return;
  if(wantsPlay.current||videoRef.current?.paused===false)pausePlayback();
  const t=Math.max(0,Math.min(p.duration_ms,Math.round(ms)));
  const ending=p.videos.find(video=>video.end_ms===t);
  const v=ending&&!p.videos.some(video=>video.start_ms===t)?ending:locateVideo(p.videos,t);if(!v)return;
  const snapped=Math.max(v.start_ms,t);setCurrent(boundaryRef.current?t:snapped);
  if(snapped!==t&&!scrubbingRef.current&&!p.continuity_bridges?.some(bridge=>t>=bridge.start_ms&&t<bridge.end_ms))setNotice('此处为录制间隔，已定位到下一段视频');
  const local=(snapped-v.start_ms)/1000;
  // Ordinary scrubbing decodes on release; boundary edits preview every published position.
  if(scrubbingRef.current&&!boundaryRef.current)return;
  if(v.id!==videoIdRef.current){activateVideo(v.id);setMediaError('');}
  // Rapid direction changes can return to the mounted video before React replaces it.
  const player=videoRef.current;
  if(!player||player.dataset.videoId!==v.id||player.readyState===0){pendingSeek.current=local;}
  else{pendingSeek.current=null;seekMediaTime(player,local,Number(player.dataset.timeScale)||1);}
 }
 function seek(ms:number,input:BoundaryInput=scrubbingRef.current?'pointer':'single'){
  if(boundaryRef.current){if(input==='single'){exitBoundary();return;}moveBoundary(ms,input);return;}
  seekVideo(ms);
 }
 function beginScrub(){finishBoundaryGesture();pausePlayback();scrubbingRef.current=true;setScrubbing(true);}
 function endScrub(_cancelled:boolean,targetMs:number){
  if(!scrubbingRef.current)return;
  if(boundaryRef.current){seek(targetMs,'pointer');finishBoundaryGesture();}
  scrubbingRef.current=false;setScrubbing(false);seekVideo(boundaryRef.current?timeRef.current:targetMs);
 }
 function moveCursor(delta:number,input:BoundaryInput='single'){const p=projectRef.current;if(!p||scrubbingRef.current)return;pausePlayback();seek(boundaryRef.current?timeRef.current+delta:advanceVideoTime(p.videos,timeRef.current,delta),input);}
 function toggle(){
  const v=videoRef.current,p=projectRef.current;if(!v||!p||scrubbingRef.current)return;
  exitBoundary();
  if(wantsPlay.current){pausePlayback();return;}
  if(timeRef.current>=p.duration_ms)seek(0);
  wantsPlay.current=true;setPlaying(true);
  if(rateRef.current>10)return; // Render the compressed-media player before starting.
  if(v.readyState>0&&v.dataset.videoId===videoIdRef.current)playNative(v);
 }
 function onReady(){
  const v=videoRef.current;if(prepareOpen||(scrubbingRef.current&&!boundaryRef.current)||!v||v.readyState===0||v.dataset.videoId!==videoIdRef.current)return;
  setMediaError('');
  const scale=Number(v.dataset.timeScale)||1;
  const source=projectRef.current?.videos.find(item=>item.id===videoIdRef.current);if(!source)return;
  if(!applyPlayerRate(v))return;
  const local=pendingSeek.current??Math.max(0,(timeRef.current-source.start_ms)/1000);
  pendingSeek.current=null;seekMediaTime(v,local,scale);
  
  if(wantsPlay.current)playNative(v);
 }
 function syncNativeTime(){
  const v=videoRef.current,p=projectRef.current;
  if(scrubbingRef.current||boundaryRef.current||!v||!p||v.readyState===0||v.seeking||pendingSeek.current!==null||v.dataset.videoId!==videoIdRef.current)return;
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
  if(!applyPlayerRate(v))return;
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
 function queueCloudSync(id:string,final=false){
  if(taskId||sharedServer||mockMode||!cloudConnected)return;
  cloudQueue.current=cloudQueue.current.catch(()=>{}).then(async()=>{
   try{await api(`/api/cloud/projects/${id}/sync`,json('POST',{final}));setCloudSyncError('');}
   catch(error){setCloudSyncError(`本机草稿已保存，但 Ubuntu 同步失败：${(error as Error).message}`);}
  });
 }
 function persist(p:Project,annotations:Project['annotations']){
  pendingSaves.current++;setSaveState('正在保存…');
  saveQueue.current=saveQueue.current.then(async()=>{
   if(saveFailedRef.current)throw new Error('草稿尚未保存，请先重试保存。');
   const saved=await api<Project>(`/api/projects/${p.id}/draft`,json('PUT',{annotations,expected_revision:serverRevision.current}));
   if(projectRef.current?.id===p.id){serverRevision.current=saved.revision;const current=projectRef.current;projectRef.current={...current,revision:saved.revision,updated_at:saved.updated_at,draft_dirty:true,annotations:current.annotations===annotations?saved.annotations:current.annotations};setProject(projectRef.current);}
   queueCloudSync(p.id);
  }).catch((e:Error)=>{if(projectRef.current?.id===p.id){saveFailedRef.current=true;setSaveFailed(true);setError(e.message);}})
   .finally(()=>{pendingSaves.current--;if(projectRef.current?.id===p.id)setSaveState(saveFailedRef.current?'保存失败，改动保留在此页':pendingSaves.current?'正在保存…':'已保存到平台');});
 }
 function finishBoundaryGesture(){
  if(boundaryTimer.current!==null){clearTimeout(boundaryTimer.current);boundaryTimer.current=null;}
  const gesture=boundaryGesture.current;boundaryGesture.current=null;
  const current=projectRef.current;if(!gesture||!current||current.id!==gesture.projectId)return;
  if(annotationsEqual(gesture.before,current.annotations,current.duration_ms)){setSaveState(saveFailedRef.current?'保存失败，改动保留在此页':pendingSaves.current?'正在保存…':'已保存到平台');return;}
  const beforeMerge=current.annotations[gesture.edit.track]?.find(item=>item.id===gesture.edit.id);
  const annotations=taskId?current.annotations:normalizeStateSeams(current.annotations,current.continuity_bridges??[],current.videos,current.custom_tracks,current.duration_ms);
  updateHistory([...historyRef.current.slice(-49),gesture.before],[]);
  const next={...current,annotations};projectRef.current=next;setProject(next);persist(next,annotations);
  if(beforeMerge&&boundaryRef.current){
   const saved=annotations[gesture.edit.track]?.find(item=>item.id===beforeMerge.id)
    ??annotations[gesture.edit.track]?.find(item=>(item.lane??0)===(beforeMerge.lane??0)&&item.label===beforeMerge.label&&item.start_ms<=beforeMerge.start_ms&&(item.end_ms??Infinity)>=(beforeMerge.end_ms??Infinity));
   if(saved){boundaryRef.current={...boundaryRef.current,id:saved.id};selectId(saved.id);setStartText(formatTime(saved.start_ms));setEndText(saved.end_ms===null?'':formatTime(saved.end_ms));seekVideo(boundaryRef.current.edge==='start'?saved.start_ms:saved.end_ms!);}
   else{boundaryRef.current=null;setSelectedBoundary(null);}
  }
 }
 function exitBoundary(){
  finishBoundaryGesture();boundaryRef.current=null;setSelectedBoundary(null);
  if(scrubbingRef.current){scrubbingRef.current=false;setScrubbing(false);}
 }
 function selectBoundary(track:Track,segment:Segment,edge:AnnotationBoundary){
  if(taskReadOnly||busyRef.current||!sessionEntered||track!==activeTrack||segment.id!==selectedId)return false;
  if(hasPendingEdit){setError('请先完成标注或备注输入，再调整边界。');return false;}
  finishBoundaryGesture();const current=projectRef.current;
  const saved=current?.annotations[track]?.find(item=>item.id===selectedIdRef.current);
  if(!current||!saved||!createBoundaryEdit(current,track,saved.id,edge))return false;
  segment=saved;
  pausePlayback();boundaryRef.current={track,id:segment.id,edge};setSelectedBoundary(edge);setSelectedRange(null);setError('');
  seekVideo(edge==='start'?segment.start_ms:segment.end_ms!);return true;
 }
 function moveBoundary(target:number,input:BoundaryInput){
  let selection=boundaryRef.current,current=projectRef.current;
  if(!selection||!current||current.deletion_pending||busyRef.current||taskReadOnly)return;
  if(boundaryGesture.current&&boundaryGesture.current.input!==input){finishBoundaryGesture();selection=boundaryRef.current;current=projectRef.current;if(!selection||!current)return;}
  if(!boundaryGesture.current){
   const edit=createBoundaryEdit(current,selection.track,selection.id,selection.edge);if(!edit){exitBoundary();return;}
   boundaryGesture.current={projectId:current.id,edit,before:current.annotations,input};
  }
  const result=moveAnnotationBoundary(boundaryGesture.current.edit,target);
  const next={...current,annotations:result.annotations};projectRef.current=next;setProject(next);
  setSaveState('正在调整边界…');
  const changed=result.annotations[selection.track]?.find(item=>item.id===selection.id);
  if(changed){setStartText(formatTime(changed.start_ms));setEndText(changed.end_ms===null?'':formatTime(changed.end_ms));}
  seekVideo(result.time);
  if(input==='wheel'){
   if(boundaryTimer.current!==null)clearTimeout(boundaryTimer.current);
   boundaryTimer.current=setTimeout(finishBoundaryGesture,250);
  }else if(input==='single')finishBoundaryGesture();
 }
 function commit(annotations:Project['annotations'],record=true){finishBoundaryGesture();const p=projectRef.current;if(!p||p.deletion_pending||busyRef.current||taskReadOnly)return false;if(taskId){if(JSON.stringify(p.annotations)===JSON.stringify(annotations))return true;if(record)updateHistory([...historyRef.current.slice(-49),p.annotations],[]);const next={...p,annotations};projectRef.current=next;setProject(next);persist(next,annotations);return true;}annotations=normalizeStateSeams(annotations,p.continuity_bridges??[],p.videos,p.custom_tracks,p.duration_ms);if(annotationsEqual(p.annotations,annotations,p.duration_ms))return true;if(record)updateHistory([...historyRef.current.slice(-49),p.annotations],[]);const next={...p,annotations};projectRef.current=next;setProject(next);persist(next,annotations);return true;}
 function choosePostureJson(){
  if(taskId)return;
  if(hasPendingEdit){setError('请先保存或取消正在编辑的标注，再导入姿势 JSON；当前输入已保留。');return;}
  pausePlayback();postureImportInput.current?.click();
 }
 async function importPostureFile(file:File){
  const p=projectRef.current;
  if(!p||p.deletion_pending||busyRef.current||taskId)return;
  if(hasPendingEdit){setError('请先保存或取消正在编辑的标注，再导入姿势 JSON；当前输入已保留。');return;}
  if(pendingSaves.current||saveFailedRef.current){setError('请先完成平台草稿保存，再导入姿势 JSON。');return;}
  pausePlayback();
  let result:PostureImport|null=null;
  await doBusy('正在导入姿势 JSON…',async()=>{
   if(file.size>20*1024*1024)throw new Error('姿势 JSON 文件不能超过 20 MB。');
   const text=await file.text(),current=projectRef.current;
   if(!current||current.id!==p.id)throw new Error('当前项目已切换，请重新选择姿势 JSON。');
   result=importPostureTimeline(text,current);
  });
  const imported=result as PostureImport|null;
  if(!imported||projectRef.current?.id!==p.id||!commit(imported.annotations))return;
  setActiveTrack('posture');clearEditor();setTrackRevealVersion(value=>value+1);setError('');
  setNotice(`已将 ${file.name} 导入姿势轴：${imported.importedCount} 段，共 ${formatTime(imported.importedDurationMs)}${imported.discardedDurationMs?`；舍弃无视频部分 ${formatTime(imported.discardedDurationMs)}`:''}${imported.replacedCount?`；覆盖 ${imported.replacedCount} 条已有主轴标注的重叠部分`:''}。可撤销本次导入。`);
 }
 async function flush(){finishBoundaryGesture();await saveQueue.current;if(saveFailedRef.current)throw new Error('请先重试保存平台草稿，再继续操作。');}
 async function chooseAssignedTask(choice:TaskChoice){
  if(choice.id===taskId&&(choice.kind==='review')===reviewMode)return;
  if(busyRef.current||reviewBusy.current)return;
  if(hasPendingEdit||reviewDirty.current){setError('请先保存或清空未提交的填写，再切换任务。');return;}
  pausePlayback();
  await saveBeforeNavigation(action=>doBusy('正在保存草稿…',action),flush,()=>window.location.assign(choice.url));
 }
 async function returnToTaskManagement(){
  if(busyRef.current||reviewBusy.current)return;
  if(reviewDirty.current){setError('请先提交或清空未提交的复核填写，再返回任务管理。');return;}
  if(hasPendingEdit){setError('请先保存或取消正在编辑的标注，再返回任务管理；当前输入已保留。');return;}
  pausePlayback();
  await saveBeforeNavigation(action=>doBusy('正在保存草稿…',action),flush,()=>window.location.assign('?tasks=1'));
 }
 async function connectCloud(username:string,password:string){
  await doBusy('正在连接 Ubuntu 公网账号…',async()=>{
   try{
    await api('/api/cloud/connect',json('POST',{username,password}));
    setCloudConnected(true);setCloudDialogOpen(false);setCloudDialogError('');setCloudSyncError('');
    const current=projectRef.current;
    if(current){await api(`/api/cloud/projects/${current.id}/sync`,json('POST',{final:false}));setNotice('公网账号已连接，本机项目进度已同步到 Ubuntu。提交后还会将 JSON 写回 NAS。');}
    else setNotice('公网账号已连接。');
   }catch(error){setCloudDialogError((error as Error).message);throw error;}
  });
 }
 async function retryCloudSync(){
  const current=projectRef.current;if(!current)return;
  await doBusy('正在同步 Ubuntu 项目总览…',async()=>{
   await flush();await cloudQueue.current;
   const final=!current.draft_dirty&&!!current.last_writeback;
   await api(`/api/cloud/projects/${current.id}/sync`,json('POST',{final}));
   setCloudSyncError('');setNotice(final?'本机项目结果已写回 NAS 并读回核验。':'本机项目进度已同步到 Ubuntu。');
  });
 }
 async function onLogout(){
  if(busyRef.current||hasPendingEdit){setError('请先完成或取消当前编辑，再退出登录。');return;}
  try{await flush();logoutNow();}catch(error){setError(error instanceof Error?error.message:'草稿尚未保存，暂不能退出登录。');}
 }
 async function retrySave(){await doBusy('正在核对草稿保存状态…',async()=>{await saveQueue.current;const local=projectRef.current;if(!local)return;const remote=await api<Project>(`/api/projects/${local.id}`);if(annotationsEqual(normalizeStateSeams(remote.annotations,remote.continuity_bridges??[],remote.videos,remote.custom_tracks,remote.duration_ms),normalizeStateSeams(local.annotations,remote.continuity_bridges??[],remote.videos,remote.custom_tracks,remote.duration_ms),remote.duration_ms)){serverRevision.current=remote.revision;projectRef.current={...local,name:remote.name,revision:remote.revision,updated_at:remote.updated_at,annotations:remote.annotations};setProject(projectRef.current);saveFailedRef.current=false;setSaveFailed(false);setSaveState('已保存到平台');setNotice('已确认当前标注保存成功');return;}if(remote.revision===serverRevision.current){saveFailedRef.current=false;setSaveFailed(false);persist(local,local.annotations);await saveQueue.current;return;}setSaveState('版本冲突，改动保留在此页');setError('草稿已在其他窗口更新。当前改动仍保留在此页；请点击「保存恢复副本并重新载入」，下载包含本页标注的恢复 JSON 后读取服务器草稿。');});}
 async function recoverAndReload(){await doBusy('正在生成恢复副本并重新载入…',async()=>{await saveQueue.current;const local=projectRef.current;if(!local)return;const remote=await api<Project>(`/api/projects/${local.id}`);const content=JSON.stringify(local,null,2);const blob=new Blob([content],{type:'application/json;charset=utf-8'});const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download=`${local.name.replace(/[<>:"/\\|?*]/g,'_')}-未保存草稿-恢复副本-${Date.now()}.json`;document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),10000);adopt(remote);setNotice('恢复副本已开始下载，已重新载入服务器草稿。原有未保存改动保留在恢复 JSON 中。');});}
 function adopt(p:Project,restore=false){setRemovedProjects(updateRemovedProjects(removedProjectsKey,p.id,false).ids);setClearedPreviewId('');p={...p,annotations:{...p.annotations,category:p.annotations.category??[]}};if(!projectTracks(p).includes(activeTrack))setActiveTrack(projectTracks(p)[0]??'scene');setAnchors([]);setSelectedRange(null);restoreOnReady.current=restore?p.id:null;setSessionEntered(false);setPrepareOpen(!restore);if(!restore)preparation.refresh();scrubbingRef.current=false;setScrubbing(false);writeProjectPreference(previewsClearedKey,null);writeProjectPreference(lastProjectKey,p.id);setMediaVersion(v=>v+1);wantsPlay.current=false;setPlaying(false);videoRef.current?.pause();serverRevision.current=p.revision;projectRef.current=p;setProject(p);setCurrent(0);pendingSeek.current=0;activateVideo(p.videos[0]?.id??'');setMediaError('');resetEditorFields();updateHistory([],[]);saveFailedRef.current=false;setSaveFailed(false);setSaveState('已保存到平台');setError('');queueCloudSync(p.id);if((p as Project & {warnings?:string[]}).warnings?.length)setNotice((p as Project & {warnings:string[]}).warnings.join('；'));}
 async function doBusy(message:string,fn:()=>Promise<void>,inImport=false){if(busyRef.current)return;busyRef.current=true;setBusy(message);setError('');if(inImport)setImportError('');try{await fn();}catch(e){if(inImport)setImportError((e as Error).message);else setError((e as Error).message);}finally{busyRef.current=false;setBusy('');}}
 async function finishImport(p:Project){const requestedName=newProjectName.trim();adopt(p);setPendingClaim(null);setImportOpen(false);if(requestedName&&p.name!==requestedName)setNotice(`这些素材已有项目，已打开「${p.name}」。可在项目下拉中点击改名。`);await refresh();}
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
  if(busyRef.current||taskId)return;
  if(mode==='new'&&hasPendingEdit){setError('请先保存或取消正在编辑的标注，再新建项目；当前输入已保留。');return;}
  if(mode!=='new'&&(!projectRef.current||projectRef.current.deletion_pending))return;
  pausePlayback();importTriggerRef.current=document.activeElement as HTMLElement|null;
  await doBusy('正在检查导入功能…',async()=>{
   await requireSourceCapabilities();
   if(mockMode){const health=await api<{storage_backend?:'s3-mock'|'nas'|'s3'}>('/api/health',{cache:'no-store'});setActiveStorage(health.storage_backend??'s3-mock');}
   setImportMode(mode);setImportError('');setPath('');setNewProjectName('');setPendingClaim(null);setNasPickerKind(null);setNasListing(null);setNasSelected([]);setMockListing(null);setImportOpen(true);
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
  await requireSourceCapabilities();
  if(importMode==='relink'){await relinkSources(sourcePath);return;}
  await flush();
  if(importMode==='new'&&sharedServer&&(activeStorage==='nas'||activeStorage==='s3')){
   const inspection=await api<ClaimInspection>('/api/source-claims/inspect',json('POST',{path:sourcePath}));
   if(inspection.requires_choice){setPendingClaim(inspection);setClaimMode(inspection.has_json?'continue':'keep');setClaimStep(1);return;}
   await finishImport(await api<Project>('/api/storage/projects/open',json('POST',{path:sourcePath,name:newProjectName.trim()})));
  }else await finishImport(await api<Project>(sharedServer||mockMode?'/api/storage/projects/open':'/api/projects/open',json('POST',{path:sourcePath,name:newProjectName.trim()})));
 }
 async function confirmClaim(){
  const claim=pendingClaim;if(!claim)return;
  if(claim.requires_takeover&&claimStep===1){setClaimStep(2);return;}
  await doBusy('正在领取采集目录…',async()=>{
   await finishImport(await api<Project>('/api/storage/projects/open',json('POST',{
    path:claim.path,name:newProjectName.trim(),claim_token:claim.claim_token,claim_mode:claimMode,
    takeover_confirmations:claim.requires_takeover?2:0,
   })));
   setPendingClaim(null);
   setNotice(claim.requires_takeover?'已接管原项目，原认领人不再拥有此项目权限。':claimMode==='restart'?'旧 JSON 已保留恢复副本，当前项目从空白开始。':activeStorage==='s3'?'已载入 S3 中的旧 JSON。':'已载入 NAS 中的旧 JSON。');
  },true);
 }
 async function chooseLocalSources(kind:'files'|'directory'){
  pausePlayback();
  await doBusy(kind==='directory'?'请选择原视频目录…':'请选择原视频文件…',async()=>{
   await checkNewProjectName();
   await requireSourceCapabilities(true);
   const chosen=await api<{paths:string[]}>('/api/local-files/pick',json('POST',{kind}));
   if(!Array.isArray(chosen.paths))throw new Error('未能读取所选路径，请重试或手动填写原目录。');
   if(!chosen.paths.length)return;
   if(kind==='directory'){setBusy(importMode==='relink'?'正在验证原视频并迁移缓存…':'正在读取原视频目录…');await importSourcePath(chosen.paths[0]);return;}
   await importSourceFiles(chosen.paths);
  },true);
 }
 async function importSourceFiles(paths:string[]){
  await requireSourceCapabilities();
  setBusy(`正在读取 ${paths.length} 段原视频的时长…`);
  await flush();
  await finishImport(await api<Project>('/api/projects/files',json('POST',{paths,name:newProjectName.trim()})));
 }
 async function browseMock(prefix=''){
  setMockLoading(true);setImportError('');
  try{setMockListing(await api<NasListing>(`/api/storage/sources?${new URLSearchParams({path:prefix})}`));}
  catch(error){setImportError(error instanceof Error?error.message:String(error));}
  finally{setMockLoading(false);}
 }
 async function importMockFolder(){
  if(!mockListing?.can_open)return;
  await doBusy('正在打开模拟 S3 项目…',async()=>{
   await checkNewProjectName();
   await finishImport(await api<Project>('/api/storage/projects/open',json('POST',{path:mockListing.path,name:newProjectName.trim()})));
  });
 }
 async function browseNas(kind:'files'|'directory', folder?:string, page=0){
  setNasLoading(true);setImportError('');
  try{
   const health=await api<{capabilities?:string[]}>('/api/health',{cache:'no-store'});
   if(!health.capabilities?.includes('nas-source-browser')&&!health.capabilities?.includes('s3-catalog'))throw new Error(oldBackendMessage);
   const query=new URLSearchParams({page:String(page)});if(folder)query.set('path',folder);
   const listing=await api<NasListing>(`/api/storage/sources?${query}`);
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
  if(!importOpen||pendingClaim)return;
  const dialog=importDialogRef.current;dialog?.focus();
  const key=(event:KeyboardEvent)=>{
   if(document.querySelector('[data-s3-upload-dialog]'))return;
   if(event.key==='Escape'){event.preventDefault();if(!busyRef.current&&!mockUploading)setImportOpen(false);return;}
   if(event.key!=='Tab'||!dialog)return;
   const controls=Array.from(dialog.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex="0"]'));
   const first=controls[0],last=controls[controls.length-1];
   if(!first){event.preventDefault();return;}
   if(event.shiftKey&&(document.activeElement===first||document.activeElement===dialog)){event.preventDefault();last.focus();}
   else if(!event.shiftKey&&(document.activeElement===last||document.activeElement===dialog)){event.preventDefault();first.focus();}
  };
  window.addEventListener('keydown',key,true);
  return()=>window.removeEventListener('keydown',key,true);
 },[importOpen,mockUploading,pendingClaim]);
 useEffect(()=>{if(!importOpen)importTriggerRef.current?.focus();},[importOpen]);
 function pauseClearedPreviews(id:string){
  forgetProjectPreviews(id);
  if(projectRef.current?.id!==id)return;
  pausePlayback();setClearedPreviewId(id);setSessionEntered(false);setPrepareOpen(false);setMediaError('');
 }
 async function askRenameProject(id:string){
  if(busyRef.current)return;
  pausePlayback();setRenameError('');
  await doBusy('正在读取项目名称…',async()=>{await flush();setRenameProject(await api<Project>(`/api/projects/${id}`));});
 }
 async function confirmRenameProject(value:string){
  const target=renameProject;if(!target||busyRef.current)return;
  const name=value.trim(),validation=projectNameError(value);if(validation){setRenameError(validation);return;}
  busyRef.current=true;setRenaming(true);setRenameError('');
  try{
   await flush();await requireProjectNaming();
   const revision=projectRef.current?.id===target.id?serverRevision.current:target.revision;
   const saved=await api<Project>(`/api/projects/${target.id}/name`,json('PATCH',{name,expected_revision:revision}));
   const current=projectRef.current;
   if(current?.id===saved.id){
    const next={...current,name:saved.name,revision:saved.revision,updated_at:saved.updated_at};
    serverRevision.current=saved.revision;projectRef.current=next;setProject(next);
   }
   queueCloudSync(saved.id);
   setProjects(items=>items.map(item=>item.id===saved.id?{...item,name:saved.name,revision:saved.revision,updated_at:saved.updated_at}:item));
   setRenameProject(null);setNotice(`项目已重命名为「${saved.name}」`);
  }catch(e){setRenameError((e as Error).message);}finally{busyRef.current=false;setRenaming(false);}
 }
 async function askDeleteProject(id:string){
  if(busyRef.current)return;
  if(projectRef.current?.id===id&&hasPendingEdit){setError('请先完成当前标注或备注输入，再移除本机项目。');return;}
  pausePlayback();setDeleteError('');
  await doBusy('正在检查本机项目…',async()=>{await flush();setDeleteProject(await api<Project>(`/api/projects/${id}`));});
 }
 function closeLocalProject(id:string){
  if(readProjectPreference(lastProjectKey)===id)writeProjectPreference(lastProjectKey,null);
  if(projectRef.current?.id!==id)return;
  pausePlayback();restoreOnReady.current=null;enterAfterSkip.current=null;
  projectRef.current=null;setProject(null);setSessionEntered(false);setPrepareOpen(false);setClearedPreviewId('');
  setAnchors([]);setSelectedRange(null);resetEditorFields();updateHistory([],[]);setCurrent(0);activateVideo('');setMediaError('');
 }
 async function confirmDeleteProject(){
  if(!deleteProject||deleting)return;
  const target=deleteProject;setDeleting(true);setDeleteError('');
  pauseClearedPreviews(target.id);
  try{
   await flush();
   await api(`/api/projects/${target.id}/preview-cache/clear`,json('POST',{confirmed:true,expected_revision:target.revision}));
   await clearBrowserVideoCache(target.id);
   const removed=updateRemovedProjects(removedProjectsKey,target.id,true);setRemovedProjects(removed.ids);
   writeProjectPreference(previewsClearedKey,JSON.stringify({id:target.id,at:Date.now()}));
   closeLocalProject(target.id);setDeleteProject(null);
   setNotice(`已清理「${target.name}」的本机预览，并从本机项目列表移除。原视频、平台标注草稿和已输出 JSON 保留。通过新建项目重新选择原素材可继续。`);
   if(!removed.saved)setError('浏览器无法保存项目列表设置；本次已移除，刷新后项目可能重新显示。');
   await refresh();
  }catch(e){setDeleteError((e as Error).message);}finally{setDeleting(false);}
 }
 useEffect(()=>{
  const removed=(event:StorageEvent)=>{
   if(event.key===removedProjectsKey)setRemovedProjects(readRemovedProjects(removedProjectsKey));
   if(event.key!==previewsClearedKey||!event.newValue)return;
   try{
    const {id}=JSON.parse(event.newValue);setRemovedProjects(readRemovedProjects(removedProjectsKey));
    if(projectRef.current?.id===id&&(hasPendingEdit||saveFailedRef.current||pendingSaves.current)){
     pausePlayback();forgetProjectPreviews(id);setClearedPreviewId(id);setPrepareOpen(false);
     setMediaError('本机预览已在其他窗口清理，请先保存右侧输入，再重新选择素材。');
     setNotice('其他窗口已清理本机预览并移除项目；此页保留尚未保存的输入，请先完成保存。');return;
    }
    pauseClearedPreviews(id);
    closeLocalProject(id);setDeleteProject(null);setNotice('其他窗口已清理本机预览，并从本机列表移除项目。');
   }catch{}
  };
  window.addEventListener('storage',removed);return()=>window.removeEventListener('storage',removed);
 },[hasPendingEdit]);
 async function chooseProject(id:string){if(!id||projectRef.current?.id===id)return;if(hasPendingEdit){setError('请先保存或取消正在编辑的标注，再切换项目；当前输入已保留。');return;}await doBusy('正在打开草稿…',async()=>{await flush();adopt(await api<Project>(`/api/projects/${id}`));});}
 async function saveTrackSelection(ids:string[]){
  if(taskId)return;
  const current=projectRef.current;if(!current)return;
  await flush();
  const updated=await api<Project>(`/api/projects/${current.id}/track-selection`,json('PUT',{tracks:ids,expected_revision:serverRevision.current}));
  serverRevision.current=updated.revision;projectRef.current=updated;setProject(updated);
  // Old undo snapshots also need empty entries for newly attached axes.
  const complete=(value:Project['annotations'])=>({...Object.fromEntries(Object.keys(updated.annotations).map(axis=>[axis,[]])),...value});
  updateHistory(historyRef.current.map(complete),redoRef.current.map(complete));
  if(!projectTracks(updated).includes(activeTrack)){setActiveTrack(projectTracks(updated)[0]??'scene');resetEditorFields();setSelectedRange(null);}
  setTrackRevealVersion(value=>value+1);setSelectionOpen(false);setNotice('项目时间轴选择已保存，未勾选轴的标注仍保留。');
  queueCloudSync(updated.id);
 }
 function selectSegment(track:Track,s:Segment,seekTime=s.start_ms){
  exitBoundary();
  const current=projectRef.current?.annotations[track]?.find(item=>item.id===s.id)
   ??projectRef.current?.annotations[track]?.find(item=>(item.lane??0)===(s.lane??0)&&item.label===s.label&&item.start_ms<=s.start_ms&&(item.end_ms??Infinity)>=(s.end_ms??Infinity));
  if(!current)return;s=current;
  if(hasPendingEdit){if(track===activeTrack&&s.id===selectedId){seek(seekTime);return;}setError('请先保存或取消正在编辑的标注，再选择其他记录；当前输入已保留。');return;}
  setActiveTrack(track);setSelectedRange(null);selectId(s.id);setLabel(s.label);setStartText(formatTime(s.start_ms));setEndText(s.end_ms===null?'':formatTime(s.end_ms));setManual(true);seek(seekTime);
 }
 function addAnchor(){
  if(!project)return;pausePlayback();const point=timeRef.current;
  setAnchors(previous=>toggleAnchor(previous,point));if(selectedRange)cancelRange();
 }
 function selectAnchorRange(){
  const range=anchorRange(anchors);if(!range)return;if(manual&&hasPendingEdit){setError('请先保存或取消正在编辑的标注，再选择锚点区间。');return;}
  if(!spans.some(span=>Math.max(range.start_ms,span.start_ms)<Math.min(range.end_ms,span.end_ms))){setError('锚点之间没有可标注的视频。');return;}
  pausePlayback();const draftLabel=!manual&&eventTrack?label:'';resetEditorFields();setLabel(draftLabel);setSelectedRange(range);setStartText(formatTime(range.start_ms));setEndText(formatTime(range.end_ms));setNotice('已选中两锚点之间的区间，请在标注面板选择内容。');
 }
 function dismissTimelineRange(){setSelectedRange(null);if(!manual&&!label.trim()){setStartText('');setEndText('');}}
 function cancelRange(){setSelectedRange(null);if(!manual){setStartText('');setEndText('');}}
 function editStart(value:string){setSelectedRange(null);setStartText(value);}
 function editEnd(value:string){setSelectedRange(null);setEndText(value);}
 function clearAfterAnnotation(){
  setSelectedRange(null);resetEditorFields();
 }
 function changeState(value:string){
  exitBoundary();
  if(!project)return;
  if(hasPendingEdit){setError('请先保存或取消正在编辑的标注，再切换状态；当前输入已保留。');return;}
  if(!selectedRange&&time>=project.duration_ms){setError('已到视频末尾，请先定位到要切换状态的位置。');return;}
  try{
   const next=selectedRange?applyAnnotationRange(activeSegments,selectedRange.start_ms,selectedRange.end_ms,value,spans,activeTrack,undefined,true,true):switchCoveredState(activeSegments,time,value,project.duration_ms,spans,activeTrack==='category');
   if(!commit({...project.annotations,[activeTrack]:next}))return;
   setError('');clearAfterAnnotation();setNotice(selectedRange?'已标注选中区间，选区已取消':'主轴状态已更新');
  }catch(error){setError((error as Error).message);}
 }
 function saveSegment(){
  exitBoundary();
  if(!project)return;const range=selectedRange;
  const a=range?.start_ms??parseTime(startText),b=range?.end_ms??(endText.trim()?parseTime(endText):null);
  if(a===null||a<0||a>project.duration_ms||b===null||b<=a||b>project.duration_ms){setError('请填写有效时间：0 ≤ 开始 < 结束 ≤ 时间线总时长。');return;}
  if(!label.trim()){setError('请选择或填写标注内容。');return;}
  if(!splitAnnotationLabel(label,editorLabelOptions).base.trim()){setError('请先填写标注内容，再添加备注。');return;}
  if(!eventTrack&&label.length>80){setError('标签和备注合计不能超过 80 个字符。');return;}
  if(!spans.some(v=>Math.max(a,v.start_ms)<Math.min(b,v.end_ms))){setError('这个时间段没有视频，请重新选择。');return;}
  try{
  const next=applyAnnotationRange(activeSegments,a,b,label.trim(),spans,activeTrack,undefined,!eventTrack,!!range);
  if(!commit({...project.annotations,[activeTrack]:next}))return;
  setError('');clearAfterAnnotation();
  setNotice('标注已保存，录制断档保持留空');
  }catch(error){setError((error as Error).message);}
 }
 function replaceSelectedLabel(value:string){
  finishBoundaryGesture();
  const current=projectRef.current,original=current?.annotations[activeTrack]?.find(item=>item.id===selectedIdRef.current);
  if(!current||!original)return;
  const nextLabel=value.trim();
  if(!splitAnnotationLabel(nextLabel,editorLabelOptions).base.trim()){setError('标注内容不能为空。');return;}
  if(nextLabel.length>(eventTrack?500:80)){setError(`标签和备注合计不能超过 ${eventTrack?500:80} 个字符。`);return;}
  const next=current.annotations[activeTrack].map(item=>item.id===original.id?{...item,label:nextLabel}:item);
  if(!commit({...current.annotations,[activeTrack]:next}))return;
  // Adjacent equal labels can merge. Follow the resulting block while keeping its lane.
  const saved=projectRef.current!.annotations[activeTrack].find(item=>item.id===original.id)
   ??projectRef.current!.annotations[activeTrack].find(item=>(item.lane??0)===(original.lane??0)&&item.label===nextLabel&&item.start_ms<=original.start_ms&&(item.end_ms??Infinity)>=(original.end_ms??Infinity));
  if(saved){selectId(saved.id);setLabel(saved.label);setStartText(formatTime(saved.start_ms));setEndText(saved.end_ms===null?'':formatTime(saved.end_ms));if(boundaryRef.current)boundaryRef.current={...boundaryRef.current,id:saved.id};}
  setError('');
 }
 function remove(){exitBoundary();const p=projectRef.current,id=selectedIdRef.current;if(!p||!id)return;commit({...p.annotations,[activeTrack]:deleteAnnotation(p.annotations[activeTrack],id,spans,activeTrack,eventTrack)});clearEditor();}
 function undo(){if(busyRef.current||taskReadOnly)return;if(hasPendingEdit){setError('请先保存或取消正在编辑的标注，再撤销；当前输入已保留。');return;}exitBoundary();const past=historyRef.current,p=projectRef.current;if(!past.length||!p)return;updateHistory(past.slice(0,-1),[...redoRef.current.slice(-49),p.annotations]);commit(past[past.length-1],false);clearEditor();}
 function redo(){if(busyRef.current||taskReadOnly)return;if(hasPendingEdit){setError('请先保存或取消正在编辑的标注，再恢复；当前输入已保留。');return;}exitBoundary();const future=redoRef.current,p=projectRef.current;if(!future.length||!p)return;updateHistory([...historyRef.current.slice(-49),p.annotations],future.slice(0,-1));commit(future[future.length-1],false);clearEditor();}
 async function exportFiles(){
  if(taskId)return;
  const current=projectRef.current;if(!current)return;
  if(hasPendingEdit){setError('请先完成标注或备注输入，再导出。');return;}
  pausePlayback();
  await doBusy('请选择 JSON 保存文件夹…',async()=>{
   const directory=await chooseJsonExportDirectory();if(!directory)return;
   await flush();setBusy('正在保存独立时间轴 JSON…');
   const result=await api<{documents:TimelineDocument[]}>(`/api/projects/${current.id}/export-documents`);
   const count=await saveJsonDocuments(directory,result.documents);
   setNotice(`已将 ${count} 个独立 JSON 保存到「${directory.name}」并读回核验。`);
  });
 }
 async function submitTask(){
  if(!taskId)return;
  await doBusy('正在校验并提交任务…',async()=>{
   await flush();
   const response=await authFetch('/api/tasks/items/'+taskId+'/submit',json('POST',{expected_revision:serverRevision.current,request_id:crypto.randomUUID()}));
   const body=await response.json();
   if(!response.ok){
    const issues=body.detail?.errors??body.detail?.issues??(Array.isArray(body.detail)?body.detail:[]);
    setTaskErrors(issues);
    const issue=issues[0];const current=projectRef.current;
    if(issue&&current){const record=current.annotations[issue.axis]?.find(item=>item.id===issue.segment_id);if(record)selectSegment(issue.axis,record);}
    throw new Error(typeof body.detail==='string'?body.detail:issues.map((issue:{reason:string})=>issue.reason).join('；')||'任务校验失败');
   }
   const saved=await api<Project>('/api/projects/'+projectRef.current!.id);
   serverRevision.current=saved.revision;projectRef.current=saved;setProject(saved);clearEditor();
   setNotice('任务已提交，当前版本已锁定。'+(body.output_sync&&body.output_sync.state!=='ready'?' 采集目录结果尚未同步完成，请点击同步按钮重试。':''));
  });
 }
 async function writeback(){
  if(taskId)return;
  const current=projectRef.current;if(!current)return;
  await doBusy(current.storage_mode==='s3-mock'?'正在保存到 S3 mock…':'正在写回 timeline 文件夹…',async()=>{
   await flush();
   const health=await api<{capabilities?:string[]}>('/api/health',{cache:'no-store'});
   if(!health.capabilities?.includes('selected-track-writeback'))throw new Error('后台尚未支持按勾选时间轴写回，请关闭平台并重新启动后再试。');
   const written=await api<{paths:string[]}>(`/api/projects/${current.id}/writeback`,json('POST',{expected_revision:serverRevision.current}));
   setSubmitOpen(false);
   const savedProject=await api<Project>(`/api/projects/${current.id}`);
   if(projectRef.current?.id===current.id){projectRef.current=savedProject;setProject(savedProject);}
   if(!sharedServer&&!mockMode){
    try{
     await cloudQueue.current;
     await api(`/api/cloud/projects/${current.id}/sync`,json('POST',{final:true}));
     setCloudSyncError('');
    }catch(error){
     setCloudSyncError(`本机标注文件已写回，但 NAS 写回或同步失败：${(error as Error).message}`);
     setNotice('标注文件已写回本机采集目录的 timeline 文件夹。');
     return;
    }
   }
   await refresh();
   setNotice(current.storage_mode==='s3'?`${written.paths.length} 个 JSON 已保存到 S3 并读回核验。`:current.storage_mode==='s3-mock'?`${written.paths.length} 个 JSON 已保存到 S3 mock 并读回核验。`:
    `${written.paths.length} 个 JSON 已写回原采集目录的 timeline 文件夹。${!sharedServer?'NAS 副本已写入并读回核验。':''}`);
  });
 }
 async function resetAnnotationsFromEmptySource(){
  const current=projectRef.current;if(!current)return;
  await doBusy('正在保存旧草稿并清空标注…',async()=>{
   await flush();
   const saved=await api<Project & {recovery_id:string}>(`/api/projects/${current.id}/annotations/reset-from-empty-source`,json('POST',{expected_revision:serverRevision.current}));
   if(projectRef.current?.id!==current.id)throw new Error('当前项目已切换，请重新打开。');
   serverRevision.current=saved.revision;projectRef.current=saved;setProject(saved);
   updateHistory([],[]);setAnchors([]);setSelectedRange(null);resetEditorFields();
   setResetAnnotationsOpen(false);setSaveState('已保存到平台');
   setNotice('旧草稿已在平台保存恢复副本，当前标注已清空，可以重新标注。');
   await refresh();
  });
 }
 useEffect(()=>{
  const key=(e:KeyboardEvent)=>{
   if(document.querySelector('[data-s3-upload-dialog]'))return;
   if(!projectRef.current||!sessionEntered||busy||importOpen||prepareOpen||deleteProject||deleting||libraryOpen||selectionOpen||adminOpen||passwordOpen||auditOpen||renameProject||submitOpen||resetAnnotationsOpen||cloudDialogOpen||settingsOpen||cacheLocationOpen||e.isComposing||e.keyCode===229)return;
   const target=e.target instanceof Element?e.target:null;
   if(target?.closest('[data-workspace-menu]'))return;
   if(e.key==='Escape'&&boundaryRef.current){e.preventDefault();e.stopImmediatePropagation();exitBoundary();return;}
   const editingInput=!!target?.closest('input,textarea,select,[contenteditable]:not([contenteditable="false"])');
   if(boundaryRef.current&&editingInput)return;
   if(e.code==='Space'&&(e.ctrlKey||e.metaKey)&&!e.altKey&&!e.shiftKey){
    e.preventDefault();e.stopImmediatePropagation();if(!e.repeat)addAnchor();return;
   }
   if(e.key==='Delete'&&!e.ctrlKey&&!e.metaKey&&!e.altKey&&!e.shiftKey){
    const target=e.target instanceof Element?e.target:null;
   if(target?.closest('[data-workspace-menu]'))return;
    if(target?.closest('input,textarea,select,[contenteditable]:not([contenteditable="false"])'))return;
    if(selectedId){e.preventDefault();e.stopImmediatePropagation();if(!e.repeat)remove();}return;
   }
   // Reserve transport keys before buttons, sliders, selects and text inputs
   // receive them. In particular, Space must never click a focused annotation.
   if(['Space','ArrowLeft','ArrowRight'].includes(e.code)){
    e.preventDefault();e.stopImmediatePropagation();transportKeys.current.add(e.code);
    if(e.code==='Space'){if(!e.repeat)toggle();}
    else moveCursor((e.code==='ArrowLeft'?-1:1)*(e.ctrlKey?seekSettings.ctrlArrowMs:e.shiftKey?seekSettings.shiftArrowMs:seekSettings.arrowMs),'key');
    return;
   }
   if((e.key==='Home'||e.key==='End')&&!editingInput){e.preventDefault();e.stopImmediatePropagation();transportKeys.current.add(e.code);seek(e.key==='Home'?0:projectRef.current.duration_ms,'key');return;}
   if((e.ctrlKey||e.metaKey)&&!e.altKey&&e.key.toLowerCase()==='z'){
    e.preventDefault();e.stopImmediatePropagation();if(!e.repeat){if(e.shiftKey)redo();else undo();}
   }
  };
  const wheel=(e:WheelEvent)=>{
   if(document.querySelector('[data-s3-upload-dialog]'))return;
   if(e.target instanceof Element&&e.target.closest('[data-workspace-menu]'))return;
   const delta=wheelSeekDelta(e,seekSettings);if(delta===null)return;
   // Capture before timeline zoom, horizontal browsing or native Shift scrolling.
   e.preventDefault();e.stopImmediatePropagation();
   if(!projectRef.current||!sessionEntered||busy||importOpen||prepareOpen||deleteProject||deleting||libraryOpen||selectionOpen||adminOpen||passwordOpen||auditOpen||renameProject||submitOpen||resetAnnotationsOpen||cloudDialogOpen||settingsOpen||cacheLocationOpen||scrubbingRef.current)return;
   if(e.target instanceof Element&&boundaryRef.current&&e.target.closest('input,textarea,select,[contenteditable]:not([contenteditable="false"])'))return;
   if(delta!==0)moveCursor(delta,'wheel');
  };
  const release=(e:KeyboardEvent)=>{if(transportKeys.current.delete(e.code)){e.preventDefault();e.stopImmediatePropagation();if(boundaryGesture.current?.input==='key'&&![...transportKeys.current].some(code=>['ArrowLeft','ArrowRight','Home','End'].includes(code)))finishBoundaryGesture();}};
  const blur=()=>{transportKeys.current.clear();exitBoundary();};
  const outside=(e:PointerEvent)=>{if(boundaryRef.current&&(!(e.target instanceof Element)||!e.target.closest('.annotation-panel,.annotation-editor,[data-boundary-keep]')))exitBoundary();};
  window.addEventListener('keydown',key,true);window.addEventListener('keyup',release,true);window.addEventListener('blur',blur);window.addEventListener('pointerdown',outside,true);window.addEventListener('wheel',wheel,{capture:true,passive:false});
  return()=>{window.removeEventListener('keydown',key,true);window.removeEventListener('keyup',release,true);window.removeEventListener('blur',blur);window.removeEventListener('pointerdown',outside,true);window.removeEventListener('wheel',wheel,true);};
 });
 const latestAnchor=anchors.at(-1),availableAnchorRange=anchorRange(anchors);
 const anchorInterval=availableAnchorRange?formatTime(availableAnchorRange.end_ms-availableAnchorRange.start_ms).split('.')[0]:'--:--:--';
 const anchorControls=<div className="anchor-toolbar" title={anchors.map(value=>`锚点：${annotationTime(value)}`).join("；")}><button className="secondary" title="添加或移除当前位置的锚点，最多保留最近两个（Ctrl + 空格）" onClick={addAnchor}><Bookmark size={14}/>添加锚点</button><button className="secondary anchor-pair-button" aria-label={`选中区间，间隔 ${anchorInterval}`} aria-pressed={!!selectedRange} disabled={!availableAnchorRange} onClick={selectAnchorRange}><ScanLine size={14}/><span>选中区间</span><strong>{anchorInterval}</strong></button>{anchors.length>0&&<div className="anchor-metrics"><output className="anchor-duration" aria-label="当前时间到前一锚点的间隔" title="当前时间到最近一个锚点的时长">距前锚点 {formatTime(Math.abs(time-(latestAnchor??time))).split('.')[0]}</output></div>}{anchors.length>0&&<button className="text-button anchor-clear" aria-label="清空全部锚点" title="一次清空全部锚点" onClick={()=>{setAnchors([]);cancelRange();}}><Trash2 size={14}/></button>}</div>;
 const taskFeedback=(project as (Project & {task_feedback?:import('./tasks/types').TaskIssue[]})|null)?.task_feedback??[];
 const taskBar=taskId&&<><div className="task-workspace-bar"><button className="secondary" disabled={!!busy} onClick={()=>void returnToTaskManagement()}>返回任务管理</button><span>{reviewMode?'复核工作台：提交版本只读，复核结论在右侧填写。':taskReadOnly?'该提交版本只读，返修时会创建新版本。':'任务草稿独立保存；允许空白和子轴重叠。'}</span>{!taskReadOnly&&<button className="primary" disabled={hasPendingEdit||pendingSaves.current>0||saveFailed||!!busy} onClick={()=>void submitTask()}>提交任务</button>}</div>{!reviewMode&&<TaskOutputSync taskId={taskId} status={(project as (Project&{task_output_sync?:import('./tasks/TaskOutputSync').TaskOutputStatus})|null)?.task_output_sync}/>}{taskFeedback.length>0&&<details className="task-rework-notes" open><summary>返修意见 · {taskFeedback.length} 处</summary>{taskFeedback.map(issue=><button className="secondary" key={issue.id} onClick={()=>{const record=project?.annotations[issue.axis]?.find(item=>item.id===issue.segment_id);if(record)selectSegment(issue.axis,record);else if(issue.start_ms!==undefined)seek(issue.start_ms);}}>{project?trackName(project,issue.axis):issue.axis}{issue.start_ms!==undefined?` · ${formatTime(issue.start_ms)}`:''}：{issue.reason}</button>)}</details>}</>;
 function previewReviewVersion(version:TaskVersion){
  const current=projectRef.current;if(!current)return;
  const annotations=Object.fromEntries(Object.keys(current.annotations).map(axis=>[axis,version.annotations[axis]??[]]));
  const next={...current,annotations};projectRef.current=next;setProject(next);clearEditor();updateHistory([],[]);
 }
 const taskFields=taskId&&project&&!taskReadOnly&&<TaskFields project={project} axes={(project as Project & {task_rule?:{axes:import('./tasks/types').TaskAxis[]}}).task_rule?.axes} selectedId={selectedId} onChange={annotations=>{commit(annotations);setTaskErrors([]);}} onSelect={(axis,id)=>{const record=project.annotations[axis]?.find(item=>item.id===id);if(record)selectSegment(axis,record);}} errors={taskErrors.map(error=>({...error,message:error.reason??'标注不符合规则'}))}/>;
 const workspaceTools=<>
  {!reviewMode&&<section className="sidebar-module project-management" aria-label="项目管理">
   <div className="panel-heading"><h2>项目管理</h2></div>
   <div className="module-content">
    {(assignedTaskMode||!!taskId)&&<TaskPicker user={user} currentTask={taskId} review={reviewMode} disabled={!!busy} onChoose={choice=>void chooseAssignedTask(choice)}/>}
    {!taskId&&!assignedTaskMode&&tasksChecked&&<ProjectPicker projects={projects.filter(item=>!removedProjects.includes(item.id))} currentId={project?.id??null} disabled={!!busy||deleting||renaming} canRename={user.role==='admin'} showStorageMode={mockMode&&mockNasAvailable} onChoose={id=>void chooseProject(id)} onRename={id=>void askRenameProject(id)} onRemove={id=>void askDeleteProject(id)} onCreate={()=>void beginImport('new')}/>}
    <button className="secondary prepare-action" title="检查全部精简播放缓存、封面和悬停图片" onClick={openPreparation} disabled={!project||!!busy||preparation.busy}>{preparation.status?.state==='ready'?<Check size={15}/>:preparation.status?.state==='running'&&preparation.status.requested?<LoaderCircle className="spin" size={15}/>:<Film size={15}/>}<span>{!project?'尚未选择项目':preparation.status?.checking?'检查缓存中':preparation.status?.state==='ready'?'本机播放就绪':preparation.status?.state==='running'&&preparation.status.requested?`准备中 ${preparation.status.ready}/${preparation.status.total}`:'准备全部预览'}</span></button>
   </div>
  </section>}
  {reviewMode&&<section className="sidebar-module task-review-task" aria-label="当前复核任务"><div className="module-content"><TaskPicker user={user} currentTask={taskId} review disabled={!!busy} onChoose={choice=>void chooseAssignedTask(choice)}/></div></section>}
  {!taskId&&!assignedTaskMode&&<section className="sidebar-module annotation-output" aria-label="标注输出">
   <div className="panel-heading"><h2>标注输出</h2></div>
   <div className="module-content output-actions">
    <input ref={postureImportInput} type="file" accept=".json,application/json" aria-label="导入姿势 JSON 文件" hidden onChange={event=>{const file=event.target.files?.[0];event.target.value='';if(file)void importPostureFile(file);}}/>
    <button className="primary" disabled={!project||!!busy||!projectTracks(project).length||(!project.source_dir&&project.storage_mode!=='s3-mock'&&project.storage_mode!=='s3')} title={project?.storage_mode==='s3'?'按版本保存到 S3 采集目录的 timeline 文件夹':project?.storage_mode==='s3-mock'?'保存到 S3 模拟采集目录的 timeline 文件夹':project?.source_dir?`写回原采集目录的 timeline 文件夹：${project.source_dir}`:'需要原采集目录才能写回'} onClick={()=>{if(hasPendingEdit){setError('请先完成标注或备注输入，再导出。');return;}pausePlayback();setSubmitOpen(true);}}><Save size={15}/>导出 JSON 到云端</button>
    <button className="secondary" disabled={!project||!!busy} title="选择本机保存文件夹，保存独立 JSON；同名文件将覆盖" onClick={exportFiles}><Download size={15}/>导出 JSON 到本地</button>
    <button className="secondary posture-import-action" disabled={!project||!sessionEntered||!!busy||pendingSaves.current>0||saveFailed||project.deletion_pending||!projectTracks(project).includes('posture')} title="按 JSON 时间戳导入姿势主轴，支持撤销" onClick={choosePostureJson}><Upload size={15}/>导入姿势 JSON</button>
    {project&&unfinishedCount>0&&<div className="output-status" role="status"><span>{unfinishedCount} 项事件未结束</span></div>}
   </div>
  </section>}
 </>;
 return <div data-task-review={reviewMode?'true':undefined} data-task-workspace={taskId?'true':undefined} className={'app-shell '+(user.role==='admin'?'is-admin':'is-annotator')+(sessionEntered&&activeVideo?' video-workspace':'')}>
  {tasksAvailable&&tasksOpen&&<TaskPanel user={user} onClose={()=>setTasksOpen(false)} onOpenTask={id=>{window.location.assign('?task='+id);}}/>}
  {adminOpen&&(tasksAvailable?<TaskAccounts onClose={()=>setAdminOpen(false)}/>:<AdminPanel s3Mode={activeStorage==='s3'} projects={projects} mockMode={mockMode} onClose={()=>setAdminOpen(false)}/>)}
  {passwordOpen&&<PasswordDialog onClose={()=>setPasswordOpen(false)} onChanged={logoutNow}/>}
  {auditOpen&&project&&<HistoryPanel projectId={project.id} onClose={()=>setAuditOpen(false)}/>}
  {legacyTrackTools&&libraryOpen&&<TrackLibraryDialog admin={user.role==='admin'} onClose={()=>setLibraryOpen(false)}/>}
  {legacyTrackTools&&selectionOpen&&project&&<TrackSelectionDialog project={project} onClose={()=>setSelectionOpen(false)} onSave={saveTrackSelection}/>}
  {cacheLocationOpen&&<CacheLocationDialog project={project} onClose={()=>setCacheLocationOpen(false)} onChanged={updated=>{if(updated&&projectRef.current?.id===updated.id){serverRevision.current=updated.revision;projectRef.current={...projectRef.current,collection_folder_name:updated.collection_folder_name,revision:updated.revision,updated_at:updated.updated_at};setProject(projectRef.current);}pausePlayback();setSessionEntered(false);preparation.refresh();setNotice('缓存位置已设置，请重新准备播放素材。');}}/>}
  {settingsOpen&&<SeekSettingsDialog value={seekSettings} onCancel={()=>setSettingsOpen(false)} onSave={saveSeekSettings}/>}
  {renameProject&&<RenameProjectDialog name={renameProject.name} saving={renaming} error={renameError} onCancel={()=>{setRenameProject(null);setRenameError('');}} onConfirm={confirmRenameProject}/>}
  {submitOpen&&project&&<SubmitProjectDialog projectName={project.name} trackNames={projectTracks(project).map(axis=>trackName(project,axis))} submitting={!!busy} remoteProject={sharedServer} mockProject={project.storage_mode==='s3-mock'} s3Project={project.storage_mode==='s3'} onCancel={()=>setSubmitOpen(false)} onConfirm={()=>void writeback()}/>}
  {resetAnnotationsOpen&&project&&<ResetAnnotationsDialog projectName={project.name} resetting={!!busy} onCancel={()=>setResetAnnotationsOpen(false)} onConfirm={()=>void resetAnnotationsFromEmptySource()}/>}
  {cloudDialogOpen&&<CloudAccountDialog busy={!!busy} error={cloudDialogError} onCancel={()=>setCloudDialogOpen(false)} onConnect={(username,password)=>void connectCloud(username,password)}/>}
  {deleteProject&&<DeleteProjectDialog project={deleteProject} deleting={deleting} error={deleteError} onCancel={()=>{setDeleteProject(null);setDeleteError('');}} onConfirm={confirmDeleteProject}/>}
  <header className="topbar" inert={importOpen||!!busy||!!deleteProject||!!renameProject||submitOpen||resetAnnotationsOpen||cloudDialogOpen||prepareOpen||settingsOpen||cacheLocationOpen||adminOpen||passwordOpen}><div className="brand"><img src="/favicon.svg" alt=""/><span>DataMark</span></div><h1 className="compact-project-name" title={project?`${project.name} · ${project.videos.length} 段视频 · 录制起点 ${project.recording_start??'未设置'}`:'视频标注工作台'}>{project?.name??'视频标注工作台'}</h1><div className="top-actions">{legacyTrackTools&&<button className="secondary" aria-haspopup="dialog" onClick={()=>{pausePlayback();setLibraryOpen(true);}}><Layers3 size={16}/>时间轴库</button>}<span className="save-indicator" role="status" title={project?saveState:'就绪'}><span className={saveFailed?'status-dot bad':'status-dot'}/>{project?saveState:'就绪'}</span><span>{user.display_name}</span>{!taskId&&!sharedServer&&!mockMode&&<button className='quiet' onClick={()=>{setCloudDialogError('');setCloudDialogOpen(true);}}>{cloudConnected?'Ubuntu 已连接':'连接 Ubuntu 账号'}</button>}{tasksAvailable&&!taskId&&<button className="quiet" onClick={()=>{pausePlayback();setTasksOpen(true);}}>任务管理</button>}{user.role==='admin'&&!taskId&&<button className="quiet" onClick={()=>setAdminOpen(true)}>账号管理</button>}<button className="quiet" disabled={hasPendingEdit||pendingSaves.current>0||saveFailed||!!busy} title="请先保存或取消当前编辑" onClick={()=>setPasswordOpen(true)}>修改密码</button><button className="quiet" onClick={()=>void onLogout()}>退出登录</button><button className="quiet icon-button workspace-fullscreen-button" aria-label="切换工作台全屏" title="工作台全屏（全屏时按 Esc 退出）" onClick={toggleWorkspaceFullscreen}><Maximize className="workspace-fullscreen-enter" size={16}/><Minimize className="workspace-fullscreen-exit" size={16}/></button><button className="quiet icon-button" aria-label="操作帮助" title="操作帮助" onClick={()=>setHelp(!help)}><Keyboard size={16}/></button><button className="quiet" disabled={hasPendingEdit||pendingSaves.current>0||saveFailed||!!busy} onClick={()=>{pausePlayback();setCacheLocationOpen(true);}}>缓存位置</button><button className="quiet icon-button" aria-label="进度快捷键设置" title="设置视频进度快捷键" aria-haspopup="dialog" onClick={()=>{pausePlayback();setHelp(false);setSettingsOpen(true);}}><Settings size={16}/></button></div></header>{taskBar}
  {project&&(editingPresence.others.length>0||editingPresence.error)&&<div className="editing-presence-warning" role="status">{editingPresence.others.length>0?`正在同时编辑：${editingPresence.others.map(item=>item.display_name).join('、')}。保存时如有冲突，平台会保留本页改动并提示恢复。`:editingPresence.error}</div>}
  {project?.source_dir&&project.warnings?.some(warning=>warning.includes('原目录 JSON 已被修改'))&&<div className="editing-presence-warning" role="status"><span>原目录的时间轴文件已变化，平台保留了尚未写回的草稿。若已主动删除 NAS 中的时间轴文件，可以保存旧草稿后重新标注。</span><button className="secondary" disabled={!!busy} onClick={()=>{if(hasPendingEdit){setError('请先保存或取消正在编辑的标注，再清空旧标注。');return;}pausePlayback();setResetAnnotationsOpen(true);}}>清空旧标注并重来</button></div>}
  {!taskId&&!sharedServer&&project&&(cloudSyncError||!cloudConnected)&&<div className="editing-presence-warning" role="status">{cloudSyncError||'本机项目尚未连接 Ubuntu 公网账号；管理员暂时看不到进度。'}{cloudConnected&&<button className="secondary" disabled={!!busy} onClick={()=>void retryCloudSync()}>重新同步</button>}</div>}
  {error&&<div className="message error" role="alert"><span>{error}</span><button aria-label="关闭提示" onClick={()=>setError('')}><X size={16}/></button></div>}
  {notice&&<div className="message success" role="status"><Check size={16}/><span>{notice}</span><button aria-label="关闭提示" onClick={()=>setNotice('')}><X size={16}/></button></div>}
  {project?.needs_source_relink&&user.role==='admin'&&<div className="source-relink-notice" inert={importOpen||!!busy||!!deleteProject||!!renameProject||submitOpen||cloudDialogOpen||prepareOpen||settingsOpen}><FolderOpen size={16}/><span>此项目仍使用平台内的视频副本。关联原目录后，缓存会移到原视频旁，释放重复存储。</span><button className="secondary" disabled={!!busy||project.deletion_pending} onClick={()=>beginImport('relink')}>关联原目录</button></div>}
  {help&&<div className="help-panel"><Keyboard size={18}/><span>顶部全屏按钮 进入工作台全屏，Esc 退出</span><span>空格 播放／暂停</span><span>Ctrl + 空格 添加／移除锚点，最多保留两个</span><span>点击「选中区间」后选一个标签，从主轴向下逐层合并同内容、填入空白，冲突部分放到下一层</span><span>← → 前后 {seekSettings.arrowMs/1000} 秒</span><span>Shift + ← → 前后 {seekSettings.shiftArrowMs/1000} 秒</span><span>Ctrl + ← → 前后 {seekSettings.ctrlArrowMs/1000} 秒</span><span>Ctrl + Alt + 滚轮 上退下进，每次 {seekSettings.wheelMs/1000} 秒</span><span>Shift + 滚轮 上退下进，每次 {seekSettings.shiftWheelMs/1000} 秒</span><span>右上角设置按钮 可调整移动步长</span><span>调整视频进度会暂停，松开后保持暂停</span><span>Ctrl + 滚轮 缩放时间轴（最细每屏1分钟）</span><span>Ctrl + + / - / 0 已禁用浏览器缩放</span><span>普通滚轮 上下查看主轴和子轴</span><span>Alt + 滚轮 左右浏览时间轴</span><span>拖动蓝色光标定位视频</span><span>Delete 删除选中色块</span><span>Ctrl + Z 撤销</span><span>Ctrl + Shift + Z 恢复</span><span>点击轴名或右侧类型切换；点击色块可编辑备注。左右键和空格固定控制视频。</span></div>}
  <main className="editor-grid" inert={importOpen||!!busy||!!deleteProject||!!renameProject||submitOpen||resetAnnotationsOpen||cloudDialogOpen||prepareOpen||settingsOpen}>
  {project&&!sessionEntered?<section className="session-start-screen"><Film size={34}/><h2>{clearedPreviewId===project.id?'本机预览已清理':restoreOnReady.current===project.id?'正在载入项目':'预览尚未就绪'}</h2><p>{clearedPreviewId===project.id?'项目和标注草稿仍在。需要继续这个项目时，再主动准备预览；也可以从右侧新建项目选择新素材。':restoreOnReady.current===project.id?(preparation.status?.detail||'正在检查已有播放素材。'):'需要先准备视频、封面和悬停图片，才能进入标注工作台。'}</p><button className="primary" onClick={openPreparation}>{clearedPreviewId===project.id?'重新准备此项目预览':'查看素材准备'}<ArrowRight size={16}/></button></section>:<section className="left-column"><div className="preview-panel"><div className="panel-heading"><div><Film size={16}/><h2>视频预览</h2></div><span className="muted-text">{activeVideo?`${project!.videos.findIndex(v=>v.id===videoId)+1} / ${project!.videos.length} · ${activeVideo.name}`:'等待选择素材'}</span></div>
   <VideoStage empty={!project}>{project&&activeVideo?<><video key={`${project.id}-${videoId}-${mediaVersion}-${profile.fast}`} ref={videoRef} data-video-id={activeVideo.id} data-time-scale={profile.scale} crossOrigin={playableUrl&&!playableUrl.startsWith('blob:')?(activeStorage==='s3'?'anonymous':separateApiOrigin?'use-credentials':undefined):undefined} src={playableUrl} poster={activeVideo.thumbnail_url?sessionAssetUrl(project.id,activeVideo.thumbnail_url):undefined} muted={muted||profile.fast} playsInline preload="auto" onLoadedMetadata={e=>{if(e.currentTarget===videoRef.current){onReady();}}} onRateChange={e=>{if(e.currentTarget===videoRef.current)applyPlayerRate(e.currentTarget);}} onPlaying={e=>{if(e.currentTarget===videoRef.current&&wantsPlay.current){applyPlayerRate(e.currentTarget);setPlaying(true);}}} onPlay={e=>{if(e.currentTarget!==videoRef.current)return;if(!wantsPlay.current)e.currentTarget.pause();else setPlaying(true);}} onPause={e=>{if(e.currentTarget===videoRef.current&&!wantsPlay.current)setPlaying(false);}} onTimeUpdate={e=>{if(e.currentTarget===videoRef.current)syncNativeTime();}} onEnded={e=>{if(e.currentTarget===videoRef.current)ended();}} onError={e=>{if(e.currentTarget!==videoRef.current||preview.state!=='ready')return;pausePlayback();setMediaError('预览读取失败，请检查素材可读性后重试。');}} onClick={toggle}/><div className="video-tag"><span className="live-dot"/> {profile.fast?'高倍速预览 · 本机缓存':'流畅预览 · 270p'}</div>{previewPending&&(preview.state!=='idle'||showPreviewWait)&&!previewError&&<div className="media-cover preview-preparation" role="status"><LoaderCircle className="spin"/><p>{preview.state==='running'?(profile.fast?'正在准备流畅快进预览':'正在生成此片段的预览'):preview.state==='queued'?'此片段正在排队':'正在读取预览缓存'}</p>{preview.state==='running'&&preview.progress!==null&&<><progress aria-label="预览生成进度" max={100} value={preview.progress}/><strong>{Math.floor(preview.progress)}%</strong></>}<small>{preview.detail||(preview.state==='running'?'仅首次准备，完成后可直接复用缓存，并提前准备后续片段':'可以继续浏览时间轴和其他片段')}</small></div>}{showBuffering&&!previewPending&&!previewError&&<div className="media-buffering" role="status"><LoaderCircle className="spin" size={14}/>正在缓冲…</div>}{scrubbing&&<div className="media-scrubbing">{selectedBoundary?"调整边界 · 实时预览":"拖动定位 · 松开后显示目标画面"}</div>}{previewError&&<div className="media-cover"><p>{previewError}</p><button className="secondary" onClick={()=>{pendingSeek.current=Math.max(0,(timeRef.current-activeVideo.start_ms)/1000);setMediaError('');forgetProjectPreviews(project.id);setMediaVersion(v=>v+1);setSessionEntered(false);setPrepareOpen(true);void preparation.start(project.id,true);}}>重新加载</button></div>}</>:<div className="empty-preview"><div className="empty-film"><Film size={34}/><span>＋</span></div><h2>选择素材，开始标注</h2><p>{mockMode?'选择测试视频，新建模拟 S3 项目。上传后会生成标注预览。':activeStorage==='s3'?'选择已上传至 S3 的采集缓存目录。':sharedServer?'从已挂载的 NAS 选择完整采集目录。':'选择本机视频文件或原视频目录。'}{!mockMode&&<> <br/>原视频留在原位置，系统会准备标注用预览。</>}</p><button className="primary" onClick={()=>beginImport('new')}><Upload size={17}/>新建项目并选择素材</button><span className="format-hint">支持 MP4 · AVI · MOV · MKV · WEBM</span></div>}</VideoStage>
   <div className="player-controls"><div className="play-controls"><button className="icon-button" aria-label="后退1秒" disabled={!project} onClick={()=>moveCursor(-1000)}><SkipBack size={17}/></button><button className="play-button" aria-label={playing?'暂停':'播放'} disabled={!project} onClick={toggle}>{playing?<Pause size={19}/>:<Play size={19} fill="currentColor"/>}</button><button className="icon-button" aria-label="前进1秒" disabled={!project} onClick={()=>moveCursor(1000)}><SkipForward size={17}/></button><span className="player-time" title="播放进度 / 总时长">{formatTime(time)}<b>/</b><span>{formatTime(project?.duration_ms??0)}</span></span></div><div className="play-options"><PlaybackSpeed value={rate} onChange={changeRate}/><button className="icon-button" aria-label={muted?'开启声音':'静音'} onClick={()=>setMuted(!muted)}>{muted?<VolumeX size={17}/>:<Volume2 size={17}/>}</button><button className="icon-button" aria-label="全屏视频" disabled={!project} onClick={()=>videoRef.current?.requestFullscreen()}><Maximize size={17}/></button></div></div>
  </div>
  {project?<Timeline project={playbackProject!} currentTime={time} activeTrack={activeTrack} trackRevealVersion={trackRevealVersion} selectedId={selectedId} selectedBoundary={selectedBoundary} onBoundarySelect={selectBoundary} onBoundaryDismiss={exitBoundary} anchors={anchors} selectedRange={selectedRange} anchorControls={anchorControls} onRangeDismiss={dismissTimelineRange} onSeek={seek} onScrubStart={beginScrub} onScrubEnd={endScrub} onTrackSelect={selectTrack} onSegmentSelect={selectSegment}/>:<div className="empty-timeline"><div className="panel-heading"><div><Layers3 size={16}/><h2>标注时间线</h2></div><span>导入视频后开始标注</span></div>{(['scene','posture','category'] as const).map((t,i)=><div className={'placeholder-track '+t} key={t}><span><i/>{names[t]}</span><div><span>{['室内 / 室外 / 半开放 / 隧道 / 电梯 / 交通工具内 / Unknown','坐 / 站 / 躺 / 移动 / 骑行 / 交通工具坐 / 交通工具站 / 交通工具躺 / Unknown','工作学习 / 运动锻炼 / 饮食 / 出行 / 社交 / 休闲娱乐 / 休息 / 日常事务 / Unknown'][i]}</span></div></div>)}</div>}
  </section>}
  <aside className="workspace-sidebar" ref={sidebarRef}>{workspaceTools}{!reviewMode&&sessionEntered&&project&&projectTracks(project).length>0&&<section className="annotation-panel" inert={taskReadOnly}><div className="panel-heading"><div><span className="heading-mark"/><h2>标注面板</h2></div><div className="panel-heading-actions"><span className="small-badge">{count} 条</span><button className="icon-button" aria-label="查看项目操作记录" title="查看项目操作记录" onClick={()=>setAuditOpen(true)}><History size={15}/></button></div></div><div className="track-tabs" role="tablist" aria-label="标注轨道">{projectTracks(project).map(t=><button key={t} role="tab" aria-selected={activeTrack===t} className={activeTrack===t?'active '+(customTrack(project,t)?'custom':t):''} onClick={()=>selectTrack(t)}><i/>{trackName(project,t)}</button>)}{legacyTrackTools&&<button className="add-custom-track" aria-label="选择时间轴" title="选择本项目使用的时间轴" disabled={!!busy||hasPendingEdit||pendingSaves.current>0||saveFailed} onClick={()=>{pausePlayback();setSelectionOpen(true);}}><Layers3 size={14}/>选择时间轴</button>}</div>
   <div className="annotation-content"><div className="section-caption"><span>{trackName(project,activeTrack)}标注</span><span className="accent-text">可留空 · 支持多状态</span></div>
    {selectedRange&&<div className="selected-range-card"><span>选中区间</span><strong>{annotationTime(selectedRange.start_ms)} → {annotationTime(selectedRange.end_ms)}</strong><button className="text-button" onClick={cancelRange}>取消选区</button></div>}
    {selected&&<p className="input-hint">在下方编辑模块修改当前色块的标签和备注。</p>}
    {selectedRange&&<p className="input-hint anchor-range-hint">选一个标签保存：从主轴向下逐层合并相同内容、填入空白，冲突部分才放到下一层。</p>}
    {!eventTrack?<>
     <p className="instruction">{selectedRange?'点击一个标签，保存选中区间。':!activeSegments.some(segment=>segment.lane===0)?'选择初始标签，覆盖主轴全部已录制时间；也可用两锚点选择区间。':'在变化发生的位置，点击按钮切换主轴状态。'}<br/>各时间轴均可留空；锚点区间自动安排主轴和子轴。</p>
     <div className={'state-buttons '+(customTrack(project,activeTrack)?'custom':activeTrack)}>{fixedLabels.map(s=><button key={s} disabled={!project} className={(eventTrack?label===s:currentStates.some(state=>state.label===s))?'chosen':''} onClick={()=>eventTrack?setLabel(s):changeState(s)}><strong>{s}</strong></button>)}</div>
     {!fixedLabels.length&&<p className="input-hint">此项目的时间轴副本没有标签，请从时间轴库选择其他时间轴。</p>}
     <div className="current-label"><i/>{currentStates.length?`当前位置：${[...new Set(currentStates.map(state=>state.label))].join('＋')}`:'当前位置尚未标注'}</div>
    </>:<>
     <p className="instruction">先输入内容或先标时间都可以。<br/>填写开始和结束时间；锚点区间自动安排主轴和子轴。</p>
     {fixedLabels.length>0&&<div className="state-buttons event-label-buttons" aria-label="事件快捷标签">{fixedLabels.map(value=><button key={value} className={label===value?'chosen':''} disabled={manual} onClick={()=>setLabel(value)}>{value}</button>)}</div>}
     {!manual&&<><label className="field-label" htmlFor="behavior-label">事件内容</label><input id="behavior-label" placeholder="例如：抽烟、喝咖啡、使用手机" value={label} maxLength={500} onChange={e=>setLabel(e.target.value)} disabled={!project}/><div className="capture-fields"><label>开始时间<input aria-label="事件开始时间" placeholder="00:00:00.000" readOnly={!!selectedRange} value={startText} onChange={e=>editStart(e.target.value)}/><button className="secondary" disabled={!project||!!selectedRange} onClick={()=>editStart(formatTime(time))}>标记开始</button></label><label>结束时间<input aria-label="事件结束时间" placeholder="00:00:00.000" readOnly={!!selectedRange} value={endText} onChange={e=>editEnd(e.target.value)}/><button className="secondary" disabled={!project||!!selectedRange} onClick={()=>editEnd(formatTime(time))}>标记结束</button></label></div><button className="primary wide" disabled={!project||!label.trim()||!startText||!endText} onClick={saveSegment}><Check size={15}/>保存标注</button><p className="input-hint">{selectedRange?'按两锚点确定区间；取消选区后可手动修改时间。':'跨越录制间隔的标注会在断档处分段保存。'}</p></>}
    </>}
   </div>
  </section>}{!reviewMode&&sessionEntered&&project&&projectTracks(project).length===0&&<section className="annotation-panel" inert={taskReadOnly}><div className="panel-heading"><h2>标注面板</h2></div><p className="instruction">{legacyTrackTools?'当前项目没有勾选时间轴。已有标注保留，可重新选择时间轴继续标注。':'当前项目没有可用时间轴，请联系管理员检查标注规则。'}</p>{legacyTrackTools&&<button className="primary wide" disabled={!!busy||pendingSaves.current>0||saveFailed} onClick={()=>{pausePlayback();setSelectionOpen(true);}}><Layers3 size={15}/>选择时间轴</button>}</section>}
  {!reviewMode&&(!project||!sessionEntered)&&<section className="sidebar-module annotation-panel"><div className="panel-heading"><h2>标注面板</h2></div><p className="module-placeholder">{project?'播放素材就绪后开始标注。':'选择项目后开始标注。'}</p></section>}
  {!reviewMode&&<section className="sidebar-module annotation-editor" aria-label="编辑标注" inert={taskReadOnly}>
   <div className="panel-heading"><h2>编辑标注</h2>{manual&&<button className="icon-button" aria-label="关闭编辑标注" onClick={clearEditor}><X size={15}/></button>}</div>
   {sessionEntered&&project&&manual&&selected?<div className="module-content segment-editor" ref={segmentEditorRef}>
    <div className="section-caption"><span>{trackName(project,activeTrack)} · {selected.lane?`子轴 ${selected.lane}`:'主轴'}</span><button className="danger-icon" aria-label="删除此标注" title="删除此标注（Delete）" onClick={remove}><Trash2 size={15}/></button></div>
    <label className="field-label" htmlFor="segment-label">标注内容</label>
    <select id="segment-label" aria-label="编辑状态内容" value={splitAnnotationLabel(label,editorLabelOptions).base} onChange={event=>replaceSelectedLabel(annotationLabel(event.target.value,splitAnnotationLabel(label,editorLabelOptions).note))}>
     {!fixedLabels.includes(splitAnnotationLabel(label,editorLabelOptions).base)&&<option value={splitAnnotationLabel(label,editorLabelOptions).base}>{splitAnnotationLabel(label,editorLabelOptions).base}（历史标签）</option>}{fixedLabels.map(value=><option key={value}>{value}</option>)}
    </select>
    <label className="field-label" htmlFor="segment-note">备注</label>
    <input id="segment-note" aria-label="这段标注的备注" placeholder="输入备注，按 Enter 或离开输入框保存" maxLength={80} value={splitAnnotationLabel(label,editorLabelOptions).note}
     onChange={event=>setLabel(annotationLabel(splitAnnotationLabel(label,editorLabelOptions).base,event.target.value))}
     onBlur={()=>replaceSelectedLabel(label)}
     onKeyDown={event=>{if(event.key==='Enter'&&!event.nativeEvent.isComposing&&event.nativeEvent.keyCode!==229){event.preventDefault();replaceSelectedLabel(label);}}}/>
    <p className="input-hint">选择标签立即替换；备注按 Enter 或离开输入框保存。<br/>{selectedBoundary?`已选中${selectedBoundary==='start'?'左':'右'}边界；拖动竖线或用快捷键调整，点击时间轴其他位置或 Esc 退出。`:'再次点击色块边缘，可拖动或用进度快捷键调整区间。'}</p>
    <div className="annotation-author" role="status">创建人 {selected.created_by_name??'未知（旧标注）'}{selected.updated_by_name&&<> · 最近编辑 {selected.updated_by_name}</>}</div>
   </div>:<p className="module-placeholder">点击时间轴上的标注色块进行编辑。</p>}
  </section>}
  {sessionEntered&&taskFields}{reviewMode&&taskId&&project&&<TaskWorkbenchReview taskId={taskId} user={user} time={time} onSeek={seek} onPreview={previewReviewVersion} onDirty={value=>{reviewDirty.current=value;}} onBusy={value=>{reviewBusy.current=value;}} selection={selected?{axis:activeTrack,start_ms:selected.start_ms,end_ms:selected.end_ms??selected.start_ms}:undefined}/>}</aside></main>
  {saveFailed&&<div className="save-recovery" role="alert" inert={importOpen||!!busy||!!deleteProject||!!renameProject||submitOpen||cloudDialogOpen||prepareOpen||settingsOpen}><span>{saveState}</span><button className="text-button" disabled={!!busy} onClick={retrySave}>重试保存</button><button className="text-button" disabled={!!busy} title="先下载包含本页未保存改动的恢复 JSON，再读取服务器草稿" onClick={recoverAndReload}>保存恢复副本并重新载入</button></div>}
  {importOpen&&<div className="modal-backdrop" inert={!!pendingClaim} onClick={()=>!busy&&!mockUploading&&setImportOpen(false)}><section ref={importDialogRef} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby="import-title" className={'import-modal'+(nasPickerKind||mockListing?' nas-picker-open':'')} onClick={e=>e.stopPropagation()}>
   <button className="modal-close icon-button" aria-label="关闭导入" disabled={!!busy||mockUploading} onClick={()=>setImportOpen(false)}><X size={20}/></button><div className="modal-icon"><FolderOpen size={24}/></div>
   <h2 id="import-title">{mockMode?'新建采集项目':importMode==='relink'?'关联原视频目录':'新建采集项目'}</h2>
   {mockMode&&activeStorage==='s3-mock' ? <>
    <p>本机上传程序会打开系统文件选择窗口，请从采集设备的 video 文件夹选择 AVI，填写采集人后由本机压缩并发布。新数据位于 daily、采集日期、“月日＋采集人”文件夹；进入该文件夹后选择项目。已有桌面测试目录仍可使用。</p>
    <label className="field-label" htmlFor="mock-project-name">项目名称</label>
    <input id="mock-project-name" className="project-name-input" value={newProjectName} disabled={!!busy} maxLength={80} onChange={e=>setNewProjectName(e.target.value)}/>
    {user.can_upload&&<><button className="secondary wide" disabled={!!busy||mockUploading} onClick={()=>setMockUploadOpen(value=>!value)}>{mockUploadOpen?'收起上传':'上传 AVI 视频'}</button>
      {mockUploadOpen&&<MockUploadPanel account={user} onPublished={prefix=>{setMockUploadOpen(false);void browseMock(prefix);}} onRunningChange={setMockUploading}/>}</>}
    {mockCatalogAvailable&&<><button className="upload-zone" disabled={!!busy||mockLoading} onClick={()=>void browseMock()}><FolderOpen size={23}/><strong>浏览模拟 S3 项目文件夹</strong><span>按采集目录层级逐级选择</span></button>
    {mockListing&&<div className="nas-browser"><div className="nas-browser-heading"><strong>模拟 S3：{mockListing.path||'采集数据'}</strong><button className="secondary" disabled={!!busy||mockLoading} onClick={()=>setMockListing(null)}>关闭</button></div>{mockListing.capture&&<p className="nas-browser-path">采集人：{mockListing.capture.collector_name||'未记录'} · 上传人：{mockListing.capture.uploader_name||'未记录'} · 操作账号：{mockListing.capture.uploaded_by_username||'未记录'}{mockListing.capture.note?` · 备注：${mockListing.capture.note}`:''}</p>}<div className="nas-browser-list">{mockListing.parent!==null&&<button disabled={!!busy||mockLoading} onClick={()=>void browseMock(mockListing.parent??'')}>↑ 上一级</button>}{mockListing.entries.map(entry=><div className="nas-browser-row" key={entry.path}>{entry.kind==='directory'?<button disabled={!!busy||mockLoading} onClick={()=>void browseMock(entry.path)}><FolderOpen size={15}/>{entry.name}</button>:<span>{entry.name}{entry.size!==undefined?` · ${(entry.size/1024/1024/1024).toFixed(2)} GB`:''}</span>}</div>)}</div>{mockListing.can_open&&<button className="primary wide" disabled={!!busy||mockLoading||!!projectNameError(newProjectName,false)} onClick={()=>void importMockFolder()}>使用此项目文件夹</button>}</div>}</>}
    {!mockCatalogAvailable&&<p className="import-error" role="alert">模拟存储目录不可用，请检查本机服务。</p>}
    {importError&&<p className="import-error" role="alert">{importError}</p>}
   </> : <>
   {activeStorage==='s3'&&user.can_upload&&importMode==='new'&&<S3UploadEntry/>}
   {importMode==='new'&&<div className="project-name-field"><label className="field-label" htmlFor="new-project-name">项目名称</label><input id="new-project-name" className="project-name-input" value={newProjectName} disabled={!!busy} placeholder="留空则使用所选文件夹名" autoComplete="off" aria-describedby="new-project-name-hint" aria-invalid={!!projectNameError(newProjectName,false)} onChange={e=>setNewProjectName(e.target.value)}/><p id="new-project-name-hint" className={projectNameError(newProjectName,false)?'import-error':'input-hint'}>{projectNameError(newProjectName,false)?projectNameError(newProjectName,false):sharedServer||mockMode?'领取完整采集目录，标注文件写回目录中的 timeline/；最多 80 个字符':'选择视频文件时，留空仍按导入时间命名；最多 80 个字符'}</p></div>}
   {importMode!=='new'&&<div className="relink-target"><span>当前项目</span><strong>{project?.name}</strong><small>已有 {project?.videos.length??0} 段视频</small></div>}
   <p>{importMode==='relink'?<>选择最初的原视频目录，验证匹配后迁移已有缓存并清理平台内的视频副本。<br/>标注、撤销记录和当前输入都会保留。</>:<>识别文件名中的录制时间，自动排序。<br/>保留视频之间的真实录制间隔。</>}</p>
   {activeStorage==='s3'?<div className="source-storage-note"><strong>S3 保存压缩缓存，原视频保留本机</strong><span>本机上传工具压缩后直接上传到 S3，并在桌面 dataMarkProject 保留同一份缓存。标注结果按版本保存在 S3 的 timeline/。</span></div>:<div className="source-storage-note"><strong>原视频保留原目录，不复制</strong><span>预览缓存存放在原目录的 <code>.datamark-cache</code> 子文件夹，并附有 README.md 用途说明。标注草稿保存在平台内。</span><small>{sharedServer||mockMode?'请保持 NAS 挂载可访问。':'使用移动硬盘、SD 卡或共享目录时，请保持该位置可访问。'}</small></div>}
   {importError&&<p className="import-error" role="alert">{importError}</p>}
   <div className="prepare-import-option"><Check size={17}/><span><strong>先准备全部播放素材，再开始标注</strong><small>{activeStorage==='s3'?'从 S3 下载并校验缓存，保存到本机授权的 dataMarkProject 目录。':sharedServer?'Ubuntu 生成并校验预览，保存到 NAS 后供此电脑下载。':'复用原目录中已有的有效缓存，本机保存精简播放素材。'}</small></span></div>
   <div className={'source-pickers'+(importMode==='relink'?' single':'')}>
    {importMode!=='relink'&&(!sharedServer&&!mockMode)&&<button className="upload-zone" disabled={!!busy||nasLoading} onClick={()=>sharedServer||mockMode?void browseNas('files'):void chooseLocalSources('files')}><Film size={23}/><strong>选择视频文件</strong><span>支持一次多选，可导出 JSON</span></button>}
    <button className="upload-zone" disabled={!!busy||nasLoading} onClick={()=>sharedServer||mockMode?void browseNas('directory'):void chooseLocalSources('directory')}><FolderOpen size={23}/><strong>{activeStorage==='s3'?'选择 S3 缓存目录':'选择原视频目录'}</strong><span>{activeStorage==='s3'?'浏览 S3 已上传缓存，支持写回标注':sharedServer||mockMode?'浏览 NAS，支持写回标注':importMode==='relink'?'匹配已有视频，迁移缓存':'直接读取目录，支持写回标注'}</span></button>
   </div>
   {sharedServer&&nasPickerKind&&nasListing&&<div className="nas-browser"><div className="nas-browser-heading"><strong>{activeStorage==='s3'?'选择 S3 采集缓存目录':nasPickerKind==='files'?'选择 NAS 视频文件':'选择 NAS 原视频目录'}</strong><button className="secondary" disabled={nasLoading||!!busy} onClick={()=>{setNasPickerKind(null);setNasListing(null);setNasSelected([]);}}>关闭</button></div><div className="nas-browser-path" title={nasListing.path}>{nasListing.path}</div><div className="nas-browser-list">{nasListing.parent&&<button disabled={nasLoading||!!busy} onClick={()=>void browseNas(nasPickerKind,nasListing.parent??undefined)}>↑ 上一级</button>}{nasListing.entries.map(entry=><div className="nas-browser-row" key={entry.path}>{entry.kind==='directory'?<button disabled={nasLoading||!!busy} onClick={()=>void browseNas(nasPickerKind,entry.path)}><FolderOpen size={15}/>{entry.name}</button>:nasPickerKind==='files'?<label><input type="checkbox" checked={nasSelected.includes(entry.path)} disabled={nasLoading||!!busy} onChange={event=>setNasSelected(previous=>event.target.checked?[...previous,entry.path]:previous.filter(value=>value!==entry.path))}/>{entry.name}</label>:<span>{entry.name}</span>}</div>)}{!nasListing.entries.length&&<p>此目录没有可选择的视频或子目录。</p>}</div>{nasPickerKind==='files'&&<p className="input-hint">单独选视频可预览和导出 JSON；需要写回 NAS 时请选完整原视频目录。</p>}<div className="nas-browser-actions">{nasListing.page>0&&<button className="secondary" disabled={nasLoading||!!busy} onClick={()=>void browseNas(nasPickerKind,nasListing.path,nasListing.page-1)}>上一页</button>}{nasListing.has_more&&<button className="secondary" disabled={nasLoading||!!busy} onClick={()=>void browseNas(nasPickerKind,nasListing.path,nasListing.page+1)}>下一页</button>}<button className="primary" disabled={nasLoading||!!busy||(nasPickerKind==='files'&&!nasSelected.length)} onClick={()=>void importNasSelection()}>{nasPickerKind==='directory'?'使用此目录':`导入已选 ${nasSelected.length} 个视频`}</button></div></div>}
   {!sharedServer&&!mockMode&&<div className="divider"><span>或直接填写本机 / 内网路径</span></div>}
   <label className="field-label" htmlFor="source-path">{importMode==='relink'||(sharedServer||mockMode)&&importMode==='new'?'原视频目录路径':'视频文件或目录路径'}</label>
   <input id="source-path" disabled={!!busy} value={path} placeholder={activeStorage==='s3'?'daily/日期/采集目录':sharedServer||mockMode?'填写服务可访问的 NAS 完整路径':'粘贴原视频所在目录的完整路径'} onChange={e=>setPath(e.target.value)} onKeyDown={e=>{if(e.key==='Enter')openPath();}}/>
   <p className="input-hint">{importMode==='relink'?'请提供包含这些原视频的目录；校验成功后才会清理对应副本。':activeStorage==='s3'?'请选择已上传的 S3 采集目录；本机 AVI 请通过本机上传工具压缩后发布。':sharedServer||mockMode?'请输入服务可访问的 NAS 采集目录。':'可选择文件或目录，也可填写本机可访问的完整路径。'}</p>
   <button className="primary wide" disabled={!path.trim()||!!busy} onClick={openPath}><FolderOpen size={16}/>{importMode==='relink'?'关联并迁移缓存':'读取并新建项目'}</button>
   <p className="privacy-note"><span className="status-dot"/>直接读取所选路径，不上传或复制原视频</p>
   </>}
  </section></div>}
  {importOpen&&pendingClaim&&<ClaimProjectDialog claim={pendingClaim} storageName={activeStorage==='s3'?'S3':'NAS'} mode={claimMode} step={claimStep} busy={!!busy} error={importError} onMode={setClaimMode} onNext={()=>void confirmClaim()} onBack={()=>setClaimStep(1)} onCancel={()=>{setPendingClaim(null);setImportError('');}}/>}
  {prepareOpen&&project&&<PreparePanel projectName={project.name} status={preparation.status} error={preparation.error} cacheLocation={preparation.manifest?.cache_location?.root??undefined} onCacheLocation={()=>setCacheLocationOpen(true)} busy={preparation.busy} skippedVideos={project.skipped_videos} onSkipFailed={ids=>changeSkippedVideos(ids)} onRestoreSkipped={()=>changeSkippedVideos()} onRetry={()=>void openPreparation()} onEnter={()=>{if(preparation.status?.state==='ready'&&preparation.manifest&&!preparation.error){setSessionEntered(true);setPrepareOpen(false);}}} onClose={()=>{enterAfterSkip.current=null;setPrepareOpen(false);}}/>}
  {busy&&<div className="busy-overlay" role="status"><LoaderCircle className="spin" size={28}/><strong>{busy}</strong><span>{mockMode?'测试视频处理可能需要一些时间':'大文件或内网素材可能需要一些时间'}</span></div>}
 </div>;
}
