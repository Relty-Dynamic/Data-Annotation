"""Compress, build, transfer and verify clips concurrently; publish the whole capture once."""
from concurrent.futures import ThreadPoolExecutor
import copy
from pathlib import Path
import shutil
import threading

from fastapi import HTTPException

from .mock_s3 import file_sha256
from .upload_playback import build
from .upload_errors import failure_detail
from . import upload_pipeline_cache as recovery


def run(manager, job, user_id, service, can_upload):
    target=manager.store
    selection=job['folder_selection']
    options=job.get('prediction_options',{})
    stop=threading.Event()
    failures=[]
    gate=threading.RLock()
    workspace=manager.staging/job['id'];prepared=workspace/'publish'
    def check():
        if stop.is_set():
            with gate:
                if failures:raise failures[0]
            raise HTTPException(409,'处理已停止，尚未发布采集目录。')
        if not can_upload(user_id):
            raise HTTPException(403,'上传权限已关闭，未发布采集目录。')
    def guarded(function,*args):
        try:return function(*args)
        except Exception as error:
            with gate:
                first = not failures
                failures.append(error)
            stop.set()
            if first:
                stage = {'predict':'姿势预标注','stage':'视频上传或核验','cache':'播放缓存生成','compress':'视频压缩'}.get(function.__name__, '本机处理')
                with manager.lock:
                    job.update(state='error',detail=failure_detail(error, stage))
            raise
    def update(**values):
        with manager.lock:
            if not stop.is_set():job.update(values)
    def prediction_progress(text):
        update(prediction_detail=text)
        if not stop.is_set():target.progress('预标注 · '+text)
    def predict():
        from .prediction_prepare import prepare_prediction
        current,result=prepare_prediction(target,service,selection['root'],selection['folder'],
            {role:options[role+'_device'].strip() for role in ('chest','thigh')},
            progress=prediction_progress,cancel=check)
        selection['folder']=current
        update(prediction_result=result)
        return current['attachments']
    initial_attachments=list(target.attachments)
    metadata={k:job[k] for k in ('prefix','collector_name','uploader_name','capture_date','note')}
    metadata.update(schema_version=1,uploaded_by=user_id,uploaded_by_username=job['uploaded_by_username'],video_profile='playback-480x270-256k32k-v1')
    completed=set();bundles={};sent=[];staged=set()
    original_digests={};normal_desktop={}
    try:
        with manager.lock:
            if job['state'] not in {'ready_to_compress','error'}:
                raise HTTPException(409,'上传任务已在处理。')
            job.update(state='compressing',progress=0,clips_total=len(job['records']),clips_verified=0,
                       prediction_detail='',detail='预标注、压缩与上传正在并行处理。')
        prepared.mkdir(parents=True,exist_ok=True)
        # One encoder, one cache builder, one transfer group; the slowest group
        # does not stop earlier groups from preparing the next clip.
        with ThreadPoolExecutor(max_workers=1) as prediction, ThreadPoolExecutor(max_workers=1) as uploads:
            prediction_job=prediction.submit(guarded,predict) if options.get('prediction')=='generate' else None
            def stage(index,bundle):
                check()
                def progress(text):
                    if not stop.is_set():target.progress(text)
                remote=type(target)(target.client,service,attachments=initial_attachments if index==0 else [],progress=progress)
                remote.transfer_progress=target.transfer_progress
                remote.source_digest=target.source_digest
                remote.publish_ready(job['prefix'],prepared,bundle,service,prepare=True)
                with gate:
                    completed.update((i['key'],i['sha256']) for i in bundle['files']+bundle.get('attachments',[])+bundle['cache_files'])
                    staged.add(index)
                    count=len(staged)
                update(clips_verified=count,progress=round(count/len(job['records'])*95,1),
                       detail=f'已有 {count}/{len(job["records"])} 段完成上传核验，继续准备采集。')
            def cache(index,item):
                check()
                bundle={**copy.deepcopy(metadata),'files':[item]}
                retained=build(service,prepared,bundle,target.progress) or {}
                if index in normal_desktop:retained[item['key']]=normal_desktop[index]
                recovery.save(service,job['prefix'],job['records'][index],original_digests[index],bundle,retained)
                bundles[index]=bundle
                sent.append(uploads.submit(guarded,stage,index,bundle))
            with ThreadPoolExecutor(max_workers=1) as caches:
                cached=[]
                def compress(index,record):
                    check()
                    source=manager._check_local_source(record)
                    target.progress(f'正在核验原视频 {index+1}/{len(job["records"])}：{record["name"]}')
                    original_digests[index]=target.source_digest(record)
                    reused=recovery.load(service,job['prefix'],record,original_digests[index],prepared)
                    if reused:
                        bundles[index]={**copy.deepcopy(metadata),**{k:reused[k] for k in ('files','cache_profile','cache_files')}}
                        sent.append(uploads.submit(guarded,stage,index,bundles[index]))
                        target.progress(f'复用本机已校验缓存 {index+1}/{len(job["records"])}，继续上传。')
                        return
                    destination=prepared/record['output'];destination.parent.mkdir(parents=True,exist_ok=True)
                    def progress(percent,number=index+1):
                        if stop.is_set():check()
                        update(detail=f'正在压缩第 {number}/{len(job["records"])} 段视频：{percent:.0f}%。')
                    manager._compress_video(service,source,destination,progress)
                    manager._check_local_source(record)
                    if service.desktop_cache.enabled:
                        normal_desktop[index]=service.desktop_cache.retain_upload(job['prefix'],source.name,destination)
                    item=dict(source=record['path'],key=record['output'],size=destination.stat().st_size,
                              sha256=file_sha256(destination),duration_ms=service.probe(destination)['duration_ms'])
                    cached.append(caches.submit(guarded,cache,index,item))
                for index,record in enumerate(job['records']):
                    guarded(compress,index,record)
                for future in cached:future.result()
            for future in sent:future.result()
            if prediction_job and not prediction_job.done():
                update(state='preparing_prediction',detail='视频和缓存已上传核验，正在等待姿势预标注完成。')
            target.attachments=prediction_job.result() if prediction_job else initial_attachments
        check()
        manifest={**metadata,'files':[bundles[i]['files'][0] for i in range(len(bundles))],
                  'cache_profile':bundles[0]['cache_profile'],
                  'cache_files':[item for i in range(len(bundles)) for item in bundles[i]['cache_files']]}
        target.publish_ready(job['prefix'],prepared,manifest,service,completed_keys=completed)
        update(state='ready',progress=100,detail='完整采集已核验并发布。')
        return manager.status(job['id'],user_id)
    except Exception as error:
        stop.set()
        with gate:actual=failures[0] if failures else error
        with manager.lock:
            if job['state'] != 'error':job.update(state='error',detail=failure_detail(actual))
        raise actual
    finally:
        shutil.rmtree(workspace,ignore_errors=True)
