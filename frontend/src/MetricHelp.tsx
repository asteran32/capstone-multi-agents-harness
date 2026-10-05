import {useEffect, useId, useLayoutEffect, useRef, useState} from 'react';
import {createPortal} from 'react-dom';

/** A short, hoverable explanation that also works with focus and touch. */
export function MetricHelp({label,description}:{label:string;description:string}) {
 const id=useId(),trigger=useRef<HTMLButtonElement>(null),tooltip=useRef<HTMLDivElement>(null);
 const closeTimer=useRef<ReturnType<typeof setTimeout>|undefined>(undefined);
 const [open,setOpen]=useState(false),[position,setPosition]=useState({left:0,top:0});
 const show=()=>{clearTimeout(closeTimer.current);setOpen(true)};
 const hide=()=>{clearTimeout(closeTimer.current);setOpen(false)};
 const leave=()=>{clearTimeout(closeTimer.current);if(document.activeElement!==trigger.current)closeTimer.current=setTimeout(()=>setOpen(false),150)};
 useEffect(()=>()=>clearTimeout(closeTimer.current),[]);
 useLayoutEffect(()=>{
  if(!open)return;
  const place=()=>{
   if(!trigger.current||!tooltip.current)return;
   const anchor=trigger.current.getBoundingClientRect(),box=tooltip.current.getBoundingClientRect();
   if(anchor.bottom<0||anchor.top>window.innerHeight||anchor.right<0||anchor.left>window.innerWidth){hide();return}
   const left=Math.max(12,Math.min(anchor.left,window.innerWidth-box.width-12));
   const below=anchor.bottom+8;
   const top=below+box.height<=window.innerHeight-12?below:Math.max(12,anchor.top-box.height-8);
   setPosition({left,top});
  };
  place();window.addEventListener('resize',place);window.addEventListener('scroll',place,true);
  return()=>{window.removeEventListener('resize',place);window.removeEventListener('scroll',place,true)};
 },[open,description]);
 useEffect(()=>{
  if(!open)return;
  const escape=(event:KeyboardEvent)=>{if(event.key==='Escape')hide()};
  const outside=(event:Event)=>{
   const target=event.target as Node;
   if(!trigger.current?.contains(target)&&!tooltip.current?.contains(target))hide();
  };
  document.addEventListener('keydown',escape);document.addEventListener('pointerdown',outside);document.addEventListener('focusin',outside);
  return()=>{document.removeEventListener('keydown',escape);document.removeEventListener('pointerdown',outside);document.removeEventListener('focusin',outside)};
 },[open]);
 return <>
  <button type="button" className="metric-help" ref={trigger} aria-label={`${label} 설명`} aria-describedby={open?id:undefined}
   onMouseEnter={show} onMouseLeave={leave} onFocus={show} onBlur={hide} onClick={show}>{label}<span className="metric-help-icon" aria-hidden="true">?</span></button>
  {open&&createPortal(<div ref={tooltip} id={id} role="tooltip" className="metric-tooltip" style={position}
   onMouseEnter={show} onMouseLeave={leave}>{description}</div>,document.body)}
 </>;
}
