import {elapsedLabel,stageLabel,stageStatus,type UploadPreparation} from './uploadStageMetrics';
export default function UploadStageProgress({value}:{value:UploadPreparation}){
 return <div className="upload-stage-progress"><p>本次已用 {elapsedLabel(value.elapsed_ms)} · 各阶段可同时进行</p><div className="upload-stage-grid">
 {value.stages.map(stage=><div className="upload-stage" key={stage.stage} data-state={stage.state}>
  <div><strong>{stageLabel(stage.stage)}</strong><span>{stageStatus(stage)}</span></div>
  <progress aria-label={stageLabel(stage.stage)+'进度'} max={100} value={stage.progress}/>
  <small>{stage.state==='waiting'?'尚未开始':`处理用时 ${elapsedLabel(stage.elapsed_ms)}`}{stage.state==='running'&&stage.active>1?` · ${stage.active} 段同时处理`:''}</small>
 </div>)}</div></div>;
}
