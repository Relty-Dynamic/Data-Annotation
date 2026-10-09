import {useState} from 'react';

export default function CloudAccountDialog({busy, error, onCancel, onConnect}: {
  busy: boolean;
  error: string;
  onCancel: () => void;
  onConnect: (username: string, password: string) => void;
}) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  return <div className="modal-backdrop"><form className="submit-project-dialog" role="dialog" aria-modal="true" aria-label="连接 Ubuntu 公网账号" onSubmit={event => {event.preventDefault(); onConnect(username, password);}}>
    <h2>连接 Ubuntu 公网账号</h2>
    <p>本机视频留在此电脑。连接后，项目进度和标注结果同步到 Ubuntu，管理员可以查看；账号密码仅用于本次连接，不保存到文件。</p>
    <label>公网账号<input value={username} disabled={busy} autoComplete="username" required onChange={event => setUsername(event.target.value)}/></label>
    <label>密码<input type="password" value={password} disabled={busy} autoComplete="current-password" required onChange={event => setPassword(event.target.value)}/></label>
    {error&&<p role="alert" className="import-error">{error}</p>}
    <div className="submit-project-actions"><button type="button" className="secondary" disabled={busy} onClick={onCancel}>取消</button><button className="primary" disabled={busy}>连接</button></div>
  </form></div>;
}
