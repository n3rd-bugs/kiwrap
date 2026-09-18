p="bisect2.py"; s=open(p).read()
if "edge added" not in s:
    s=s.replace('FP="C:/Program Files', '''if "edge" in flags:
    for a,bb in [((0,0),(30,0)),((30,0),(30,20)),((30,20),(0,20)),((0,20),(0,0))]:
        seg = pcbnew.PCB_SHAPE(b); seg.SetShape(pcbnew.SHAPE_T_SEGMENT); seg.SetStart(V(*a)); seg.SetEnd(V(*bb)); seg.SetLayer(pcbnew.Edge_Cuts); seg.SetWidth(MM(0.1)); b.Add(seg)
    print("edge added", flush=True)
if "edgerect" in flags:
    r = pcbnew.PCB_SHAPE(b); r.SetShape(pcbnew.SHAPE_T_RECT); r.SetStart(V(0,0)); r.SetEnd(V(30,20)); r.SetLayer(pcbnew.Edge_Cuts); r.SetWidth(MM(0.1)); b.Add(r)
    print("edgerect added", flush=True)
FP="C:/Program Files''')
    open(p,"w").write(s)
print("patched")
