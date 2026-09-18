import sys
def extract(path, name):
    txt=open(path,encoding='utf-8').read()
    i=txt.find('\t(symbol "%s"\n'%name)
    if i<0:
        i=txt.find('(symbol "%s"'%name)
    assert i>=0, name
    depth=0; j=i
    while True:
        c=txt[j]
        if c=='(':
            depth+=1
        elif c==')':
            depth-=1
            if depth==0:
                break
        elif c=='"':
            j+=1
            while txt[j]!='"':
                if txt[j]==chr(92): j+=1
                j+=1
        j+=1
    return txt[i:j+1]
if __name__=='__main__':
    lib,name=sys.argv[1],sys.argv[2]
    print(extract(lib,name))
