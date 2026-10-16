/** Navigate only after saving and after the surrounding busy operation finishes. */
export async function saveBeforeNavigation(
 run:(save:()=>Promise<void>)=>Promise<void>,
 save:()=>Promise<void>,
 navigate:()=>void,
){
 let saved=false;
 await run(async()=>{await save();saved=true;});
 // The runner reports save failures itself. They must never trigger navigation.
 if(saved)navigate();
}
