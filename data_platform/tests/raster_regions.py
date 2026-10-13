"""Bounded pixel-only candidates for clean, axis-aligned diagrams/ruled tables.

No PDF text extraction, parser blocks, OCR strings, answers or sample coordinates.
These are experimental geometric candidates, not semantic/arrow interpretation.
"""
from PIL import Image

VERSION = 'raster-lines-v1'
MAX_PIXELS = 6_000_000
MAX_REGIONS = 64


def pixels(image):
    w,h=image.size
    if min(w,h)<1 or w*h>MAX_PIXELS:raise ValueError('Raster size outside detector budget')
    return image.convert('L').point(lambda x: int(x<160),'L').tobytes()


def runs(values, gap=0):
    out=[];start=None
    for i,on in enumerate(values):
        if on and start is None:start=i
        if not on and start is not None:out.append([start,i]);start=None
    if start is not None:out.append([start,len(values)])
    merged=[]
    for a,b in out:
        if merged and a-merged[-1][1]<=gap:merged[-1][1]=b
        else:merged.append([a,b])
    return merged


def horizontal_lines(image, min_width, data=None):
    w,h=image.size;data=pixels(image) if data is None else data;out=[]
    for y in range(h):
        for x0,x1 in runs(data[y*w:(y+1)*w]):
            if x1-x0<min_width:continue
            match=next((s for s in reversed(out[-50:]) if y-s[3]<=3 and abs(s[0]-x0)<=3 and abs(s[2]-x1)<=3),None)
            if match:match[3]=y+1
            else:out.append([x0,y,x1,y+1])
            if len(out)>2000:raise ValueError('Too many raster lines')
    return out


def diagram_boxes(image):
    w,h=image.size;data=pixels(image);lines=horizontal_lines(image,max(30,w//25),data);boxes=[]
    for a in lines:
        if a[2]-a[0]>w*.6:continue
        for b in lines:
            dy=b[1]-a[3]
            if not 12<=dy<=w*.15 or abs(a[0]-b[0])>4 or abs(a[2]-b[2])>4:continue
            rows=range(a[3],b[1])
            if not all(sum(any(data[y*w+x] for x in range(max(0,x0-2),min(w,x0+3))) for y in rows)/len(rows)>.8 for x0 in (a[0],a[2]-1)):continue
            box=[a[0],a[1],a[2],b[3]]
            if any(max(abs(x-y) for x,y in zip(box,old))<5 for old in boxes):continue
            boxes.append(box)
            if len(boxes)>MAX_REGIONS:raise ValueError('Diagram candidate budget exceeded')
    # Stable geometric rows, explicitly not arrow/semantic order.
    grouped=[]
    for box in sorted(boxes,key=lambda b:b[1]):
        row=next((r for r in grouped if abs(box[1]-r[0][1])<=min(box[3]-box[1],r[0][3]-r[0][1])*.3),None)
        if row is None:grouped.append([box])
        else:row.append(box)
    return [b for row in grouped for b in sorted(row,key=lambda b:b[0])]


def table_groups(image):
    """Repeated header geometry -> panels -> first-column ink row anchors.

    Never OCR or guess anchor numbers. Leading unnumbered content is returned
    separately. Unsupported/ambiguous layout is refused, not silently clipped.
    """
    w,h=image.size;data=pixels(image);ls=horizontal_lines(image,w*.5,data);candidates=[]
    for a,b in zip(ls,ls[1:]):
        if not 12<=b[1]-a[3]<=h*.04 or abs(a[0]-b[0])>4 or abs(a[2]-b[2])>4:continue
        bottoms=[z for z in ls if z[1]>b[3]+h*.1 and abs(z[0]-b[0])<5 and abs(z[2]-b[2])<5]
        if bottoms:candidates.append((a,b,bottoms[0]))
    if len(candidates)!=1:raise ValueError('Need one unambiguous ruled table header/body')
    top,sep,bottom=candidates[0];x0,x1=top[0],top[2];y0,y1=top[3]+1,sep[1]-1
    gap=max(2,(y1-y0)//3)
    words=[[a+x0,b+x0] for a,b in runs([any(data[y*w+x] for y in range(y0,y1)) for x in range(x0,x1)],gap)]
    if len(words)<6 or len(words)%2:raise ValueError('Repeated header geometry not found')
    period=len(words)//2
    if not 3<=period<=8 or any(abs((a[1]-a[0])-(b[1]-b[0]))>max(4,(a[1]-a[0])*.15) for a,b in zip(words[:period],words[period:])):
        raise ValueError('Ambiguous table panels')
    split=(words[period-1][1]+words[period][0])//2
    panels=[]
    for index,(left,right) in enumerate(((x0,split),(split,x1))):
        first,second=words[index*period:index*period+2];lane_end=(first[1]+second[0])//2
        start,end=sep[3]+1,bottom[1]
        anchors=[[a+start,b+start] for a,b in runs([sum(data[y*w+x] for x in range(left,lane_end))>=2 for y in range(start,end)],3)]
        if not 1<=len(anchors)<=12:raise ValueError('Unbounded/empty table anchor lane')
        if any(b-a>max(40,(y1-y0)*2) for a,b in anchors):raise ValueError('Non-text anchor lane')
        cuts=[max(start,a-4) for a,b in anchors]
        groups=[]
        if sum(data[y*w+x] for y in range(start,cuts[0]) for x in range(left,right))>10:
            groups.append({'kind':'leading-unassigned','box':[left,start,right,cuts[0]]})
        for i,cut in enumerate(cuts):
            groups.append({'kind':'anchor-group','box':[left,cut,right,cuts[i+1] if i+1<len(cuts) else end], 'anchor_bbox':[left,anchors[i][0],lane_end,anchors[i][1]]})
        panels.append({'header':[left,top[1],right,sep[3]],'body':[left,start,right,end], 'columns':period,'groups':groups})
    return panels


def compose_diagram(image, boxes):
    if not 1<=len(boxes)<=MAX_REGIONS:raise ValueError('No bounded diagram candidates')
    out=Image.new('RGB',(1440,((len(boxes)+1)//2)*140+20),'white');mapping=[]
    if out.width*out.height>MAX_PIXELS:raise ValueError('Diagram montage exceeds image budget')
    for i,(x0,y0,x1,y1) in enumerate(boxes):
        inset=max(3,int((y1-y0)*.15));box=[x0+inset,y0+inset,x1-inset,y1-inset]
        part=image.crop(box);part=part.resize((max(1,round(part.width*100/part.height)),100),Image.Resampling.LANCZOS)
        part.thumbnail((700,100),Image.Resampling.LANCZOS);x,y=20+(i%2)*710,20+(i//2)*140
        out.paste(part,(x,y));mapping.append({'source_bbox':box,'image_bbox':[x,y,x+part.width,y+part.height],'role':'diagram-interior'});part.close()
    return out,mapping


def compose_table(image, header, group):
    """Repeat original header pixels; body remains contiguous and is not repaired."""
    boxes=[header,group['box']];width=header[2]-header[0]
    out=Image.new('RGB',(width+16,16+(header[3]-header[1])+12+(boxes[1][3]-boxes[1][1])),'white');mapping=[];y=8
    for role,box in zip(('header-context','body'),boxes):
        part=image.crop(box);out.paste(part,(8,y));mapping.append({'source_bbox':box,'image_bbox':[8,y,8+part.width,y+part.height],'role':role});y+=part.height+12;part.close()
    original_size=out.size
    scale=min(2,1600/max(out.size));target=(round(out.width*scale),round(out.height*scale));scaled=out.resize(target,Image.Resampling.LANCZOS);out.close()
    for m in mapping:m['image_bbox']=[v*(target[0]/original_size[0] if j%2==0 else target[1]/original_size[1]) for j,v in enumerate(m['image_bbox'])]
    return scaled,mapping
