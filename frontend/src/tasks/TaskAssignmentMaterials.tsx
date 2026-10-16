import type {assignmentCollections} from './collections';
export default function TaskAssignmentMaterials({collections,busy}:{collections:ReturnType<typeof assignmentCollections>;busy:boolean}){
 return <fieldset className="task-assignment-materials"><legend>选择完整采集</legend>
  <div className="task-assignment-section-heading"><span>一个采集目录对应一个任务</span><span>{collections.filter(collection=>collection.eligible).length} 个可选</span></div>
  {collections.map(collection=><div className="task-assignment-collection" key={collection.id}>
   <label><input type="checkbox" name="collection" value={collection.id} disabled={busy||!collection.eligible}/><span><strong>{collection.name}</strong><small>{collection.videos.length} 段视频 · {!collection.complete?'尚未登记完整':collection.eligible?'全部质检合格':'仍有视频未通过质检'}</small></span></label>
   <details><summary>查看包含的视频</summary><ul>{collection.videos.map(video=><li key={video.id}>{video.video_name??video.id}</li>)}</ul></details>
  </div>)}
  <p className="task-assignment-hint">完整采集为最小分配单位，标注台连续显示全部视频；不拆分人员、提交或复核。</p>
  {!collections.length&&<p className="task-assignment-empty">暂无已登记采集，请先导入并登记完整文件夹。</p>}
 </fieldset>;
}
