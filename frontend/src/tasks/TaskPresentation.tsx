import {Inbox} from 'lucide-react';
import type {ReactNode} from 'react';
import {statusNames} from './types';
export function TaskPageHeading({title,description,count}:{title:string;description:string;count?:string}){
 return <header className="task-page-heading"><div><h3>{title}</h3><p>{description}</p></div>{count&&<span className="task-page-count">{count}</span>}</header>;
}
export function TaskStatus({status}:{status:string}){
 return <span className="task-status-badge" data-status={status}>{statusNames[status]??status}</span>;
}
export function TaskEmpty({title,children}:{title:string;children?:ReactNode}){
 return <div className="task-empty-state"><Inbox size={26} aria-hidden="true"/><strong>{title}</strong>{children&&<p>{children}</p>}</div>;
}
