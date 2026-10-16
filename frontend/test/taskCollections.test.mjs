import test from 'node:test';
import assert from 'node:assert/strict';
import {sourceCollections,registeredCollections} from '../src/tasks/collections.ts';
const source=(project,video,backend='s3')=>({id:`${project}/${video}`,name:`${video}.mp4`,collection:'1001lixin',collection_id:project,duration_ms:1000,source:{backend,key:`${project}/${video}`}});
const video=(source,fields={})=>({id:source.id,source:source.source,quality:'passed',...fields});
test('collection carries uploaded collector without guessing names for legacy or NAS sources',()=>{
 const sources=[{...source('a','v1'),collector_name:'李鑫'},{...source('a','v2'),collector_name:'李鑫'},source('b','v1','nas')];
 const groups=sourceCollections(sources,[]);
 assert.equal(groups[0].collector_name,'李鑫');
 assert.equal(groups[1].collector_name,undefined);
 assert.equal(sourceCollections(sources.map(s=>({...s})),[])[0].collector_name,'李鑫');
});
test('folder selection groups clips once and counts unregistered sources by backend and identity',()=>{
 const sources=[source('a','v1'),source('a','v2'),source('b','v1')];
 assert.deepEqual(sourceCollections(sources,[video(sources[0])]),[
  {id:'a',name:'1001lixin',count:2,pending:1},{id:'b',name:'1001lixin',count:1,pending:1}]);
 assert.equal(sourceCollections(sources,[video(source('a','v1','nas'))])[0].pending,2);
});
test('registered list groups legacy and new records while keeping same-named folders separate',()=>{
 const sources=[source('a','v1'),source('a','v2'),source('b','v1')];
 const videos=[video(sources[0]),video(sources[1],{collection_id:'a',collection_name:'1001lixin'}),video(sources[2])];
 const grouped=registeredCollections(videos,sources);
 assert.equal(grouped.length,2);assert.equal(grouped[0].name,'1001lixin');assert.equal(grouped[0].videos.length,2);
 assert.deepEqual(grouped.flatMap(group=>group.videos),videos);
 assert.equal(registeredCollections([videos[1]],[])[0].name,'1001lixin');
 assert.equal(registeredCollections([video({id:'old',source:{backend:'nas',key:'old'}})],[])[0].name,'old');
});
