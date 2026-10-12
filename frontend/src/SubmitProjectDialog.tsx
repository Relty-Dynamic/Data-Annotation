export default function SubmitProjectDialog({projectName, submitting, remoteProject, mockProject = false, onCancel, onConfirm}: {
  projectName: string;
  submitting: boolean;
  remoteProject: boolean;
  mockProject?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return <div className="modal-backdrop" onClick={() => {if (!submitting) onCancel();}}>
    <section className="submit-project-dialog" role="dialog" aria-modal="true" aria-labelledby="submit-project-title" onClick={event => event.stopPropagation()}>
      <h2 id="submit-project-title">提交「{projectName}」的标注</h2>
      <p>{mockProject?'时间轴 JSON 将保存到此模拟采集目录的 timeline/ 文件夹，并在读回核验后确认。压缩视频和标注文件都会保留。':<>时间轴 JSON 将写回原采集目录的 timeline 文件夹。{!remoteProject&&'本机项目还会在 NAS 建立同样的采集目录与 timeline 层级，写入并核验 JSON。'}原视频、预览缓存和标注文件都会保留。</>}</p>
      <div className="submit-project-actions"><button className="secondary" disabled={submitting} onClick={onCancel}>取消</button><button className="primary" disabled={submitting} onClick={onConfirm}>{submitting?'正在提交…':mockProject?'确认保存':'确认写回'}</button></div>
    </section>
  </div>;
}
