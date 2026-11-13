"""Read numeric columns of the verified BIFF8 UCI original XLS without xlrd.

Not a general Excel reader. Restricted to standard OLE2 BIFF8, sheet 'Data',
predeclared schema, stored numeric cells. Fails closed on unsupported input.
"""
from pathlib import Path
import struct
import numpy as np
import pandas as pd

EXPECTED = ['ID','LIMIT_BAL','SEX','EDUCATION','MARRIAGE','AGE'] + [f'PAY_{i}' for i in [0,2,3,4,5,6]] + [f'BILL_AMT{i}' for i in range(1,7)] + [f'PAY_AMT{i}' for i in range(1,7)] + ['default payment next month']

def _ole_stream(path):
    raw=Path(path).read_bytes()
    if raw[:8]!=bytes.fromhex('d0cf11e0a1b11ae1'): raise ValueError('Not an OLE2 .xls file')
    sector_size=1<<struct.unpack_from('<H',raw,30)[0]
    mini_sector_size=1<<struct.unpack_from('<H',raw,32)[0]
    if sector_size not in (512,4096) or mini_sector_size!=64:raise ValueError('Unsupported OLE sectors')
    nfat=struct.unpack_from('<I',raw,44)[0]
    first_dir=struct.unpack_from('<I',raw,48)[0]
    mini_cutoff=struct.unpack_from('<I',raw,56)[0]
    first_minifat=struct.unpack_from('<I',raw,60)[0]
    n_minifat=struct.unpack_from('<I',raw,64)[0]
    first_difat=struct.unpack_from('<I',raw,68)[0]
    n_difat=struct.unpack_from('<I',raw,72)[0]
    def sector(idx):
        loc=(idx+1)*sector_size
        if loc+sector_size>len(raw): raise ValueError('Truncated OLE2 sector')
        return raw[loc:loc+sector_size]
    difat=list(struct.unpack_from('<109I',raw,76))
    sec=first_difat
    for _ in range(n_difat):
        d=sector(sec)
        difat.extend(struct.unpack_from('<'+'I'*(sector_size//4-1),d,0))
        sec=struct.unpack_from('<I',d,sector_size-4)[0]
    fatsectors=[i for i in difat if i<0xFFFFFFFA][:nfat]
    if len(fatsectors)!=nfat:raise ValueError('Invalid OLE FAT')
    fat=[]
    for i in fatsectors:fat.extend(struct.unpack('<'+'I'*(sector_size//4),sector(i)))
    def chain(start,table,maxlen=100000):
        indices=[];visited=set();cur=start
        while cur<0xFFFFFFFA:
            if cur>=len(table) or cur in visited or len(indices)>=maxlen:raise ValueError('Corrupted OLE stream chain')
            visited.add(cur);indices.append(cur);cur=table[cur]
        if cur!=0xFFFFFFFE:raise ValueError('Improper OLE stream termination')
        return indices
    def read_fat(start):return b''.join(sector(i) for i in chain(start,fat))
    directory=read_fat(first_dir)
    entries=[]
    for off in range(0,len(directory),128):
        e=directory[off:off+128]
        if len(e)!=128:continue
        name_bytes=struct.unpack_from('<H',e,64)[0]
        if not 2<=name_bytes<=64:continue
        name=e[:name_bytes-2].decode('utf-16le')
        typ=e[66];start=struct.unpack_from('<I',e,116)[0]
        length=struct.unpack_from('<Q',e,120)[0]
        entries.append((name,typ,start,length))
    root=next((e for e in entries if e[1]==5),None)
    wb=next((e for e in entries if e[1]==2 and e[0] in ('Workbook','Book')),None)
    if root is None or wb is None:raise ValueError('Workbook/root missing')
    if wb[3]>=mini_cutoff:return read_fat(wb[2])[:wb[3]]
    ministream=read_fat(root[2])[:root[3]]
    minifat=[]
    if n_minifat:
        mf=b''.join(sector(i) for i in chain(first_minifat,fat))[:n_minifat*sector_size]
        minifat=list(struct.unpack('<'+'I'*(len(mf)//4),mf))
    return b''.join(ministream[i*mini_sector_size:(i+1)*mini_sector_size] for i in chain(wb[2],minifat))[:wb[3]]

def _rk(v):
    if v&2:val=struct.unpack('<i',struct.pack('<I',v))[0]>>2
    else:val=struct.unpack('<d',struct.pack('<Q',(v&0xFFFFFFFC)<<32))[0]
    if v&1:val/=100.
    return float(val)

def load_verified_uci(path):
    data=_ole_stream(path)
    def records(begin=0):
        at=begin
        while at+4<=len(data):
            rectype,size=struct.unpack_from('<HH',data,at)
            nxt=at+4+size
            if nxt>len(data):raise ValueError('Truncated BIFF record')
            yield at,rectype,data[at+4:nxt]
            at=nxt
    sheets=[]
    for _,code,body in records():
        if code==0x0085:
            ofs=struct.unpack_from('<I',body,0)[0];ln=body[6];high=body[7]&1
            name=body[8:8+ln*(2 if high else 1)].decode('utf-16le' if high else 'latin1')
            sheets.append((name,ofs))
        if code==0x000A:break
    matches=[off for name,off in sheets if name=='Data']
    if len(matches)!=1:raise ValueError('Exactly one Data sheet is required')
    offset=matches[0]
    if struct.unpack_from('<H',data,offset)[0]!=0x0809:raise ValueError('Invalid BIFF sheet offset')
    # Official source has a title row (index 0), header row (index 1), and 30k numeric samples beginning at index 2.
    x=np.full((30000,25),np.nan,dtype=np.float64)
    count=0
    for _,code,body in records(offset):
        if code==0x000A:break
        if code==0x0203 and len(body)>=14:
            r,c=struct.unpack_from('<HH',body);value=struct.unpack_from('<d',body,6)[0]
            if 2<=r<30002 and c<25:x[r-2,c]=value;count+=1
        elif code==0x027E and len(body)>=10:
            r,c=struct.unpack_from('<HH',body)
            if 2<=r<30002 and c<25:x[r-2,c]=_rk(struct.unpack_from('<I',body,6)[0]);count+=1
        elif code==0x00BD and len(body)>=10:
            r,c=struct.unpack_from('<HH',body);last=struct.unpack_from('<H',body,len(body)-2)[0]
            for j in range(last-c+1):
                if 2<=r<30002 and c+j<25:
                    x[r-2,c+j]=_rk(struct.unpack_from('<I',body,6+6*j)[0]);count+=1
        elif code==0x0006 and len(body)>=20:
            r,c=struct.unpack_from('<HH',body)
            if 2<=r<30002 and c<25:x[r-2,c]=struct.unpack_from('<d',body,6)[0];count+=1
    if not np.isfinite(x).all():
        missing=int(np.isnan(x).sum());raise ValueError(f'Unexpected missing raw UCI numeric cells: {missing}, stored {count}')
    df=pd.DataFrame(x,columns=EXPECTED)
    if df.ID.nunique()!=30000 or int(df.ID.min())!=1 or int(df.ID.max())!=30000 or int(df.iloc[:,-1].sum())!=6636:
        raise ValueError('UCI BIFF8 cell mapping failed known reference checks')
    return df
