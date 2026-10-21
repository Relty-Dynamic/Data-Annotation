// scrollIntoView can scroll hidden ancestors as well as the intended panel.
// Keep automatic editor reveal inside the explicitly owned scroll container.
export function revealInPanel(panel:HTMLElement|null,target:HTMLElement|null){
  if(!panel||!target||!panel.contains(target))return;
  const viewport=panel.getBoundingClientRect(),item=target.getBoundingClientRect();
  const top=viewport.top+panel.clientTop,bottom=top+panel.clientHeight;
  if(item.top<top||item.height>panel.clientHeight)panel.scrollTop+=item.top-top;
  else if(item.bottom>bottom)panel.scrollTop+=item.bottom-bottom;
}
