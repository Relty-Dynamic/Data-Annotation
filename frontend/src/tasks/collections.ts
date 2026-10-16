import type {TaskVideo} from './types';
export interface TaskSource {id:string;name:string;collection:string;collection_id?:string;collector_name?:string;duration_ms:number;source:{backend:string;key:string}}
export function sourceCollections(sources:TaskSource[],videos:TaskVideo[]){
 const registered=new Set(videos.map(v=>`${v.source.backend}:${v.source.key}`));
 const groups=new Map<string,{id:string;name:string;count:number;pending:number;collector_name?:string}>();
 for(const source of sources){const id=source.collection_id??source.id.split('/')[0];const group=groups.get(id)??{id,name:source.collection,count:0,pending:0};if(source.collector_name)group.collector_name=source.collector_name;group.count++;if(!registered.has(`${source.source.backend}:${source.source.key}`))group.pending++;groups.set(id,group);}
 return [...groups.values()];
}
export function registeredCollections(videos:TaskVideo[],sources:TaskSource[]){
 const groups=new Map<string,{id:string;name:string;videos:TaskVideo[]}>();
 for(const video of videos){const source=sources.find(s=>s.source.backend===video.source.backend&&s.source.key===video.source.key);const collectionId=video.collection_id??source?.collection_id??(video.source.key.includes('/')?video.source.key.split('/')[0]:video.id);const id=`${video.source.backend}:${collectionId}`;const group=groups.get(id)??{id,name:video.collection_name??source?.collection??collectionId,videos:[]};group.videos.push(video);groups.set(id,group);}
 return [...groups.values()];
}
export function assignmentCollections(videos:TaskVideo[],sources:TaskSource[]){
 return registeredCollections(videos,sources).map(group=>{
  const [backend,...parts]=group.id.split(':');const collectionId=parts.join(':');
  const known=sources.filter(source=>source.source.backend===backend&&(source.collection_id??source.id.split('/')[0])===collectionId);
  const expected=Math.max(known.length,...group.videos.map(video=>video.collection_video_count??0),group.videos.length);
  const complete=group.videos.length===expected;
  return {...group,complete,eligible:complete&&group.videos.every(video=>video.quality==='passed'),expected};
 });
}
