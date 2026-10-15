/** The same workbench is scoped to a task; legacy URLs are unchanged otherwise. */
export function taskFromSearch(search:string):string|null {
  const value=new URLSearchParams(search).get('task');
  return value&&/^[0-9a-f]{32}$/.test(value)?value:null;
}
export function taskRequestUrl(url:string,taskId:string|null):string {
  if(!taskId)return url;
  const base='/api/tasks/items/'+taskId+'/workspace/';
  if(url==='/api/projects')return base+'projects';
  const project=url.match(/^\/api\/projects\/[0-9a-f]{32}(.*)$/);
  if(project)return base+'project'+project[1];
  const media=url.match(/^\/api\/(previews|storyboards|media|thumbnails|session-media|session-thumbnails|session-storyboards)\/[0-9a-f]{32}\/(.*)$/);
  return media?base+media[1]+'/'+media[2]:url;
}
