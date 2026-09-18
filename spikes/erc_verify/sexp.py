# minimal s-expression parser/serializer for kicad files
import re
class Sym(str): pass
def parse(text):
    i=0; n=len(text)
    def ws():
        nonlocal i
        while i<n and text[i] in ' \t\r\n': i+=1
    def node():
        nonlocal i
        ws()
        if text[i]=='(':
            i+=1; out=[]
            while True:
                ws()
                if text[i]==')': i+=1; return out
                out.append(node())
        elif text[i]=='"':
            i+=1; s=''
            while True:
                c=text[i]
                if c==chr(92): s+=text[i:i+2]; i+=2; continue
                if c=='"': i+=1; return s   # keep escapes raw
                s+=c; i+=1
        else:
            j=i
            while i<n and text[i] not in ' \t\r\n()': i+=1
            return Sym(text[j:i])
    return node()
def ser(node, ind=0):
    if isinstance(node, list):
        head=[]; tail=[]; k=0
        # inline leading atoms, nested lists on own lines (kicad style)
        while k<len(node) and not isinstance(node[k], list): head.append(node[k]); k+=1
        s='\t'*ind+'('+' '.join(atom(a) for a in head)
        rest=node[k:]
        if not rest: return s+')'
        if all(not isinstance(r,list) for r in rest):
            return s+' '+' '.join(atom(a) for a in rest)+')'
        # special: pts lists put xy inline
        if head and head[0]=='pts':
            return s+'\n'+'\t'*(ind+1)+' '.join(ser(r,0) for r in rest)+'\n'+'\t'*ind+')'
        for r in rest:
            s+='\n'+(ser(r,ind+1) if isinstance(r,list) else '\t'*(ind+1)+atom(r))
        return s+'\n'+'\t'*ind+')'
    return '\t'*ind+atom(node)
def atom(a):
    if isinstance(a,Sym): return a
    return '"'+a+'"'
def find(node, key):
    return [c for c in node if isinstance(c,list) and c and c[0]==key]
def find1(node,key):
    f=find(node,key); return f[0] if f else None
