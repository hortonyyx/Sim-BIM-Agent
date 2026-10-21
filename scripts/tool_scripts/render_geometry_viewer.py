"""Self-contained OFFLINE interactive 3D viewer for building_geometry.json (#3).

The geometry-confirmation gate (contracts §1 2/3 ②a) is a HUMAN gate: the user
inspects the deterministic build in 3D before the run continues. Real interactive
3D: orbit/zoom, wall translucency, section cuts, exploded view
(by floor or by zone, always upward), CAD-style vertex-snap measure, click-select,
save-PNG, a small ground compass + axis ticks.

ONE fully self-contained ``geometry_viewer.html``:
  - three.js (r0.137.5 UMD global) + OrbitControls INLINED from vendor/ → offline,
    file:// double-click, no CDN.
  - geometry embedded inline as ``window.GEO`` (script-safe via ``_js_embed``).
  - ALL faces are kept (each zone stays a closed box, preserving the EP reciprocal
    split-pairing). z-fighting is avoided WITHOUT deleting geometry: at explode=0 one
    face of each coincident reciprocal pair is HIDDEN (solid clean shell, no gaps);
    at explode>0 all faces show, separated by the explode offset. Windows are pushed
    proud of their wall (WIN_POP) so they render clean and stay pickable.

Headless note: the rendered result needs a browser to confirm (no browser here);
generation + ``node --check`` of the app JS is the automated guard.

Usage:
    python scripts/tool_scripts/render_geometry_viewer.py <building_geometry.json|source_model.json> [--out viewer.html]
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

_VENDOR = Path(__file__).resolve().parent / "vendor"


def _js_embed(obj) -> str:
    """JSON for inline embedding in a <script> — escape the chars that could break
    out of the script element / be mis-tokenised (``<`` → ``</script>``, plus & and
    the JS line separators U+2028/U+2029). These become \\u00xx, which a JS object
    literal parses back to the original char."""
    return (
        json.dumps(obj, ensure_ascii=False)
        .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        .replace(" ", "\\u2028").replace(" ", "\\u2029")
    )


# --------------------------------------------------------------------------- #
# viewer app (classic script; global THREE + THREE.OrbitControls + window.GEO).
# Standalone constant so it can be `node --check`ed.
# --------------------------------------------------------------------------- #
_APP_JS = r"""
(function () {
  const GEO = window.GEO || { zones: [], surfaces: [], windows: [], roles: {} };
  const $ = (id) => document.getElementById(id);
  const SURF = (GEO.surfaces || []).filter(s => (s.verts || []).length >= 3);
  const WINS = (GEO.windows || []).filter(w => (w.verts || []).length >= 3);
  const OPENS = (GEO.openings || []).filter(o => (o.verts || []).length >= 3);
  const WALL_PARTS = GEO.visible_wall_parts || {};
  const ENC_REGIONS = GEO.enclosure_regions || [];
  const ZONES = (GEO.zones || []).slice().sort((a, b) => b.length - a.length);
  const SOURCE = GEO.source_model || null;
  const SOURCE_MAP = SOURCE ? (SOURCE.derived || {}) : {};
  const SOURCE_SPACES = Object.fromEntries(((SOURCE && SOURCE.spaces) || []).map(s=>[s.id,s]));
  const SOURCE_BOUNDARIES = Object.fromEntries(((SOURCE && SOURCE.boundaries) || []).map(b=>[b.id,b]));
  const WALL_PROPERTIES = GEO.wall_properties || {};
  // resolve a window's zone: its parent surface's zone first (parent = "<wall>_<i>"),
  // then a zone-name prefix, then the nearest zone centroid — never returns '?' so a
  // window always pops out + groups/explodes with a real zone.
  const _surfZoneByName = {};
  SURF.forEach(s => { if (s.name) _surfZoneByName[s.name] = s.zone || '?'; });
  const _surfNames = Object.keys(_surfZoneByName).sort((a, b) => b.length - a.length);
  function zoneOfWindow(w){
    const p = w.parent || '';
    for (const n of _surfNames) if (p.startsWith(n)) return _surfZoneByName[n];
    const z = ZONES.find(z => p.startsWith(z)); if (z) return z;
    let cx=0,cy=0,cz=0; const vs=w.verts||[]; vs.forEach(v=>{cx+=v[0];cy+=v[1];cz+=v[2];}); const n=vs.length||1;
    const wc=new THREE.Vector3(cx/n,cy/n,cz/n); let best=ZONES[0]||'?', bd=1e18;
    for (const z in zoneCentroid){ const d=zoneCentroid[z].distanceToSquared(wc); if(d<bd){bd=d;best=z;} }
    return best;
  }

  const FLOOR_COLORS = [0xb0d0e8,0xffe0b2,0xc8e6c9,0xf4c7c7,0xd1c4e9,0xfff59d,0xb2dfdb,0xd7ccc8];
  const TYPE_COLORS = { Wall:0xdfe3e6, Floor:0xc8a165, Ceiling:0x9fa8da, Roof:0xfff3b0 };
  const WINDOW_COLOR = 0x1e5ad2, WHITE = 0xffffff, SEL_COLOR = 0xff9800;
  const ENCLOSURE_COLOR = 0x8b949e, ENCLOSURE_OPACITY = 0.32, LOGICAL_COLOR = 0x596b86;
  // Classification and colors come from the same pinned catalog as generation.
  const ROOM_TYPES = GEO.room_types || {};
  const ROLE_COLORS = Object.fromEntries(Object.entries(ROOM_TYPES).map(([k,v])=>[k,parseInt(v.color.slice(1),16)]));
  const ROLE_DEFAULT = ROLE_COLORS.unknown ?? 0xc9ced4;
  const NAMES = GEO.public_names || {};
  const REFERENCES = NAMES.references || {};
  const referenceKeys = Object.keys(REFERENCES).filter(k=>k!==REFERENCES[k]).sort((a,b)=>b.length-a.length);
  const referencePattern = referenceKeys.length ? new RegExp(referenceKeys.map(k=>k.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('|'),'g') : null;
  function referenceText(value){ const text=String(value);
    return referencePattern ? text.replace(referencePattern,(key,at)=>{
      const before=text[at-1]||'', after=text.slice(at+key.length);
      return /[A-Za-z0-9_./:%\\-]/.test(before) || /^(?:[A-Za-z0-9_/%\\-]|[.:][A-Za-z0-9_])/.test(after) ? key : REFERENCES[key];
    }) : text; }
  const spaceName = z => (NAMES.spaces||{})[(SOURCE_MAP.zones||{})[z]||z] || z;
  const objectName = n => (NAMES.objects||{})[n] || n;
  const roleLabel = r => ROOM_TYPES[r] ? ROOM_TYPES[r].label_zh+' · '+r : r;
  const ROLES = GEO.roles || {};
  const HAS_ROLES = Object.keys(ROLES).length > 0;
  const roleOf = z => (ROLES[z] || '').toString().toLowerCase();
  // zone-mode fill: by room type if roles are available, else white (legacy behaviour)
  const roleColor = z => { if(!HAS_ROLES) return WHITE; const r=roleOf(z); return r ? (ROLE_COLORS[r] ?? ROLE_DEFAULT) : ROLE_DEFAULT; };
  const hex6 = c => '#'+('000000'+(c>>>0).toString(16)).slice(-6);
  const esc = s => String(s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  // Geometry is drawn at TRUE positions (so measure is exact + no inset gaps).
  // z-fighting is avoided by: (a) at explode=0, HIDING one face of each coincident
  // reciprocal (split-paired) pair — solid clean shell, no gaps; (b) at explode>0,
  // showing ALL faces (each zone a closed box) separated by the explode offset;
  // (c) windows pushed proud of their wall by WIN_POP (also makes them pickable).
  const WIN_POP = 0.03;  // metres

  // ---- floors: bases from FLOOR-type surfaces, per-zone assignment ----
  const zmin = (s) => Math.min(...s.verts.map(v => v[2]));
  function cluster(zs){ const u=[...new Set(zs.map(z=>Math.round(z*10)/10))].sort((a,b)=>a-b);
    const b=[]; for(const z of u) if(!b.length||Math.abs(z-b[b.length-1])>0.3) b.push(z); return b.length?b:[0]; }
  function nearestBase(z,bases){ let bi=0,bd=1e9; bases.forEach((b,i)=>{const d=Math.abs(z-b); if(d<bd){bd=d;bi=i;}}); return bi; }
  const floorSurf = SURF.filter(s => s.type === 'Floor');
  const NAMED_FLOORS = NAMES.floors || [];
  const BASES = NAMED_FLOORS.length ? NAMED_FLOORS.map(f=>f.z_floor) : cluster((floorSurf.length ? floorSurf : SURF).map(zmin));
  const sourceFloor = i => (SOURCE?.floors||[]).find(f=>f.id===NAMED_FLOORS[i]?.id);
  const floorName = i => sourceFloor(i)?.name || NAMED_FLOORS[i]?.name || 'F'+(i+1);
  const _zFloorZ = {}, _zMinZ = {};
  SURF.forEach(s => { const z=s.zone||'?', mn=zmin(s); _zMinZ[z]=Math.min(_zMinZ[z]??1e9,mn);
    if(s.type==='Floor') _zFloorZ[z]=Math.min(_zFloorZ[z]??1e9,mn); });
  const zoneFloor = {}, zoneFloors = {};
  Object.keys(_zMinZ).forEach(z => { const sid=(SOURCE_MAP.zones||{})[z]||z;
    const fi=NAMED_FLOORS.findIndex(f=>f.id===SOURCE_SPACES[sid]?.floor_id);
    zoneFloor[z]=fi>=0 ? fi : nearestBase(_zFloorZ[z]??_zMinZ[z], BASES);
    // Filtering follows declared source membership only. A continuous space
    // remains one volume while appearing in every storey that explicitly
    // lists it; legacy viewers keep their single geometric floor assignment.
    const declared=(GEO.floor_memberships||{})[z]||[];
    zoneFloors[z]=declared.length ? declared : [zoneFloor[z]];
  });
  const onFloor = (u,f) => f<0 || (zoneFloors[u.zone]||[u.floor]).includes(f);

  // ---- zone centroids (explode-by-zone + window pop-out) ----
  const zoneSum = {};
  SURF.forEach(s => { const z=s.zone||'?'; const a=zoneSum[z]||(zoneSum[z]=[0,0,0,0]);
    s.verts.forEach(v=>{a[0]+=v[0];a[1]+=v[1];a[2]+=v[2];a[3]++;}); });
  const zoneCentroid = {};
  Object.keys(zoneSum).forEach(z=>{const a=zoneSum[z]; zoneCentroid[z]=new THREE.Vector3(a[0]/a[3],a[1]/a[3],a[2]/a[3]);});
  // translate a window's verts a few cm AWAY from its zone centroid → proud of the wall
  const popOut = (verts, zone) => { const c=zoneCentroid[zone]; if(!c) return verts;
    let cx=0,cy=0,cz=0; verts.forEach(v=>{cx+=v[0];cy+=v[1];cz+=v[2];}); const n=verts.length||1; cx/=n;cy/=n;cz/=n;
    const dx=cx-c.x, dy=cy-c.y, dz=cz-c.z; const L=Math.hypot(dx,dy,dz)||1; const t=WIN_POP/L;
    return verts.map(v=>[v[0]+dx*t, v[1]+dy*t, v[2]+dz*t]); };

  // ---- bbox ----
  const bb = new THREE.Box3();
  SURF.forEach(s => s.verts.forEach(v => bb.expandByPoint(new THREE.Vector3(v[0],v[1],v[2]))));
  if (bb.isEmpty()) bb.set(new THREE.Vector3(0,0,0), new THREE.Vector3(10,10,3));
  const center = bb.getCenter(new THREE.Vector3());
  const size = bb.getSize(new THREE.Vector3());
  const radius = Math.max(size.x, size.y, size.z) || 10;
  const zoneDir = {};  // full 3D radial from building centre (zone explode = all directions)
  Object.keys(zoneCentroid).forEach(z=>{ const d=zoneCentroid[z].clone().sub(center);
    zoneDir[z] = d.lengthSq()<1e-6 ? new THREE.Vector3(0,0,0) : d.normalize(); });

  // ---- renderer / scene / camera ----
  const renderer = new THREE.WebGLRenderer({ antialias:true, preserveDrawingBuffer:true });
  renderer.setPixelRatio(window.devicePixelRatio);
  renderer.setSize(window.innerWidth, window.innerHeight);
  renderer.localClippingEnabled = true;
  renderer.setClearColor(0xf2f4f7);
  $('app').appendChild(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(50, window.innerWidth/window.innerHeight, 0.05, radius*200);
  camera.up.set(0,0,1);
  const controls = new THREE.OrbitControls(camera, renderer.domElement);
  function defaultView(){ camera.position.set(center.x+radius*1.6, center.y-radius*1.8, center.z+radius*1.3);
    controls.target.copy(center); controls.update(); }
  scene.add(new THREE.AmbientLight(0xffffff, 0.8));
  const dl=new THREE.DirectionalLight(0xffffff,0.6); dl.position.set(1,-1,2); scene.add(dl);
  const dl2=new THREE.DirectionalLight(0xffffff,0.3); dl2.position.set(-1,1,0.5); scene.add(dl2);
  const grid=new THREE.GridHelper(radius*4,20,0xcccccc,0xe6e6e6); grid.rotateX(Math.PI/2);
  grid.position.set(center.x, center.y, bb.min.z); scene.add(grid);

  // ---- small text labels (axes + compass) ----
  function label(txt, color, scl){
    const c=document.createElement('canvas'); c.width=c.height=64; const x=c.getContext('2d');
    x.fillStyle=color; x.font='bold 46px sans-serif'; x.textAlign='center'; x.textBaseline='middle'; x.fillText(txt,32,34);
    const sp=new THREE.Sprite(new THREE.SpriteMaterial({map:new THREE.CanvasTexture(c), depthTest:false, transparent:true}));
    sp.scale.set(scl, scl, 1); return sp;
  }
  // small axis ticks at the origin corner
  const aL=radius*0.18, aLab=radius*0.05;
  const axes=new THREE.AxesHelper(aL); axes.position.copy(bb.min); scene.add(axes);
  [['x',0xcc1111,new THREE.Vector3(aL*1.25,0,0)],['y',0x11aa11,new THREE.Vector3(0,aL*1.25,0)],
   ['z',0x1111cc,new THREE.Vector3(0,0,aL*1.25)]].forEach(([t,col,off])=>{
    const s=label(t,'#'+col.toString(16).padStart(6,'0'),aLab); s.position.copy(bb.min).add(off); scene.add(s); });
  // compass: small flat 2D ring on the ground, N = +Y (EnergyPlus convention)
  const cR=radius*0.16, cC=new THREE.Vector3(bb.min.x-radius*0.32, bb.min.y-radius*0.32, bb.min.z+0.02);
  const ring=new THREE.Mesh(new THREE.RingGeometry(cR*0.86, cR, 48),
    new THREE.MeshBasicMaterial({color:0x9aa0a6, side:THREE.DoubleSide})); ring.position.copy(cC); scene.add(ring);
  const ntick=new THREE.Line(new THREE.BufferGeometry().setFromPoints(
    [cC.clone(), cC.clone().add(new THREE.Vector3(0,cR,0))]), new THREE.LineBasicMaterial({color:0xd32f2f}));
  scene.add(ntick);
  const nlab=label('N','#d32f2f',radius*0.06); nlab.position.copy(cC).add(new THREE.Vector3(0,cR*1.25,0)); scene.add(nlab);

  // ---- clip planes (section cuts) ----
  const AX = [
    {key:'X', base:new THREE.Vector3(-1,0,0), min:bb.min.x, max:bb.max.x},
    {key:'Y', base:new THREE.Vector3(0,-1,0), min:bb.min.y, max:bb.max.y},
    {key:'Z', base:new THREE.Vector3(0,0,-1), min:bb.min.z, max:bb.max.z},
  ];
  AX.forEach(a=>{ a.enabled=false; a.flip=false; a.pos=a.max; a.plane=new THREE.Plane(a.base.clone(), a.max); });
  function updatePlane(a){ a.plane.normal.copy(a.flip?a.base.clone().negate():a.base.clone()); a.plane.constant=a.flip?-a.pos:a.pos; }
  function activePlanes(){ return AX.filter(a=>a.enabled).map(a=>a.plane); }

  // ---- build meshes (keep ALL faces; reciprocal dup hidden at rest, windows popped out) ----
  const surfMeshes=[], winMeshes=[], openingMeshes=[], enclosureMeshes=[], edgeSegs=[], logicalLines=[], enclosureLines=[], windowEdges=[];
  const root=new THREE.Group(); scene.add(root);
  function fanTriangulate(n){ const idx=[]; for(let i=1;i<n-1;i++) idx.push(0,i,i+1); return idx; }
  function projectRing(ring){
    let nx=0,ny=0,nz=0;
    for(let i=0;i<ring.length;i++){ const a=ring[i],b=ring[(i+1)%ring.length];
      nx+=(a[1]-b[1])*(a[2]+b[2]); ny+=(a[2]-b[2])*(a[0]+b[0]); nz+=(a[0]-b[0])*(a[1]+b[1]); }
    const ax=Math.abs(nx), ay=Math.abs(ny), az=Math.abs(nz);
    if(ax>=ay && ax>=az) return ring.map(v=>[v[1],v[2]]);
    if(ay>=ax && ay>=az) return ring.map(v=>[v[0],v[2]]);
    return ring.map(v=>[v[0],v[1]]);
  }
  function signedArea2D(pts){ let a=0; for(let i=0;i<pts.length;i++){ const p=pts[i],q=pts[(i+1)%pts.length]; a+=p[0]*q[1]-q[0]*p[1]; } return a/2; }
  function pointInTri(p,a,b,c){
    const v0=[c[0]-a[0],c[1]-a[1]], v1=[b[0]-a[0],b[1]-a[1]], v2=[p[0]-a[0],p[1]-a[1]];
    const dot00=v0[0]*v0[0]+v0[1]*v0[1], dot01=v0[0]*v1[0]+v0[1]*v1[1], dot02=v0[0]*v2[0]+v0[1]*v2[1];
    const dot11=v1[0]*v1[0]+v1[1]*v1[1], dot12=v1[0]*v2[0]+v1[1]*v2[1];
    const den=dot00*dot11-dot01*dot01; if(Math.abs(den)<1e-12) return false;
    const u=(dot11*dot02-dot01*dot12)/den, v=(dot00*dot12-dot01*dot02)/den;
    return u>-1e-9 && v>-1e-9 && u+v<1+1e-9;
  }
  function earTriangulate(ring){
    const n=ring.length; if(n<5) return fanTriangulate(n);
    const pts=projectRing(ring); const area=signedArea2D(pts);
    if(Math.abs(area)<1e-9) return fanTriangulate(n);
    const ccw=area>0, verts=[...Array(n).keys()], idx=[]; let guard=0;
    function cross(a,b,c){ return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]); }
    while(verts.length>3 && guard++<n*n){
      let clipped=false;
      for(let vi=0;vi<verts.length;vi++){
        const ia=verts[(vi-1+verts.length)%verts.length], ib=verts[vi], ic=verts[(vi+1)%verts.length];
        const a=pts[ia], b=pts[ib], c=pts[ic]; const cr=cross(a,b,c);
        if(ccw ? cr<=1e-10 : cr>=-1e-10) continue;
        let contains=false;
        for(const j of verts){ if(j===ia||j===ib||j===ic) continue; if(pointInTri(pts[j],a,b,c)){ contains=true; break; } }
        if(contains) continue;
        idx.push(ia,ib,ic); verts.splice(vi,1); clipped=true; break;
      }
      if(!clipped) return fanTriangulate(n);
    }
    if(verts.length===3) idx.push(verts[0],verts[1],verts[2]);
    return idx.length ? idx : fanTriangulate(n);
  }
  function ringGeom(ring, holes=[]){ const pos=[]; [ring,...holes].forEach(r=>r.forEach(v=>pos.push(v[0],v[1],v[2])));
    const vec = r => projectRing(r).map(p=>new THREE.Vector2(p[0],p[1]));
    const idx=holes.length ? THREE.ShapeUtils.triangulateShape(vec(ring),holes.map(vec)).flat() : earTriangulate(ring);
    const g=new THREE.BufferGeometry(); g.setAttribute('position',new THREE.Float32BufferAttribute(pos,3));
    g.setIndex(idx); g.computeVertexNormals(); return g; }
  function edgeGeom(ring){ const pos=[]; for(let i=0;i<ring.length;i++){const a=ring[i],b=ring[(i+1)%ring.length];
    pos.push(a[0],a[1],a[2], b[0],b[1],b[2]);} const g=new THREE.BufferGeometry();
    g.setAttribute('position',new THREE.Float32BufferAttribute(pos,3)); return g; }
  function dashedEdge(ring,color,dash,gap,userData){
    const line=new THREE.LineSegments(edgeGeom(ring),new THREE.LineDashedMaterial({color,transparent:true,opacity:0.95,dashSize:dash,gapSize:gap,depthTest:false}));
    line.computeLineDistances(); line.renderOrder=800; line.userData=userData; root.add(line); return line; }
  const isDup = (s) => s.obc==='Surface' && s.obc_obj && s.name > s.obc_obj;  // one of each reciprocal pair
  SURF.forEach(s=>{
    const zone=s.zone||'?', fi=zoneFloor[zone] ?? nearestBase(zmin(s),BASES);
    // FLAT (unlit) fill so every face of a zone renders the EXACT same colour — no
    // lighting wash that made horizontal (roof/floor) faces read near-white. Edges keep form.
    const m=new THREE.MeshBasicMaterial({side:THREE.DoubleSide, transparent:true, opacity:1});
    const parts = Object.prototype.hasOwnProperty.call(WALL_PARTS,s.name) ? WALL_PARTS[s.name] : [{verts:s.verts,holes:[]}];
    parts.forEach((part,partIndex)=>{
      const dup=part.duplicate_at_rest ?? isDup(s);
      const enclosureCondition=part.enclosure_condition || 'physical';
      // Unknown patches are represented by the enclosure-region helper below.
      // Skipping their projected wall part prevents two coincident viewer faces;
      // the authoritative source boundary and display projection remain untouched.
      if(enclosureCondition==='unknown') return;
      const mesh=new THREE.Mesh(ringGeom(part.verts,part.holes||[]), m.clone());
      mesh.userData={zone, floor:fi, type:s.type||'Wall', name:s.name, kind:'surface', dup,
        enclosureCondition, publicName:(NAMES.parts||{})[s.name]?.[partIndex] || objectName(s.name),
        area:polyArea(part.verts)-(part.holes||[]).reduce((sum,r)=>sum+polyArea(r),0)};
      surfMeshes.push(mesh); root.add(mesh);
      [part.verts,...(part.holes||[])].forEach(ring=>{
        const em=new THREE.LineSegments(edgeGeom(ring), new THREE.LineBasicMaterial({color:0x303030}));
        em.userData={zone, floor:fi, dup}; edgeSegs.push(em); root.add(em);
      });
    });
    logicalLines.push(dashedEdge(s.verts,LOGICAL_COLOR,radius*0.018,radius*0.012,
      {zone,floor:fi,kind:'logical',dup:false}));
    m.dispose();
  });
  const enclosureRegionKeys=new Set();
  function enclosureRegionKey(ring){
    return ring.map(v=>v.map(x=>Math.round(x*1e6)/1e6).join(',')).sort().join('|');
  }
  ENC_REGIONS.forEach((r,regionIndex)=>{
    if(!(r.verts||[]).length) return;
    const sourceBoundary=SOURCE_BOUNDARIES[r.boundary_id] || {};
    const zone=r.space_id || sourceBoundary.space_id || '?';
    const fi=zoneFloor[zone] ?? nearestBase(Math.min(...r.verts.map(v=>v[2])),BASES);
    const key=enclosureRegionKey(r.verts), duplicate=enclosureRegionKeys.has(key);
    enclosureRegionKeys.add(key);
    const userData={zone,floor:fi,kind:'enclosure-region',condition:r.condition,dup:duplicate,
      publicName:(NAMES.regions||[])[regionIndex] || objectName(r.boundary_id),
      boundaryId:r.boundary_id,area:polyArea(r.verts),sourceRefs:r.source_refs||[],assumptions:r.assumptions||[],
      evidenceKind:r.evidence_kind,baseColor:ENCLOSURE_COLOR};
    // This translucent face is a viewer-only aid. It makes an open extent easy
    // to see without restoring a physical wall, changing volume, or connectivity.
    const material=new THREE.MeshBasicMaterial({color:ENCLOSURE_COLOR,side:THREE.DoubleSide,
      transparent:true,opacity:ENCLOSURE_OPACITY,depthWrite:false});
    const mesh=new THREE.Mesh(ringGeom(r.verts,r.holes||[]),material); mesh.userData=userData;
    enclosureMeshes.push(mesh); root.add(mesh);
    enclosureLines.push(dashedEdge(r.verts,ENCLOSURE_COLOR,radius*0.012,radius*0.008,{...userData}));
  });
  WINS.forEach(w=>{
    const zone=zoneOfWindow(w), sv=popOut(w.verts, zone);  // proud of wall → clean + pickable
    const m=new THREE.MeshStandardMaterial({color:WINDOW_COLOR, side:THREE.DoubleSide, roughness:0.4});
    const mesh=new THREE.Mesh(ringGeom(sv), m);
    mesh.userData={zone, floor:zoneFloor[zone]??nearestBase(Math.min(...w.verts.map(v=>v[2])),BASES), type:'Window', name:w.name, parent:w.parent, kind:'window', dup:false, area:polyArea(w.verts)};
    winMeshes.push(mesh); root.add(mesh);
    // Comparison contours use the exact source geometry, without the picking offset.
    const edge=new THREE.LineSegments(edgeGeom(w.verts),new THREE.LineBasicMaterial({color:0x087d3b}));
    edge.userData={...mesh.userData}; windowEdges.push(edge); root.add(edge);
  });
  OPENS.forEach(o=>{
    const zone=_surfZoneByName[o.parent], dup=Boolean(o.partner && o.name>o.partner);
    const color=o.kind==='door'?0xa5672a:0x16877b;
    const m=new THREE.MeshBasicMaterial({color,side:THREE.DoubleSide,transparent:true,
      opacity:o.state==='closed'?0.7:0.12,depthWrite:false});
    const mesh=new THREE.Mesh(ringGeom(o.verts),m);
    mesh.userData={zone,floor:zoneFloor[zone]||0,type:o.kind==='door'?'门':'空开口',name:o.name,
      parent:o.parent,kind:'opening',dup,area:polyArea(o.verts),baseColor:color,sourceId:o.source_opening_id,
      spaceId:o.space_id,otherSpaceId:o.other_space_id,state:o.state};
    openingMeshes.push(mesh);root.add(mesh);
    const em=new THREE.LineSegments(edgeGeom(o.verts),new THREE.LineBasicMaterial({color}));
    em.userData={zone,floor:zoneFloor[zone]||0,dup,kind:'opening',baseColor:color};edgeSegs.push(em);root.add(em);
  });
  const allMeshes = () => surfMeshes.concat(winMeshes,openingMeshes,enclosureMeshes);
  const allPickables = () => allMeshes().concat(enclosureLines);

  // The admitted mesh is an offline reference, never a source-BIM entity or pick target.
  const referenceMeshes=[], referenceGroup=new THREE.Group(); scene.add(referenceGroup);
  const reference=GEO.textured_mesh;
  $('meshControls').hidden=!reference;
  let textureFailures=0;
  if(reference){
    const loader=new THREE.TextureLoader();
    const textures=reference.textures.map(uri=>{
      const t=loader.load(uri,undefined,undefined,()=>{textureFailures++; applyFilter();});
      t.wrapS=t.wrapT=THREE.RepeatWrapping;
      // Payload RGB already includes the linear base-colour factor and is sRGB encoded.
      // The existing unlit viewer uses LinearEncoding for both maps and output.
      t.encoding=THREE.LinearEncoding; return t;
    });
    reference.parts.forEach(part=>{
      const geometry=new THREE.BufferGeometry();
      geometry.setAttribute('position',new THREE.Float32BufferAttribute(part.positions,3));
      geometry.setAttribute('uv',new THREE.Float32BufferAttribute(part.uv,2));
      geometry.setIndex(part.indices);
      const material=new THREE.MeshBasicMaterial({map:textures[part.texture],side:THREE.DoubleSide,
        polygonOffset:true,polygonOffsetFactor:1,polygonOffsetUnits:1});
      const mesh=new THREE.Mesh(geometry,material);
      mesh.userData={alphaMode:part.alpha_mode,alphaFactor:part.alpha_factor};
      referenceMeshes.push(mesh); referenceGroup.add(mesh);
    });
  }
  const meshActive = () => Boolean(reference && $('showMesh').checked && parseFloat($('explode').value)===0);
  function comparisonPlanes(){
    const p=activePlanes().slice(), f=parseInt($('floorSel').value,10), floor=sourceFloor(f);
    if(meshActive() && floor){
      p.push(new THREE.Plane(new THREE.Vector3(0,0,1),-floor.z_floor),
        new THREE.Plane(new THREE.Vector3(0,0,-1),floor.z_floor+floor.height));
    }
    return p;
  }

  function applyClipping(){ const p=comparisonPlanes();
    allMeshes().forEach(m=>{m.material.clippingPlanes=p; m.material.needsUpdate=true;});
    referenceMeshes.forEach(m=>{m.material.clippingPlanes=p; m.material.needsUpdate=true;});
    edgeSegs.concat(logicalLines,enclosureLines,windowEdges).forEach(e=>{e.material.clippingPlanes=p; e.material.needsUpdate=true;}); }

  // ---- geometric measures (on TRUE geometry) ----
  function polyArea(r){ let nx=0,ny=0,nz=0; const n=r.length;  // Newell → planar polygon area
    for(let i=0;i<n;i++){ const a=r[i],b=r[(i+1)%n]; nx+=(a[1]-b[1])*(a[2]+b[2]); ny+=(a[2]-b[2])*(a[0]+b[0]); nz+=(a[0]-b[0])*(a[1]+b[1]); }
    return Math.hypot(nx,ny,nz)/2; }
  const zoneVol={};  // closed-zone volume via divergence theorem over its faces
  SURF.forEach(s=>{ const z=s.zone||'?', r=s.verts; let v=zoneVol[z]||0;
    for(let i=1;i<r.length-1;i++){ const a=r[0],b=r[i],c=r[i+1];
      v += (a[0]*(b[1]*c[2]-b[2]*c[1]) - a[1]*(b[0]*c[2]-b[2]*c[0]) + a[2]*(b[0]*c[1]-b[1]*c[0]))/6; }
    zoneVol[z]=v; });
  Object.keys(zoneVol).forEach(z=>zoneVol[z]=Math.abs(zoneVol[z]));

  // ---- select-by modes + uniform-colour selection ----
  let selected=new Set(); const selGroup=new THREE.Group(); scene.add(selGroup);
  function refreshColors(){
    const mode=$('colorBy').value;
    surfMeshes.forEach(m=>{ let c; if(mode==='floor') c=FLOOR_COLORS[m.userData.floor%FLOOR_COLORS.length];
      else if(mode==='zone') c=roleColor(m.userData.zone);   // colour by room type
      else if(mode==='edge') c=WHITE; else c=TYPE_COLORS[m.userData.type] ?? 0xcccccc;
      m.userData.baseColor=c; });
    winMeshes.forEach(m=>m.userData.baseColor=WINDOW_COLOR);
    enclosureMeshes.forEach(m=>m.userData.baseColor=ENCLOSURE_COLOR);
    allPickables().forEach(m=>m.material.color.setHex(selected.has(m) ? SEL_COLOR : m.userData.baseColor));
    updateLegend(mode);
  }
  function clearSelGroup(){ while(selGroup.children.length) selGroup.remove(selGroup.children[0]); }
  function setSelection(arr, lbl){ clearSelGroup(); selected=new Set(arr);
    allPickables().forEach(m=>m.material.color.setHex(selected.has(m)?SEL_COLOR:(m.userData.baseColor??0xcccccc)));
    $('sel').innerHTML=lbl||''; $('sel').style.display=lbl?'block':'none'; }
  function clearSelection(){ setSelection([], ''); }

  // ---- measure (button; CONTINUOUS; CAD-style screen-space vertex snap on true geometry) ----
  let measuring=false, measurePts=[]; const mGroup=new THREE.Group(); scene.add(mGroup);
  // overlay helpers: always-on-top (depthTest off + depthWrite off + high renderOrder) so a
  // highlight is NEVER hidden behind the (transparent) faces — no need to rotate to see it.
  function topMat(color){ return new THREE.MeshBasicMaterial({color, depthTest:false, depthWrite:false, transparent:true}); }
  function topBall(p, color, rmul){ const s=new THREE.Mesh(new THREE.SphereGeometry(radius*(rmul||0.010),16,16), topMat(color));
    s.position.copy(p); s.renderOrder=999; return s; }
  // a THICK highlight line as a thin cylinder — WebGL ignores LineMaterial.linewidth, so a
  // real tube is the only way to get a visibly bold line that reads at any camera angle.
  function fatLine(a, b, color, rmul){ const dir=new THREE.Vector3().subVectors(b,a); const len=dir.length()||1e-6; const r=radius*(rmul||0.005);
    const m=new THREE.Mesh(new THREE.CylinderGeometry(r, r, len, 8), topMat(color));
    m.position.copy(a).addScaledVector(dir, 0.5);
    m.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0), dir.clone().normalize()); m.renderOrder=999; return m; }
  const snap=topBall(new THREE.Vector3(), 0x00e5ff, 0.011); snap.visible=false; scene.add(snap);
  const CAND=[];  // {v: true world Vector3, zone}
  SURF.forEach(s=>s.verts.forEach(v=>CAND.push({v:new THREE.Vector3(v[0],v[1],v[2]), zone:s.zone||'?'})));
  WINS.forEach(w=>{const z=zoneOfWindow(w); w.verts.forEach(v=>CAND.push({v:new THREE.Vector3(v[0],v[1],v[2]), zone:z}));});
  OPENS.forEach(o=>o.verts.forEach(v=>CAND.push({v:new THREE.Vector3(...v),zone:_surfZoneByName[o.parent]})));
  function clearPair(){ while(mGroup.children.length) mGroup.remove(mGroup.children[0]); measurePts=[]; }
  function clearMeasure(){ measuring=false; snap.visible=false; clearPair();
    $('meas').style.display='none'; renderer.domElement.style.cursor='default'; }
  function startMeasure(){ clearMeasure(); measuring=true; renderer.domElement.style.cursor='crosshair';
    $('meas').style.display='block'; $('meas').textContent='measure: snap vertex 1  (Esc to exit)'; }
  function marker(p){ mGroup.add(topBall(p, 0xd81b60, 0.011)); }
  // Picking follows wall opacity: when walls are OPAQUE you can only snap/select what is visibly
  // in front (a candidate hidden behind a face is skipped); turn DOWN wall opacity to reach the
  // geometry behind. Keeps the precise screen-nearest aim within the tolerance.
  const _ray=new THREE.Raycaster();
  function occluded(w){ const from=camera.position; const dir=w.clone().sub(from); const dist=dir.length()||1e-6;
    _ray.set(from, dir.divideScalar(dist)); _ray.near=0; _ray.far=dist-radius*0.01;   // only faces strictly IN FRONT of w
    return _ray.intersectObjects(surfMeshes.filter(m=>m.visible), false)
      .some(h=>comparisonPlanes().every(p=>p.distanceToPoint(h.point)>=-0.0001)); }
  function seeThrough(){ return parseFloat($('opacity').value) < 1; }   // transparent → allowed to reach behind
  function snapPick(cx, cy){ const r=renderer.domElement.getBoundingClientRect(); const thru=seeThrough();
    let best=null, bd=24*24;
    for(const c of CAND){ const w=c.v.clone().add(explodeOffset(c.zone)); const p=w.clone().project(camera);
      if(meshActive() && $('meshMode').value==='mesh') continue;
      if(!onFloor({zone:c.zone,floor:zoneFloor[c.zone]},parseInt($('floorSel').value,10))) continue;
      if(!comparisonPlanes().every(plane=>plane.distanceToPoint(w)>=-0.0001)) continue;
      if(p.z<-1||p.z>1) continue; const sx=(p.x*0.5+0.5)*r.width, sy=(-p.y*0.5+0.5)*r.height;
      const d=(sx-cx)**2+(sy-cy)**2; if(d<bd && (thru || !occluded(w))){ bd=d; best=w; } }
    return best; }

  // ---- edge select (screen-space nearest segment → length) ----
  const EDGES=[];  // {a,b: true world Vector3, zone, len, floor, dup, kind}
  function pushEdges(verts, zone, floor, dup, kind, name){ for(let i=0;i<verts.length;i++){ const a=verts[i], b=verts[(i+1)%verts.length];
    EDGES.push({a:new THREE.Vector3(a[0],a[1],a[2]), b:new THREE.Vector3(b[0],b[1],b[2]), zone, floor, dup, kind,
      publicName:(NAMES.edges||{})[name]?.[i] || objectName(name)+"_Edge"+(i+1), parentName:objectName(name), sourceName:name,
      len:Math.hypot(a[0]-b[0],a[1]-b[1],a[2]-b[2])}); } }
  SURF.forEach(s=>{ const z=s.zone||'?'; pushEdges(s.verts, z, zoneFloor[z]??nearestBase(zmin(s),BASES), isDup(s), 'surface', s.name); });
  WINS.forEach(w=>{ const z=zoneOfWindow(w); pushEdges(w.verts, z, zoneFloor[z]??nearestBase(Math.min(...w.verts.map(v=>v[2])),BASES), false, 'window', w.name); });
  OPENS.forEach(o=>{const z=_surfZoneByName[o.parent];pushEdges(o.verts,z,zoneFloor[z]||0,Boolean(o.partner&&o.name>o.partner),'opening',o.name);});
  // only pick an edge whose source face is currently shown (same predicate as applyFilter)
  function edgeVisible(e){ const f=parseInt($('floorSel').value,10); const exploded=parseFloat($('explode').value)>0;
    if(meshActive() && ($('meshMode').value==='mesh'||!$('showEdges').checked)) return false;
    if(!onFloor(e,f)) return false;
    if(!exploded && e.dup) return false;
    return (e.kind==='window') ? $('showWin').checked : (e.kind==='opening') ? $('showOpen').checked : $('showWalls').checked; }
  function segDist(px,py,ax,ay,bx,by){ const dx=bx-ax,dy=by-ay, L2=dx*dx+dy*dy||1;
    let t=((px-ax)*dx+(py-ay)*dy)/L2; t=Math.max(0,Math.min(1,t));
    return (px-(ax+t*dx))**2 + (py-(ay+t*dy))**2; }
  function edgePick(ev){ const r=renderer.domElement.getBoundingClientRect();
    const cx=ev.clientX-r.left, cy=ev.clientY-r.top; const thru=seeThrough(); let best=null, bd=18*18;
    for(const e of EDGES){ if(!edgeVisible(e)) continue; const o=explodeOffset(e.zone);
      const A=e.a.clone().add(o), B=e.b.clone().add(o);
      if(!comparisonPlanes().every(p=>p.distanceToPoint(A)>=-0.0001 && p.distanceToPoint(B)>=-0.0001)) continue;
      const pa=A.clone().project(camera), pb=B.clone().project(camera);
      if(pa.z<-1||pa.z>1||pb.z<-1||pb.z>1) continue;  // skip if EITHER endpoint is behind/clipped
      const ax=(pa.x*.5+.5)*r.width, ay=(-pa.y*.5+.5)*r.height, bx=(pb.x*.5+.5)*r.width, by=(-pb.y*.5+.5)*r.height;
      const d=segDist(cx,cy,ax,ay,bx,by);   // opaque: skip an edge hidden behind a face (use its midpoint)
      if(d<bd && (thru || !occluded(A.clone().add(B).multiplyScalar(0.5)))){ bd=d; best={e,A,B}; } }
    clearSelection(); if(!best) return;
    selGroup.add(fatLine(best.A, best.B, SEL_COLOR, 0.006));   // whole edge as one thick, always-on-top line
    $('sel').innerHTML='<div class="hh">edge</div>'+kv([['名称',best.e.publicName],['所属面',best.e.parentName],['所属房间',spaceName(best.e.zone)],['所属楼层',floorName(best.e.floor)],['length',best.e.len.toFixed(3)+' m']])+traceInfo([['源父对象 ID',(SOURCE_MAP.surfaces||{})[best.e.sourceName]||(SOURCE_MAP.windows||{})[best.e.sourceName]||(SOURCE_MAP.openings||{})[best.e.sourceName]||best.e.sourceName]]); $('sel').style.display='block'; }

  // ---- selection picking (face raycast, for click-select only) ----
  const raycaster=new THREE.Raycaster();
  raycaster.params.Line.threshold=radius*0.012;
  function pick(ev){ const r=renderer.domElement.getBoundingClientRect();
    const mouse=new THREE.Vector2(((ev.clientX-r.left)/r.width)*2-1, -((ev.clientY-r.top)/r.height)*2+1);
    raycaster.setFromCamera(mouse,camera);
    const hits=raycaster.intersectObjects(allPickables().filter(m=>m.visible),false)
      .filter(h=>comparisonPlanes().every(p=>p.distanceToPoint(h.point)>=-0.0001)); return hits.length?hits[0]:null; }
  // structured selection readout: a titled block of label→value rows (one per line)
  function kv(pairs){ return pairs.filter(p=>p[1]!=null && p[1]!=='').map(p=>row(p[0], esc(p[1]))).join(''); }
  function traceInfo(pairs){ const content=kv(pairs); return content?'<details class="trace"><summary>内部编号与原始记录</summary>'+content+'</details>':''; }
  function rawEvidence(items){ return (items||[]).map(x=>typeof x==='string'?x:JSON.stringify(x)).join('; '); }
  function evidenceText(items){ return referenceText(rawEvidence(items)); }
  function enclosureLabel(v){ return ({enclosed:'封闭',semi_open:'半开敞',open:'开敞',physical:'实体',mixed:'局部混合',unknown:'未知'})[v] || v; }
  function boundaryFor(u){ const bid=((SOURCE_MAP.surfaces||{})[u.name] || u.name); return SOURCE_BOUNDARIES[bid] || null; }
  function coverageFor(b){
    if(!b || !(b.vertices||[]).length) return '';
    const total=(polyArea(b.vertices)-(b.holes||[]).reduce((sum,h)=>sum+polyArea(h),0))||1, area={open:0,unknown:0};
    (b.enclosure_regions||[]).forEach(r=>{if(area[r.condition]!=null) area[r.condition]+=polyArea(r.vertices||[])-(r.holes||[]).reduce((sum,h)=>sum+polyArea(h),0);});
    if(!(b.enclosure_regions||[]).length){ if(b.enclosure==='open') area.open=total; if(b.enclosure==='unknown') area.unknown=total; }
    const physical=Math.max(0,total-area.open-area.unknown), rows=[];
    [['实体',physical],['开敞',area.open],['未知',area.unknown]].forEach(([k,a])=>{if(a>total*1e-8) rows.push(k+' '+(a/total*100).toFixed(0)+'%');});
    return rows.join(' · ');
  }
  function boundaryEvidence(b,key){ return evidenceText([...(b&&b[key]||[]),...(b&&b.enclosure_regions||[]).flatMap(r=>r[key]||[])]); }
  function boundaryEvidenceKinds(b){ return [...new Set((b&&b.enclosure_regions||[]).map(r=>r.evidence_kind).filter(Boolean))].join(', '); }
  function wallPropertyRows(b){
    if(!b || b.geometry_type!=='wall' || b.kind!=='physical') return [];
    const p=WALL_PROPERTIES[b.id]||{}, thickness=p.thickness_m;
    return [['墙厚',p.conflict?'厚度记录冲突，待确认':thickness==null?'未知':Number((thickness*1000).toFixed(3))+' mm'],
      ['厚度判定',({observed:'图纸读数',inferred:'推断',unknown:'未知'})[p.evidence_status]||'未注明'],
      ['取线位置',({centerline:'墙中心线',negative_face:'世界负方向侧墙面',positive_face:'世界正方向侧墙面',unknown:'未知'})[p.reference_side]||'未知'],
      ['厚度依据',evidenceText(p.basis||[])],['墙厚来源',evidenceText(p.source_refs||[])]];
  }
  function describe(mode,o){ const u=o.userData;
    if(mode==='floor') return '<div class="hh">楼层</div>'+kv([['名称',floorName(u.floor)],['底标高',BASES[u.floor]+' m']])+traceInfo([['源楼层 ID',NAMED_FLOORS[u.floor]?.id],['原楼层名',NAMED_FLOORS[u.floor]?.source_name]]);
    if(u.kind==='enclosure-region' && mode!=='zone'){ const boundary=SOURCE_BOUNDARIES[u.boundaryId];
      return '<div class="hh">'+(u.condition==='open'?'明确开敞区域':'围护未知区域')+'</div>'+kv([
        ['名称',u.publicName],['所属面',objectName(u.boundaryId)],['所属楼层',floorName(u.floor)],['空间',spaceName(u.zone)],['面积',u.area.toFixed(2)+' m²'],
        ['边界覆盖',coverageFor(boundary)],['证据类型',u.evidenceKind],
        ['假设',evidenceText(u.assumptions)]])+traceInfo([['源边界 ID',u.boundaryId],['来源',rawEvidence(u.sourceRefs)]]); }
    if(u.kind==='opening' && mode!=='zone') return '<div class="hh">'+esc(u.type)+'</div>'+kv([
      ['名称',objectName(u.name)],['所属面',objectName(u.parent)],['所属楼层',floorName(u.floor)],['所属房间',spaceName(u.zone)],['连通',spaceName(u.spaceId)+' ↔ '+(u.otherSpaceId?spaceName(u.otherSpaceId):'室外')],
      ['开闭状态',({open:'开放',closed:'关闭',unknown:'未确定'})[u.state]],['面积',u.area.toFixed(2)+' m²']])+traceInfo([['源开口 ID',u.sourceId],['显示对象 ID',u.name]]);
    if(mode==='zone'){ const r=roleOf(u.zone), sid=(SOURCE_MAP.zones||{})[u.zone]||u.zone, space=SOURCE_SPACES[sid],
      enclosureEvidence=space&&(space.enclosure_evidence||{}), roleEvidence=space&&space.role_evidence;
      return '<div class="hh">zone</div>'+kv([['名称',spaceName(u.zone)],['所属楼层',floorName(u.floor)],['功能',roleLabel(r)||'—'],
        ['功能判定',roleEvidence&&({observed:'图文明确',inferred:'推断',unknown:'待判定'})[roleEvidence.basis]],
        ['功能依据',roleEvidence&&evidenceText(roleEvidence.source_refs)],
        ['功能假设',roleEvidence&&evidenceText(roleEvidence.assumptions)],
        ['空间开敞性',space&&enclosureLabel(space.exposure||space.enclosure)],
        ['证据类型',enclosureEvidence&&enclosureEvidence.evidence_kind],
        ['假设',space&&evidenceText([...(space.assumptions||[]),...(enclosureEvidence.assumptions||[])])],
        ['volume',(zoneVol[u.zone]||0).toFixed(2)+' m³']])+traceInfo([['源空间 ID',sid],['来源',space&&rawEvidence([...(space.source_refs||[]),...(enclosureEvidence.source_refs||[])])]]); }
    // Area of the selected visible fragment: wall apertures are cut out;
    // windows remain separate child surfaces and are not subtracted here.
    const boundary=boundaryFor(u);
    return '<div class="hh">surface</div>'+kv([['名称',u.publicName||objectName(u.name)],['所属面',objectName(u.parent||u.name)],['所属房间',spaceName(u.zone)],['所属楼层',floorName(u.floor)],['type',u.type],
      ['显示语义',u.kind==='surface'?(u.enclosureCondition==='unknown'?'未知围护（未当作实体墙）':'实体围护'):'窗'],
      ...(u.type==='Wall'?wallPropertyRows(boundary):[]),
      ['边界围护',boundary&&enclosureLabel(boundary.enclosure||boundary.kind)],['边界覆盖',coverageFor(boundary)],
      ['证据类型',boundaryEvidenceKinds(boundary)],['假设',boundaryEvidence(boundary,'assumptions')],
      ['area',(u.area||0).toFixed(2)+' m²'], ['note', u.type==='Wall'?'当前实体片面积；开敞或未知区域另以半透明灰色辅助面显示':'']])+traceInfo([
      ['源对象 ID',(SOURCE_MAP.surfaces||{})[u.name] || (SOURCE_MAP.windows||{})[u.name] || u.name],
      ['来源',boundary&&rawEvidence([...(boundary.source_refs||[]),...(boundary.enclosure_regions||[]).flatMap(r=>r.source_refs||[])])]]);
  }
  function handleClick(ev){
    if(measuring){ const r=renderer.domElement.getBoundingClientRect();
      const p=snapPick(ev.clientX-r.left, ev.clientY-r.top); if(!p) return;
      if(measurePts.length>=2) clearPair();  // continuous: a new click starts a fresh pair (only latest shown)
      marker(p); measurePts.push(p);
      if(measurePts.length<2){ $('meas').textContent='measure: snap vertex 2  (Esc to exit)'; }
      else { const d=measurePts[0].distanceTo(measurePts[1]);
        mGroup.add(fatLine(measurePts[0], measurePts[1], 0xd81b60, 0.004));  // always-on-top distance line
        $('meas').textContent='distance: '+d.toFixed(3)+' m  (click=next, Esc=exit)'; }
      return; }
    const mode=$('colorBy').value;
    if(mode==='edge'){ edgePick(ev); return; }
    const hit=pick(ev); if(!hit){ clearSelection(); return; }
    const o=hit.object;
    let sel; if(mode==='floor') sel=allPickables().filter(m=>m.userData.floor===o.userData.floor);
    else if(mode==='zone') sel=allPickables().filter(m=>m.userData.zone===o.userData.zone); else sel=[o];
    setSelection(sel, describe(mode,o));
  }
  // click vs orbit-drag: only a near-stationary press is a click
  let downXY=null;
  renderer.domElement.addEventListener('pointerdown', e=>{ if(e.button===0) downXY=[e.clientX,e.clientY]; });
  renderer.domElement.addEventListener('pointerup', e=>{ if(e.button!==0||!downXY) return;
    const moved=Math.hypot(e.clientX-downXY[0], e.clientY-downXY[1]); downXY=null; if(moved<=5) handleClick(e); });
  renderer.domElement.addEventListener('pointermove', e=>{ if(!measuring) return;
    const r=renderer.domElement.getBoundingClientRect(); const p=snapPick(e.clientX-r.left, e.clientY-r.top);
    if(p){ snap.position.copy(p); snap.visible=true; } else snap.visible=false; });

  // ---- display toggles + explode ----
  // At explode=0 the duplicate of each coincident reciprocal pair is HIDDEN (solid
  // clean shell, no gaps, no z-fight); at explode>0 ALL faces show (each zone a
  // closed box) separated by the offset.
  function applyFilter(){ const f=parseInt($('floorSel').value,10);
    const exploded=parseFloat($('explode').value)>0;
    const comparison=meshActive(), mode=$('meshMode').value;
    const fills=!comparison || mode==='blend', contours=!comparison || mode!=='mesh';
    const sw=$('showWalls').checked, swin=$('showWin').checked, se=$('showEdges').checked;
    const okF=(u)=>onFloor(u,f) && (exploded || !u.dup);
    surfMeshes.forEach(m=>m.visible = fills && sw && okF(m.userData));
    winMeshes.forEach(m=>m.visible = fills && swin && okF(m.userData));
    openingMeshes.forEach(m=>m.visible=fills && $('showOpen').checked && okF(m.userData));
    enclosureMeshes.forEach(m=>m.visible=fills && $('showEnclosure').checked && okF(m.userData));
    edgeSegs.forEach(e=>{e.visible = contours && se && okF(e.userData) && (e.userData.kind!=='opening'||$('showOpen').checked);
      e.material.color.setHex(e.userData.kind==='opening' ? (comparison?0xec820e:e.userData.baseColor) : (comparison?0xc51b80:0x303030));});
    windowEdges.forEach(e=>e.visible=comparison && contours && se && swin && okF(e.userData));
    logicalLines.forEach(e=>e.visible=contours && $('showLogical').checked && okF(e.userData));
    enclosureLines.forEach(e=>e.visible=contours && $('showEnclosure').checked && okF(e.userData));
    referenceGroup.visible=comparison;
    referenceMeshes.forEach(m=>{const v=parseFloat($('meshOpacity').value)*m.userData.alphaFactor;
      m.material.opacity=v; m.material.transparent=v<1||m.userData.alphaMode==='BLEND';
      m.material.depthWrite=!m.material.transparent;});
    $('meshStatus').textContent=textureFailures?'贴图加载失败，请重新打开完整 HTML。':
      ($('showMesh').checked&&exploded?'展开时暂停贴图叠加':
      (comparison?'粉色：BIM边界 · 绿色：窗 · 橙色：门；遮挡处可剖切查看。':'原始贴图按已保存坐标对齐，可随剖切和楼层筛选。'));
    applyClipping(); }
  function explodeOffset(zone){ const amt=parseFloat($('explode').value); if(amt<=0) return new THREE.Vector3();
    if($('explodeMode').value==='floor') return new THREE.Vector3(0,0, (zoneFloor[zone]||0)*amt*radius*1.0);
    return (zoneDir[zone]||new THREE.Vector3()).clone().multiplyScalar(amt*radius*1.2);  // zone: full 3D radial
  }
  function applyExplode(){ surfMeshes.forEach(m=>m.position.copy(explodeOffset(m.userData.zone)));
    winMeshes.forEach(m=>m.position.copy(explodeOffset(m.userData.zone)));
    openingMeshes.forEach(m=>m.position.copy(explodeOffset(m.userData.zone)));
    enclosureMeshes.forEach(m=>m.position.copy(explodeOffset(m.userData.zone)));
    edgeSegs.concat(logicalLines,enclosureLines).forEach(e=>e.position.copy(explodeOffset(e.userData.zone)));
    applyFilter(); }  // re-evaluate dup visibility when crossing explode 0 ↔ >0

  // ---- right-side info (structured) ----
  function row(k,v){ return '<div class="kv"><span>'+k+'</span><b>'+v+'</b></div>'; }
  $('hud').innerHTML =
    '<div class="hh">MODEL</div>' + row('zones',(GEO.zones||[]).length) + row('surfaces',SURF.length) + row('windows',WINS.length) +
    '<div class="hh">BOUNDING BOX</div>' + row('width (x)',size.x.toFixed(2)+' m') + row('depth (y)',size.y.toFixed(2)+' m') +
    row('height (z)',size.z.toFixed(2)+' m') + row(SOURCE?'源高度分组':'floors',BASES.length);
  if(SOURCE) $('hud').innerHTML += '<div class="hh">源建筑模型</div>' +
    row('源空间',SOURCE.spaces.length) + row('源边界',SOURCE.boundaries.length) +
    row('源开口',SOURCE.openings.length) + row((SOURCE.schema_version||'').startsWith('source_bim_v')?'源几何检查':'派生映射检查',esc(SOURCE.validation.status)) +
    row('已记录的门/开口连接',(SOURCE.connections||[]).length) +
    (SOURCE.schema_version==='source_bim_v3' ? row('半开敞 / 开敞空间',SOURCE.spaces.filter(s=>['semi_open','open'].includes(s.enclosure)).length) +
      row('开敞 / 未知边界',SOURCE.boundaries.filter(b=>['open','unknown','mixed'].includes(b.enclosure)).length) : '') +
    '<p>逻辑闭合只界定空间范围，不表示实体密闭或热区。半透明灰色是开敞或未知围护的查看辅助面；点选后可区分语义与来源。</p>';

  // ---- room-type legend (shown in zone mode: colour swatch → room type) ----
  function updateLegend(mode){
    const el=$('legend'); if(!el) return;
    if(mode!=='zone' || !HAS_ROLES){ el.style.display='none'; el.innerHTML=''; return; }
    const present=[], seen=new Set();
    (GEO.zones||[]).forEach(z=>{ const r=roleOf(z)||'untyped'; if(!seen.has(r)){ seen.add(r); present.push(r);} });
    present.sort();
    let h='';
    present.forEach(r=>{ const c=(r==='untyped')?ROLE_DEFAULT:(ROLE_COLORS[r]??ROLE_DEFAULT);
      h+='<div class="lg"><span class="sw" style="background:'+hex6(c)+'"></span>'+esc(roleLabel(r))+'</div>'; });
    el.innerHTML=h; el.style.display='block';
  }

  // ---- section controls ----
  const secDiv=$('sections'); secDiv.innerHTML='<h2>section cuts</h2>';
  AX.forEach(a=>{ const wrap=document.createElement('div'); wrap.className='sec'; const step=((a.max-a.min)/200)||0.01;
    wrap.innerHTML='<label class="chk"><input type="checkbox" id="en'+a.key+'"> cut '+a.key+'</label>'+
      '<input type="range" id="pos'+a.key+'" min="'+a.min+'" max="'+a.max+'" step="'+step+'" value="'+a.max+'">'+
      '<label class="chk"><input type="checkbox" id="flip'+a.key+'"> flip side</label>';
    secDiv.appendChild(wrap);
    const refresh=()=>{a.enabled=$('en'+a.key).checked; a.flip=$('flip'+a.key).checked; a.pos=parseFloat($('pos'+a.key).value); updatePlane(a); applyClipping();};
    $('en'+a.key).onchange=refresh; $('pos'+a.key).oninput=refresh; $('flip'+a.key).onchange=refresh; });

  // ---- floor select + wiring ----
  const fs=$('floorSel'); BASES.forEach((b,i)=>{const o=document.createElement('option'); o.value=i; o.textContent=floorName(i)+' (z='+b.toFixed(2)+')'; fs.appendChild(o);});
  $('colorBy').onchange=()=>{ refreshColors(); clearSelection(); }; fs.onchange=applyFilter;
  $('showWalls').onchange=applyFilter; $('showWin').onchange=applyFilter; $('showEdges').onchange=applyFilter;
  $('showOpen').onchange=applyFilter; $('showLogical').onchange=applyFilter; $('showEnclosure').onchange=applyFilter;
  $('showMesh').onchange=applyFilter; $('meshMode').onchange=applyFilter; $('meshOpacity').oninput=applyFilter;
  $('opacity').oninput=e=>{const v=parseFloat(e.target.value); surfMeshes.forEach(m=>{
    m.material.opacity=v; m.material.transparent=v<1;});};
  $('explode').oninput=applyExplode; $('explodeMode').onchange=applyExplode;
  $('measure').onclick=()=>{ measuring ? clearMeasure() : startMeasure(); };  // toggle
  $('clearMeasure').onclick=clearMeasure;
  window.addEventListener('keydown', e=>{ if(e.key==='Escape' && measuring) clearMeasure(); });
  $('reset').onclick=defaultView;
  $('savePng').onclick=()=>{ renderer.render(scene,camera); const a=document.createElement('a');
    a.download='geometry_view.png'; a.href=renderer.domElement.toDataURL('image/png'); a.click(); };

  refreshColors(); applyFilter(); applyExplode(); applyClipping(); defaultView();
  window.addEventListener('resize',()=>{ camera.aspect=window.innerWidth/window.innerHeight; camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight); });
  (function loop(){ requestAnimationFrame(loop); controls.update(); renderer.render(scene,camera); })();
})();
"""

_PANEL_HTML = r"""
<div id="panel">
  <h1>Geometry inspection</h1>
  <div class="subtitle">__TITLE__</div>

  <h2>select by</h2>
  <select id="colorBy"><option value="zone">zone</option><option value="floor">floor</option><option value="surface">surface</option><option value="edge">edge</option></select>

  <h2>楼层 / 高度范围</h2>
  <select id="floorSel"><option value="-1">all floors</option></select>

  <div id="meshControls" hidden>
    <h2>识别对照</h2>
    <label class="chk"><input type="checkbox" id="showMesh"> 叠加原始 textured mesh</label>
    <select id="meshMode"><option value="outline">贴图 + BIM轮廓</option><option value="blend">贴图 + BIM实体</option><option value="mesh">仅贴图</option></select>
    <label class="rng">贴图透明度</label>
    <input type="range" id="meshOpacity" min="0.1" max="1" step="0.05" value="1">
    <p id="meshStatus"></p>
    <p>贴图用于视觉对照；半透明区域不作颜色或表面量测。</p>
  </div>

  <h2>display</h2>
  <label class="rng">wall opacity</label>
  <input type="range" id="opacity" min="0.05" max="1" step="0.05" value="1">
  <label class="rng">exploded view</label>
  <select id="explodeMode"><option value="floor">by floor</option><option value="zone">by zone</option></select>
  <input type="range" id="explode" min="0" max="1" step="0.02" value="0">
  <label class="chk"><input type="checkbox" id="showWalls" checked> wall / floor / roof faces</label>
  <label class="chk"><input type="checkbox" id="showWin" checked> windows</label>
  <label class="chk"><input type="checkbox" id="showOpen" checked> 门 / 空开口</label>
  <label class="chk"><input type="checkbox" id="showEdges" checked> edges</label>
  <label class="chk"><input type="checkbox" id="showLogical"> 逻辑空间边界（细虚线）</label>
  <label class="chk"><input type="checkbox" id="showEnclosure" checked> 开敞 / 未知辅助面</label>
  <div class="enclosure-key"><span></span>开敞 / 未知（点选查看类型）</div>

  <div id="sections"></div>

  <h2>tools</h2>
  <button id="measure">measure (vertex &rarr; vertex)</button>
  <button id="clearMeasure">clear measure</button>
  <button id="savePng">save PNG</button>
  <button id="reset">reset view</button>
</div>
<div id="rinfo">
  <div id="hud"></div>
  <div id="legend"></div>
  <div id="sel"></div>
  <div id="meas"></div>
</div>
"""

_STYLE = r"""
  * { box-sizing: border-box; }
  html, body { margin:0; height:100%; overflow:hidden; font:13px/1.4 system-ui, sans-serif; }
  #app { position:fixed; inset:0; }
  #panel { position:absolute; top:14px; left:14px; width:280px; max-height:calc(100% - 28px); overflow-y:auto;
    background:rgba(255,255,255,0.96); border:1px solid #d7dbe0; border-radius:10px; padding:16px 18px;
    box-shadow:0 4px 16px rgba(0,0,0,0.14); color:#222; }
  #panel h1 { font-size:16px; font-weight:600; margin:0 0 2px; }
  #panel .subtitle { color:#8a9099; font-size:12px; margin-bottom:6px; }
  #panel .hint { color:#8a9099; font-size:11.5px; margin:5px 0 2px; }
  #panel h2 { font-size:11px; font-weight:700; margin:18px 0 7px; color:#5b6b86; text-transform:uppercase; letter-spacing:.06em; }
  #panel label { display:block; }
  #panel label.rng { font-size:12.5px; color:#555; margin:10px 0 5px; }
  #panel label.chk { font-size:13px; margin:7px 0; cursor:pointer; }
  #panel label.chk input { margin-right:7px; vertical-align:-1px; }
  .enclosure-key { color:#667085; font-size:11.5px; margin:5px 0 0 2px; }
  .enclosure-key span { width:13px; height:3px; display:inline-block; margin:0 4px 2px 0;
    background:#8b949e; opacity:.65; }
  #panel input[type=range] { width:100%; margin:2px 0 4px; accent-color:#3b6ea5; }
  #panel select { width:100%; padding:6px 8px; font-size:13px; border:1px solid #cfd4da; border-radius:6px; background:#fff; }
  #panel button { width:100%; padding:8px; margin-top:8px; font-size:13px; cursor:pointer;
    border:1px solid #cfd4da; border-radius:7px; background:#f4f6f8; color:#2a3340; transition:background .12s; }
  #panel button:hover { background:#e7ebf0; }
  .sec { border-top:1px solid #ebedf0; padding-top:9px; margin-top:9px; }
  #rinfo { position:absolute; top:12px; right:12px; width:340px; max-height:calc(100% - 24px); overflow-y:auto; display:flex; flex-direction:column; gap:10px; }
  #hud { background:rgba(20,28,44,0.88); color:#eef; padding:12px 16px; border-radius:8px; font-size:15px; }
  #hud .hh { font-size:12px; letter-spacing:.05em; color:#8fb0e0; margin:8px 0 3px; text-transform:uppercase; }
  #hud .hh:first-child { margin-top:0; }
  #hud .kv { display:flex; justify-content:space-between; padding:2px 0 2px 10px; }
  #hud .kv span { color:#c7d2e0; }
  #hud .kv b { color:#fff; font-weight:600; }
  #legend { display:none; background:rgba(20,28,44,0.88); color:#eef; padding:10px 16px; border-radius:8px; font-size:13px; }
  #legend .hh { font-size:12px; letter-spacing:.05em; color:#8fb0e0; margin:0 0 6px; text-transform:uppercase; }
  #legend .lg { display:flex; align-items:center; gap:8px; padding:2px 0; text-transform:capitalize; }
  #legend .sw { width:14px; height:14px; border-radius:3px; border:1px solid rgba(255,255,255,0.4); display:inline-block; flex:0 0 auto; }
  #sel { display:none; background:rgba(38,50,80,0.92); color:#eef; padding:12px 16px; border-radius:8px; font-size:14px; }
  #sel .hh { font-size:12px; letter-spacing:.05em; color:#9fc0ef; margin:0 0 5px; text-transform:uppercase; }
  #sel .kv { display:flex; justify-content:space-between; gap:16px; padding:3px 0 3px 10px; }
  #sel .kv span { color:#c7d2e0; }
  #sel .kv b { color:#fff; font-weight:600; text-align:right; word-break:break-word; }
  #sel .kv span { flex-shrink:0; }
  #sel .trace { border-top:1px solid #64708a; margin-top:10px; padding-top:8px; }
  #sel .trace summary { cursor:pointer; color:#c7d2e0; font-size:12px; }
  #meas { display:none; background:#d81b60; color:#fff; padding:11px 16px; border-radius:8px; font-size:19px; font-weight:700; box-shadow:0 2px 8px rgba(216,27,96,0.45); }
"""

_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Geometry inspection — __TITLE__</title>
<style>__STYLE__</style></head>
<body>
<div id="app"></div>
__PANEL__
<script>__THREE_JS__</script>
<script>__ORBIT_JS__</script>
<script>window.GEO = __GEO_JSON__;</script>
<script>__APP_JS__</script>
</body></html>
"""


def app_js() -> str:
    """The viewer app script (for embedding + standalone syntax checking)."""
    return _APP_JS


def discover_roles(bg_path: Path) -> dict:
    """zone-name -> room role (office/meeting/corridor/...).

    Prefer building_geometry.json zone_meta because deterministic public zone
    names intentionally differ from correction Cell.id. Fall back to the legacy
    sibling correction lookup for older artifacts.
    """
    try:
        bg = json.loads(bg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        bg = {}
    roles = {
        str(m["name"]): str(m["role"])
        for m in bg.get("zone_meta", [])
        if m.get("name") and m.get("role")
    }
    if roles:
        return roles
    candidates = [
        bg_path.parent.parent / "1_correction" / "correction_geometry.json",  # <run>/2_modelling/bg.json
        bg_path.parent / "correction_geometry.json",
        bg_path.with_name("correction_geometry.json"),
    ]
    for cg in candidates:
        if not cg.exists():
            continue
        try:
            d = json.loads(cg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        roles = {}
        for fl in d.get("floors", []):
            for c in fl.get("cells", []):
                cid, role = c.get("id"), c.get("role")
                if cid and role:
                    roles[str(cid)] = str(role)
        return roles
    return {}


def wall_properties(source: dict) -> dict:
    """Project optional thickness evidence for selection, including unknowns.

    Component edits and reading evidence can coexist. If they disagree, expose
    that conflict instead of silently choosing one value in the viewer.
    """
    result = {}
    records = [*source.get("component_attributes", []), *source.get("wall_references", [])]
    for boundary in source.get("boundaries", []):
        if boundary.get("geometry_type") != "wall" or boundary.get("kind") != "physical":
            continue
        rows = [row for row in records
                if boundary["id"] in row.get("boundary_ids", [row.get("boundary_id")])]
        values = {row["thickness_m"] for row in rows if row.get("thickness_m") is not None}
        statuses = {row["evidence_status"] for row in rows if row.get("evidence_status")}
        sides = {row["reference_side"] for row in rows if row.get("reference_side")}
        result[boundary["id"]] = {
            "thickness_m": next(iter(values)) if len(values) == 1 else None,
            "conflict": len(values) > 1,
            "evidence_status": next(iter(statuses)) if len(statuses) == 1 else ("unknown" if not values else None),
            "reference_side": next(iter(sides)) if len(sides) == 1 else "unknown",
            "basis": list(dict.fromkeys(row.get("basis") or row.get("reference_basis")
                                        for row in rows if row.get("basis") or row.get("reference_basis"))),
            "source_refs": list(dict.fromkeys(ref for row in rows for ref in row.get("source_refs", []))),
        }
    return result


def build_viewer_html(data: dict, *, title: str = "building geometry", roles: dict | None = None,
                      mesh_path: Path | None = None) -> str:
    three_js = (_VENDOR / "three.min.js").read_text(encoding="utf-8")
    orbit_js = (_VENDOR / "OrbitControls.js").read_text(encoding="utf-8")
    from src.agent.geometry.openings import visible_wall_parts

    geo = {
        "zones": data.get("zones", []),
        "surfaces": data.get("surfaces", []),
        "windows": data.get("windows", []),
        "openings": data.get("openings", []),
        "visible_wall_parts": data["display_surface_parts"] if "display_surface_parts" in data else visible_wall_parts(data),
        "enclosure_regions": data.get("enclosure_regions", []),
        "roles": roles if roles is not None else data.get("roles", {}),
        "source_model": data.get("source_model"),
    }
    from src.agent.roles import ROOM_TYPES, normalize
    from src.agent.geometry.source_naming import viewer_names
    geo["roles"] = {sid: normalize(role) or "unknown" for sid, role in geo["roles"].items()}
    geo["room_types"] = ROOM_TYPES
    geo["public_names"] = viewer_names(data, geo["visible_wall_parts"])
    source = data.get("source_model") or {}
    if mesh_path is not None:
        from src.agent.geometry.mesh_observation import MeshObservation
        geo["textured_mesh"] = MeshObservation(mesh_path).viewer_payload(source.get("mesh_frame"))
    geo["wall_properties"] = wall_properties(source)
    spaces = {row["id"]: row for row in source.get("spaces", [])}
    source_floors = {row["id"]: row for row in source.get("floors", [])}
    zone_map = source.get("derived", {}).get("zones", {})
    named_floors = geo["public_names"]["floors"]
    memberships = {}
    for zone in geo["zones"]:
        sid = zone_map.get(zone, zone)
        space = spaces.get(sid)
        if not space:
            continue
        indices = [i for i, floor in enumerate(named_floors)
                   if space["floor_id"] == floor["id"]
                   or sid in source_floors.get(floor["id"], {}).get("spanning_space_ids", [])]
        if indices:
            memberships[zone] = indices
    geo["floor_memberships"] = memberships
    safe_title = html.escape(title)  # HTML-context (title tag + panel text)
    from scripts.tool_scripts.bim_agent_delivery_display import render_regularization_html
    regularization_html = render_regularization_html(source, data.get("regularization_report"))
    panel = _PANEL_HTML.replace("__TITLE__", safe_title)
    if regularization_html:
        audit_panel = ('<details style="margin-top:14px;max-width:100%;overflow-wrap:anywhere">'
                       '<summary>几何规整清单</summary><div style="overflow-x:auto">'
                       + regularization_html + '</div></details>')
        panel = panel.replace('<h2>tools</h2>', audit_panel + '<h2>tools</h2>')
    return (
        _HTML
        .replace("__STYLE__", _STYLE)
        .replace("__PANEL__", panel)
        .replace("__THREE_JS__", three_js)
        .replace("__ORBIT_JS__", orbit_js)
        .replace("__GEO_JSON__", _js_embed(geo))  # script-safe (no </script> breakout)
        .replace("__APP_JS__", _APP_JS)
        .replace("__TITLE__", safe_title)
    )


def load_viewer_geometry(path: Path) -> dict:
    """Load either a display geometry file or an authoritative source BIM.

    Source BIM inputs go through their validated display projection.  Unknown
    source schema versions fail rather than opening a plausible-looking empty
    legacy viewer.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    schema = data.get("schema_version")
    if schema in {"source_bim_v2", "source_bim_v3"}:
        from src.agent.geometry.source_bim import source_view_geometry

        display = source_view_geometry(data)
        audit_path = path.with_name("regularization_report.json")
        if audit_path.is_file():
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            if audit.get("source_model_sha256") == data.get("source_model_sha256"):
                display["regularization_report"] = audit
        return display
    if isinstance(schema, str) and schema.startswith("source_bim"):
        raise ValueError(f"unsupported source BIM schema: {schema}")

    source_path = path.with_name("source_model.json")
    if source_path.exists() and not data.get("source_model"):
        from src.agent.geometry.source_model import _digest

        source = json.loads(source_path.read_text(encoding="utf-8"))
        # Legacy building_geometry carries a digest of that derived geometry.
        if source.get("derived_geometry_sha256") == _digest(data):
            data["source_model"] = source
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("json", help="display/building geometry JSON, or validated source_bim_v2/v3 source_model.json")
    ap.add_argument("--out", help="output HTML (default: <json dir>/geometry_viewer.html)")
    ap.add_argument("--title", default="")
    ap.add_argument("--mesh", type=Path, help="admitted original GLB; requires a matching source mesh_frame")
    ap.add_argument("--roles", help="optional JSON {zone: role} to colour zones by room type; "
                                    "default uses building_geometry zone_meta, then legacy correction lookup")
    args = ap.parse_args()
    j = Path(args.json)
    data = load_viewer_geometry(j)
    if args.roles:
        roles = json.loads(Path(args.roles).read_text(encoding="utf-8"))
    else:
        roles = discover_roles(j) or None
    out = Path(args.out) if args.out else j.with_name("geometry_viewer.html")
    title = args.title or j.parent.parent.name or j.stem
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_viewer_html(data, title=title, roles=roles, mesh_path=args.mesh), encoding="utf-8")
    if roles:
        print(f"  room-type roles: {len(roles)} zones (colour-by-type enabled)")
    kb = out.stat().st_size // 1024
    print(f"wrote {out}  ({kb} KB, offline; orbit / opacity / sections / explode / vertex-measure)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
