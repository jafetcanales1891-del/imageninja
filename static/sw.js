const CACHE='imageninja-v1';
const CORE=['/','/manifest.webmanifest','/static/icon.svg'];
self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(CORE))));
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k))))));
self.addEventListener('fetch',e=>{
 const u=new URL(e.request.url);
 if(e.request.method!=='GET'||u.pathname.startsWith('/api/'))return;
 e.respondWith(caches.match(e.request).then(cached=>cached||fetch(e.request).then(r=>{
  if(r.ok&&u.origin===location.origin){const copy=r.clone();caches.open(CACHE).then(c=>c.put(e.request,copy))}
  return r;
 })));
});
