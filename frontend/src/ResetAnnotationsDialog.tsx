export default function ResetAnnotationsDialog({projectName, resetting, onCancel, onConfirm}: {
  projectName: string;
  resetting: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return <div className="modal-backdrop" onClick={() => { if (!resetting) onCancel(); }}>
    <section className="submit-project-dialog" role="dialog" aria-modal="true" aria-labelledby="reset-annotations-title" onClick={event => event.stopPropagation()}>
      <h2 id="reset-annotations-title">清空「{projectName}」的旧标注？</h2>
      <p>仅当原采集目录没有时间轴 JSON 时才能重新开始。平台会先保存当前草稿的恢复副本，再清空各条轴的标注；视频、预览、时间轴设置和 NAS 文件不会删除。</p>
      <div className="submit-project-actions"><button className="secondary" disabled={resetting} onClick={onCancel}>取消</button><button className="primary" disabled={resetting} onClick={onConfirm}>{resetting ? '正在保存并清空…' : '保存副本并清空'}</button></div>
    </section>
  </div>;
}
