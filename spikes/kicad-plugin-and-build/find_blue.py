import sys, struct, zlib
p=sys.argv[1]; d=open(p,'rb').read(); pos=8; idat=b''
while pos<len(d):
    n=struct.unpack('>I',d[pos:pos+4])[0]; t=d[pos+4:pos+8]; c=d[pos+8:pos+8+n]; pos+=12+n
    if t==b'IHDR': w,h,bd,ct=struct.unpack('>IIBB',c[:10])
    elif t==b'IDAT': idat+=c
raw=zlib.decompress(idat); bpp={2:3,6:4}[ct]; stride=w*bpp; i=0; prev=bytearray(stride); xs=[]; ys=[]
for y in range(min(h,200)):
    f=raw[i]; i+=1; line=bytearray(raw[i:i+stride]); i+=stride
    for x in range(stride):
        a=line[x-bpp] if x>=bpp else 0; b=prev[x]; c=prev[x-bpp] if x>=bpp else 0
        if f==1: line[x]=(line[x]+a)&255
        elif f==2: line[x]=(line[x]+b)&255
        elif f==3: line[x]=(line[x]+((a+b)>>1))&255
        elif f==4:
            pa=abs(b-c); pb=abs(a-c); pc=abs(a+b-2*c); pr=a if pa<=pb and pa<=pc else (b if pb<=pc else c); line[x]=(line[x]+pr)&255
    prev=line
    for x in range(w):
        r,g,b=line[x*bpp],line[x*bpp+1],line[x*bpp+2]
        if abs(r-30)<=6 and abs(g-120)<=6 and abs(b-200)<=6: xs.append(x); ys.append(y)
if xs: print(f"{(min(xs)+max(xs))//2} {(min(ys)+max(ys))//2} {len(xs)}")
else: print("none")
