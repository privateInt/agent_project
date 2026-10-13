"""Sample acceptance harness around pixel-only detectors; expected text is scoring only."""
from contextlib import closing
import hashlib
import io
from pathlib import Path
import pypdfium2 as pdfium
from raster_regions import (MAX_PIXELS,VERSION,diagram_boxes,table_groups,compose_diagram,compose_table)
from raster_quality import reference


def page_image(pdf,number):
    # Independent of pdfplumber, parser metadata, text content and text boxes.
    with closing(pdf[number-1]) as page:
        w,h=page.get_size()
        scale=min(192/72,(MAX_PIXELS/(w*h))**.5*.999,4096/max(w,h))
        with closing(page.render(scale=scale,rev_byteorder=True)) as bitmap:
            return bitmap.to_pil().convert('RGB')


def prepare(source,destination):
    ref=reference()
    if hashlib.sha256(source).hexdigest()!=ref['source_sha256']:raise ValueError('Source differs from authored acceptance sample')
    prepared=[]
    with pdfium.PdfDocument(source) as pdf:
        if len(pdf)!=9:raise ValueError('Unexpected PDF page count')
        for number in (3,4):
            image=page_image(pdf,number)
            try:
                plans=[]
                if number==3:
                    boxes=diagram_boxes(image)
                    if len(boxes)!=24:raise ValueError('Sample acceptance expects 24 automatic diagram boxes; no work submitted')
                    out,mapping=compose_diagram(image,boxes);plans.append(('figure-labels',out,mapping,{'boxes':boxes}))
                else:
                    panels=table_groups(image)
                    if any(p['columns']!=5 for p in panels):raise ValueError('Sample acceptance expects two five-column panels')
                    for side,panel in zip(('left','right'),panels):
                        ordinal=0
                        for group in panel['groups']:
                            if group['kind']=='leading-unassigned':suffix='leading'
                            else:ordinal+=1;suffix=str(ordinal)
                            out,mapping=compose_table(image,panel['header'],group)
                            plans.append(('table-'+side+'-'+suffix,out,mapping,{'panel':side,'header':panel['header'],'group':group}))
                for name,out,mapping,detection in plans:
                    try:
                        stream=io.BytesIO();out.save(stream,format='PNG');data=stream.getvalue()
                        if len(data)>8<<20:raise ValueError('Prepared PNG exceeds upload budget')
                        for m in mapping:m.update(source_page=number,source_raster_size=list(image.size),coordinate_system='rendered-page-pixels-top-left')
                        prepared.append(({'id':name,'file':name+'.png','sha256':hashlib.sha256(data).hexdigest(),'size_bytes':len(data),
                                          'width':out.width,'height':out.height,'mapping':mapping,'detector':VERSION,'detection':detection},data))
                    finally:out.close()
            finally:image.close()
    expected=['figure-labels']+[c['id'] for c in ref['cases']]
    if [i['id'] for i,b in prepared]!=expected or len(prepared)>12:raise ValueError('Detected regions differ from sample coverage; no partial submission')
    destination.mkdir(parents=True,exist_ok=True)
    if any((destination/i['file']).exists() for i,b in prepared):raise ValueError('Do not overwrite prepared inputs')
    for i,data in prepared:(destination/i['file']).write_bytes(data)
    return [i for i,data in prepared]
