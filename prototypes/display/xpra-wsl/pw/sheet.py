import sys, glob, os
from PIL import Image, ImageDraw
out, w, cols = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]); files = sys.argv[4:]
ims = []
for f in files:
    im = Image.open(f).convert('RGB'); r = w / im.width; im = im.resize((w, int(im.height * r)))
    d = ImageDraw.Draw(im); d.rectangle([0, 0, 300, 14], fill=(0,0,0)); d.text((2, 1), os.path.basename(f), fill=(255,255,0)); ims.append(im)
h = max(i.height for i in ims); rows = (len(ims) + cols - 1) // cols
sheet = Image.new('RGB', (w * cols, h * rows), (40,40,40))
for k, im in enumerate(ims): sheet.paste(im, ((k % cols) * w, (k // cols) * h))
sheet.save(out); print(out, sheet.size)
