// Never show raw upstream bodies: they may contain a server traceback or URLs.
export async function readUploadResponse(response){
 let result;
 try{result=JSON.parse(await response.text());}
 catch{throw new Error(`上传服务返回了异常响应（HTTP ${response.status}），请检查上传助手终端后重试。`);}
 if(!result||typeof result!=='object'||Array.isArray(result))throw new Error('上传服务返回的数据格式不正确，请重新打开上传窗口。');
 if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:`上传操作未成功（HTTP ${response.status}）。`);
 return result;
}
