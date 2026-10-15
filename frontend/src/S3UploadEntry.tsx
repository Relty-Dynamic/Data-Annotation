import './s3Upload.css';

export default function S3UploadEntry(){
 return <section className="s3-upload-entry" aria-label="上传到 S3">
  <h3>上传采集文件夹到 S3</h3>
  <p>在本机上传窗口选择文件夹。视频按原方案压缩，IMU、HEART 和 prediction JSON 原样上传；视频缓存同时保留到桌面 dataMarkProject。</p>
  <a className="s3-upload-open" href="http://127.0.0.1:8767/" target="_blank" rel="noopener noreferrer">打开本机上传窗口</a>
  <details><summary>首次使用：启动本机上传助手</summary>
   <p>在电脑的 DataMark 项目目录打开终端，运行对应命令并保持终端开启。窗口使用内网／公网通用账号登录，须有上传权限。</p>
   <p>macOS</p><code>.venv/bin/python -m backend.r2_upload_helper</code>
   <p>Windows</p><code>.venv\Scripts\python.exe -m backend.r2_upload_helper</code>
   <p>若无法打开窗口，请确认助手已启动。Safari、Chrome、Edge 均通过独立本机窗口选择目录，避免跨域页面直接读写本机文件。</p>
  </details>
  <p>上传完成后，使用下方“导入”选择已有 S3 采集目录；登记和质检后再分配任务。timeline 由标注输出生成，不上传本机旧时间轴。</p>
 </section>;
}
