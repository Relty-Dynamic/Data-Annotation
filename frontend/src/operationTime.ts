import {parseRecordingOrigin} from './recordingTime.ts';
const clock=new Intl.DateTimeFormat('en-GB',{
 timeZone:'Asia/Shanghai',numberingSystem:'latn',year:'numeric',month:'2-digit',day:'2-digit',
 hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23',
});
/** Display operation records to seconds in Beijing time; storage stays unchanged. */
export function formatOperationTime(value:unknown):string{
 if(typeof value!=='string'||!value.trim())return '—';
 const timestamp=parseRecordingOrigin(value);
 if(timestamp===null)return '—';
 const parts=clock.formatToParts(timestamp);
 const part=(name:Intl.DateTimeFormatPartTypes)=>parts.find(item=>item.type===name)?.value??'';
 return `${part('year')}-${part('month')}-${part('day')} ${part('hour')}:${part('minute')}:${part('second')}`;
}
