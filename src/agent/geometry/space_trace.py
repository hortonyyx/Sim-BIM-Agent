"""Preview model-observed pixel contours; no image inference or source mutation."""
from __future__ import annotations

import math
from PIL import Image, ImageDraw
from shapely.geometry import LineString, Polygon
from src.agent.geometry.source_image_overlay import _axis_anchors


def render_space_trace(image: Image.Image, *, polygon_pixels: list, openings: list,
                       x_anchors: list, y_anchors: list, basis: str) -> tuple[Image.Image, dict]:
    """Validate/preview an ordered full room ring and wall-hosted opening segments.

    Polygon closes logically across openings. Aperture endpoints lie on the
    closed boundary, unlike a swing arc/leaf tip. Pixel support and semantic
    identity are not validated here; the overlay is for visual review.
    """
    if not isinstance(basis, str) or not basis.strip():
        raise ValueError('basis must explain the observed reference planes and calibration')
    def point(p):
        if (not isinstance(p, list) or len(p) != 2 or
                any(type(v) not in (int, float) or not math.isfinite(v) for v in p)):
            raise ValueError('points must be finite [x,y] original-image pixels')
        if not (0 <= p[0] < image.width and 0 <= p[1] < image.height):
            raise ValueError('point outside original image')
        return list(p)
    if not isinstance(polygon_pixels, list) or len(polygon_pixels) < 4:
        raise ValueError('polygon_pixels requires an ordered full room ring, at least four vertices')
    ring = [point(p) for p in polygon_pixels]
    if ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) < 4:
        raise ValueError('at least four distinct ring vertices required')
    if not isinstance(openings, list):
        raise ValueError('openings must be a list of {id,p1,p2}')
    poly = Polygon(ring)
    errors = []
    if not poly.is_valid or poly.area == 0:
        errors.append('room contour is self-intersecting or has zero area')
    for i,(p,q) in enumerate(zip(ring,ring[1:]+ring[:1])):
        if p == q or (p[0] != q[0] and p[1] != q[1]):
            errors.append(f'edge {i+1} must be nonzero and horizontal or vertical')
    parsed = []
    ids = set()
    for o in openings:
        if not isinstance(o,dict) or set(o) != {'id','p1','p2'}:
            raise ValueError('each opening must contain only id,p1,p2')
        if not isinstance(o['id'],str) or not o['id'].strip() or o['id'] in ids:
            raise ValueError('each opening needs a unique nonblank id')
        ids.add(o['id'])
        p,q = point(o['p1']),point(o['p2'])
        segment = LineString([p,q])
        if p == q or (p[0] != q[0] and p[1] != q[1]):
            errors.append(f"opening {o['id']} must be a nonzero horizontal/vertical aperture, not a swing arc")
        if not poly.boundary.buffer(1e-7).covers(segment):
            errors.append(f"opening {o['id']} is not fully on the traced room boundary")
        parsed.append({'id':o['id'],'p1':p,'p2':q})
    ax,bx,xanchors = _axis_anchors(x_anchors,axis='x',size=image.width)
    ay,by,yanchors = _axis_anchors(y_anchors,axis='y',size=image.height)
    world=lambda p:[round(ax*p[0]+bx,6),round(ay*p[1]+by,6)]
    meta={'polygon_pixels':ring,'openings':parsed,'x_anchors':xanchors,'y_anchors':yanchors,
          'world_polygon':[world(p) for p in ring],
          'world_openings':[{'id':o['id'],'p1':world(o['p1']),'p2':world(o['p2'])} for o in parsed],
          'basis':basis,'geometry_errors':errors,'geometrically_executable':not errors,
          'drawing_fidelity':'not_evaluated', 'calibration_unverified':True,
          'note':'Complete room contour is logical across apertures. Orange segments are proposed wall-hosted openings, not door leaves. Geometry pass does not validate the drawing or calibration.'}
    xs=[p[0] for p in ring];ys=[p[1] for p in ring]
    box=[max(0,int(min(xs))-35),max(0,int(min(ys))-35),min(image.width,math.ceil(max(xs))+36),min(image.height,math.ceil(max(ys))+36)]
    original=image.convert('RGB').crop(box)
    scale=min(3,1500/(2*original.width+8),1000/original.height)
    size=(max(1,round(original.width*scale)),max(1,round(original.height*scale)))
    original=original.resize(size,Image.Resampling.NEAREST)
    marked=original.copy();draw=ImageDraw.Draw(marked)
    xy=lambda p:((p[0]-box[0])*size[0]/(box[2]-box[0]),(p[1]-box[1])*size[1]/(box[3]-box[1]))
    draw.line([xy(p) for p in ring+[ring[0]]],fill=(255,60,200),width=2)
    for i,p in enumerate(ring):
        x,y=xy(p);draw.ellipse((x-3,y-3,x+3,y+3),fill='white');draw.text((x+4,y+3),f'V{i+1}',fill='yellow',stroke_width=1,stroke_fill='black')
    for o in parsed:
        p,q=xy(o['p1']),xy(o['p2']);draw.line([p,q],fill='orange',width=5)
        draw.text(((p[0]+q[0])/2+3,(p[1]+q[1])/2+3),o['id'],fill='orange',stroke_width=1,stroke_fill='black')
    combined=Image.new('RGB',(size[0]*2+8,size[1]),'white');combined.paste(original,(0,0));combined.paste(marked,(size[0]+8,0))
    meta.update(box_original_pixels=box,returned_size=list(combined.size),
                panel_note='Left: clean original crop. Right: proposed contour and apertures. V labels follow the supplied ring. All JSON coordinates refer to the original image.')
    return combined,meta
