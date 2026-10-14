import {authFetch} from './auth.ts';
import {projectAllTracks, projectTrackLabels, trackName, isEventTrack, type Project} from './domain.ts';

export type LibraryTrack = {id:string; name:string; mode:'state'|'event'; labels:string[]; is_default:boolean};

export async function libraryRequest<T>(path:string, method='GET', body?:unknown):Promise<T> {
  const response=await authFetch(path,{method,cache:'no-store',...(body===undefined?{}:{headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})});
  const data=await response.json();
  if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'时间轴操作失败，请重试。');
  return data;
}

export function selectionOptions(project:Project, library:LibraryTrack[]):Array<LibraryTrack & {retained:boolean}> {
  const existing=projectAllTracks(project);
  return [
    ...library.map(track=>existing.includes(track.id)?{...track,name:trackName(project,track.id),labels:projectTrackLabels(project,track.id),mode:isEventTrack(project,track.id)?'event' as const:'state' as const,retained:false}:{...track,retained:false}),
    ...existing.filter(id=>!library.some(track=>track.id===id)).map(id=>({id,name:trackName(project,id),labels:projectTrackLabels(project,id),mode:isEventTrack(project,id)?'event' as const:'state' as const,is_default:false,retained:true})),
  ];
}
