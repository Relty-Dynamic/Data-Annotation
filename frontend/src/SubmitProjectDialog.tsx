import {useState} from 'react';

export default function SubmitProjectDialog({projectName, submitting, remoteProject, onCancel, onConfirm}: {
  projectName: string;
  submitting: boolean;
  remoteProject: boolean;
  onCancel: () => void;
  onConfirm: (clearServer: boolean, clearComputer: boolean) => void;
}) {
  const [clearServer, setClearServer] = useState(false);
  const [clearComputer, setClearComputer] = useState(false);
  return <div className="modal-backdrop" onClick={() => {if (!submitting) onCancel();}}>
    <section className="submit-project-dialog" role="dialog" aria-modal="true" aria-labelledby="submit-project-title" onClick={event => event.stopPropagation()}>
      <h2 id="submit-project-title">提交「{projectName}」的标注</h2>
      <p>先将时间轴 JSON 写回原采集目录的 timeline 文件夹。{!remoteProject&&'本机项目还会在 NAS 建立同样的采集目录与 timeline 层级，写入并核验 JSON。'}写回成功后，可选择清理以下精简播放缓存；原视频和标注文件始终保留。</p>
      <label><input type="checkbox" checked={clearServer} disabled={submitting} onChange={event => setClearServer(event.target.checked)}/> 清理{remoteProject?'Ubuntu':'本机服务'}上的播放缓存</label>
      {remoteProject&&<label><input type="checkbox" checked={clearComputer} disabled={submitting} onChange={event => setClearComputer(event.target.checked)}/> 清理此浏览器在电脑上的播放缓存</label>}
      <div className="submit-project-actions"><button className="secondary" disabled={submitting} onClick={onCancel}>取消</button><button className="primary" disabled={submitting} onClick={() => onConfirm(clearServer, clearComputer)}>{submitting?'正在提交…':'确认写回'}</button></div>
    </section>
  </div>;
}
