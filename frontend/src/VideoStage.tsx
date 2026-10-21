import {useLayoutEffect, useRef, useState, type ReactNode} from 'react';

// Measure the available slot, not the fitted frame: resizing the frame must never
// change its own size budget or push the timeline out of the viewport.
export default function VideoStage({empty=false,children}:{empty?:boolean;children:ReactNode}){
  const slot=useRef<HTMLDivElement>(null);
  const [bounds,setBounds]=useState({width:0,height:0});
  const [ratio,setRatio]=useState(16/9);
  useLayoutEffect(()=>{
    const element=slot.current;
    if(!element||empty)return;
    const observer=new ResizeObserver(([entry])=>{
      const {width,height}=entry.contentRect;
      setBounds(previous=>previous.width===width&&previous.height===height?previous:{width,height});
    });
    const updateRatio=(event:Event)=>{
      const video=event.target;
      if(video instanceof HTMLVideoElement&&element.contains(video)&&video.videoWidth>0&&video.videoHeight>0){
        setRatio(video.videoWidth/video.videoHeight);
      }
    };
    observer.observe(element);
    // Capture also handles replacement video elements when clip/speed changes.
    element.addEventListener('loadedmetadata',updateRatio,true);
    element.addEventListener('resize',updateRatio,true);
    return ()=>{
      observer.disconnect();
      element.removeEventListener('loadedmetadata',updateRatio,true);
      element.removeEventListener('resize',updateRatio,true);
    };
  },[empty]);
  const width=Math.max(0,Math.min(bounds.width,bounds.height*ratio));
  if(empty)return <div className="video-stage empty">{children}</div>;
  return <div ref={slot} className="video-slot">
    <div className="video-stage video-frame" style={{width,height:width/ratio}}>{children}</div>
  </div>;
}
