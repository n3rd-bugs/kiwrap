//! Image codecs: JPEG (libjpeg-turbo through the `turbojpeg` crate, statically built), PNG
//! (`png` crate) for screenshots, and an area-averaging downscale for the `max_px` rule
//! (ARCHITECTURE 9: agent images <= 1024 px).

use std::io::Write;

/// A BGRA8 image, tightly packed (pitch == width * 4).
#[derive(Clone, Debug, Default)]
pub struct Bgra {
    pub w: u32,
    pub h: u32,
    pub data: Vec<u8>,
}

impl Bgra {
    pub fn new(w: u32, h: u32) -> Self {
        Bgra { w, h, data: vec![0u8; (w as usize) * (h as usize) * 4] }
    }
    pub fn is_empty(&self) -> bool {
        self.w == 0 || self.h == 0
    }
    #[allow(dead_code)]
    pub fn row(&self, y: u32) -> &[u8] {
        let p = self.w as usize * 4;
        &self.data[y as usize * p..(y as usize + 1) * p]
    }
}

pub struct JpegEncoder {
    comp: turbojpeg::Compressor,
}

impl JpegEncoder {
    pub fn new(quality: u32) -> Result<Self, String> {
        let mut comp = turbojpeg::Compressor::new().map_err(|e| e.to_string())?;
        comp.set_quality(quality.clamp(1, 100) as i32).map_err(|e| e.to_string())?;
        // 4:2:0 like the prototype (simplejpeg default); text stays readable at q >= 60.
        comp.set_subsamp(turbojpeg::Subsamp::Sub2x2).map_err(|e| e.to_string())?;
        Ok(JpegEncoder { comp })
    }

    pub fn set_quality(&mut self, quality: u32) {
        let _ = self.comp.set_quality(quality.clamp(1, 100) as i32);
    }

    pub fn encode(&mut self, img: &Bgra) -> Result<Vec<u8>, String> {
        let image = turbojpeg::Image {
            pixels: img.data.as_slice(),
            width: img.w as usize,
            pitch: img.w as usize * 4,
            height: img.h as usize,
            format: turbojpeg::PixelFormat::BGRA,
        };
        self.comp.compress_to_vec(image).map_err(|e| e.to_string())
    }

    /// Encode a sub-rectangle (x, y, w, h) of `img`.
    #[allow(dead_code)] // tile encode path, kept for a future dirty-rect-only transport
    pub fn encode_rect(&mut self, img: &Bgra, x: u32, y: u32, w: u32, h: u32) -> Result<Vec<u8>, String> {
        let pitch = img.w as usize * 4;
        let start = y as usize * pitch + x as usize * 4;
        let end = (y + h - 1) as usize * pitch + (x + w) as usize * 4;
        let image = turbojpeg::Image {
            pixels: &img.data[start..end],
            width: w as usize,
            pitch,
            height: h as usize,
            format: turbojpeg::PixelFormat::BGRA,
        };
        self.comp.compress_to_vec(image).map_err(|e| e.to_string())
    }
}

/// PNG (RGB, no alpha) of a BGRA image.
pub fn encode_png(img: &Bgra) -> Result<Vec<u8>, String> {
    let mut rgb = Vec::with_capacity(img.w as usize * img.h as usize * 3);
    for px in img.data.chunks_exact(4) {
        rgb.extend_from_slice(&[px[2], px[1], px[0]]);
    }
    let mut out = Vec::new();
    {
        let mut enc = png::Encoder::new(&mut out, img.w, img.h);
        enc.set_color(png::ColorType::Rgb);
        enc.set_depth(png::BitDepth::Eight);
        enc.set_compression(png::Compression::Fast);
        let mut w = enc.write_header().map_err(|e| e.to_string())?;
        w.write_image_data(&rgb).map_err(|e| e.to_string())?;
        w.finish().map_err(|e| e.to_string())?;
    }
    out.flush().ok();
    Ok(out)
}

/// Downscale so that max(w, h) <= max_px (area averaging over integer boxes, then a bilinear-free
/// nearest step for the remainder; good enough for a preview that is downscaled further by the
/// client). Returns the input unchanged when it already fits.
pub fn downscale_to(img: &Bgra, max_px: u32) -> Bgra {
    if max_px == 0 || (img.w <= max_px && img.h <= max_px) || img.is_empty() {
        return img.clone();
    }
    let scale = (img.w.max(img.h) as f64) / (max_px as f64);
    let ow = ((img.w as f64 / scale).floor() as u32).max(1);
    let oh = ((img.h as f64 / scale).floor() as u32).max(1);
    let mut out = Bgra::new(ow, oh);
    let sw = img.w as usize;
    for oy in 0..oh as usize {
        let y0 = (oy as f64 * scale) as usize;
        let y1 = (((oy + 1) as f64 * scale) as usize).clamp(y0 + 1, img.h as usize);
        for ox in 0..ow as usize {
            let x0 = (ox as f64 * scale) as usize;
            let x1 = (((ox + 1) as f64 * scale) as usize).clamp(x0 + 1, img.w as usize);
            let mut acc = [0u32; 4];
            let mut n = 0u32;
            for y in y0..y1 {
                let row = &img.data[y * sw * 4..(y + 1) * sw * 4];
                for x in x0..x1 {
                    let p = &row[x * 4..x * 4 + 4];
                    acc[0] += p[0] as u32;
                    acc[1] += p[1] as u32;
                    acc[2] += p[2] as u32;
                    acc[3] += p[3] as u32;
                    n += 1;
                }
            }
            let o = (oy * ow as usize + ox) * 4;
            out.data[o] = (acc[0] / n) as u8;
            out.data[o + 1] = (acc[1] / n) as u8;
            out.data[o + 2] = (acc[2] / n) as u8;
            out.data[o + 3] = 255;
        }
    }
    out
}

/// Copy `src` into `dst` at (dx, dy), clipping to both images.
pub fn blit(dst: &mut Bgra, src: &Bgra, dx: i32, dy: i32) {
    if src.is_empty() || dst.is_empty() {
        return;
    }
    let x0 = dx.max(0);
    let y0 = dy.max(0);
    let x1 = (dx + src.w as i32).min(dst.w as i32);
    let y1 = (dy + src.h as i32).min(dst.h as i32);
    if x1 <= x0 || y1 <= y0 {
        return;
    }
    let cols = (x1 - x0) as usize * 4;
    let dp = dst.w as usize * 4;
    let sp = src.w as usize * 4;
    for y in y0..y1 {
        let sy = (y - dy) as usize;
        let sx = (x0 - dx) as usize;
        let d = y as usize * dp + x0 as usize * 4;
        let s = sy * sp + sx * 4;
        dst.data[d..d + cols].copy_from_slice(&src.data[s..s + cols]);
    }
}

/// Copy the sub-rect (sx, sy, w, h) of `src` into `dst` at (dx, dy) with clipping.
pub fn blit_rect(dst: &mut Bgra, src: &Bgra, sx: i32, sy: i32, w: i32, h: i32, dx: i32, dy: i32) {
    if src.is_empty() || dst.is_empty() || w <= 0 || h <= 0 {
        return;
    }
    // clip the source rect to the source image
    let sx0 = sx.max(0);
    let sy0 = sy.max(0);
    let sx1 = (sx + w).min(src.w as i32);
    let sy1 = (sy + h).min(src.h as i32);
    if sx1 <= sx0 || sy1 <= sy0 {
        return;
    }
    let dx0 = dx + (sx0 - sx);
    let dy0 = dy + (sy0 - sy);
    // clip to the destination
    let cx0 = dx0.max(0);
    let cy0 = dy0.max(0);
    let cx1 = (dx0 + (sx1 - sx0)).min(dst.w as i32);
    let cy1 = (dy0 + (sy1 - sy0)).min(dst.h as i32);
    if cx1 <= cx0 || cy1 <= cy0 {
        return;
    }
    let cols = (cx1 - cx0) as usize * 4;
    let dp = dst.w as usize * 4;
    let sp = src.w as usize * 4;
    for y in cy0..cy1 {
        let syy = (sy0 + (y - dy0)) as usize;
        let sxx = (sx0 + (cx0 - dx0)) as usize;
        let d = y as usize * dp + cx0 as usize * 4;
        let s = syy * sp + sxx * 4;
        dst.data[d..d + cols].copy_from_slice(&src.data[s..s + cols]);
    }
}

/// Dirty tiles between two same-sized images: returns the list of changed tile rects
/// (x, y, w, h) merged per row run, or None when the sizes differ (everything is dirty).
pub fn dirty_rects(prev: &Bgra, next: &Bgra, tile: u32) -> Option<Vec<[i32; 4]>> {
    if prev.w != next.w || prev.h != next.h || prev.data.len() != next.data.len() {
        return None;
    }
    let tile = tile.max(8) as usize;
    let w = next.w as usize;
    let h = next.h as usize;
    let pitch = w * 4;
    let tiles_x = w.div_ceil(tile);
    let tiles_y = h.div_ceil(tile);
    let mut out = Vec::new();
    for ty in 0..tiles_y {
        let y0 = ty * tile;
        let y1 = (y0 + tile).min(h);
        let mut run: Option<usize> = None;
        for tx in 0..tiles_x {
            let x0 = tx * tile * 4;
            let x1 = ((tx + 1) * tile * 4).min(pitch);
            let mut changed = false;
            for y in y0..y1 {
                let a = &prev.data[y * pitch + x0..y * pitch + x1];
                let b = &next.data[y * pitch + x0..y * pitch + x1];
                if a != b {
                    changed = true;
                    break;
                }
            }
            match (changed, run) {
                (true, None) => run = Some(tx),
                (false, Some(start)) => {
                    out.push([(start * tile) as i32, y0 as i32, ((tx - start) * tile).min(w - start * tile) as i32, (y1 - y0) as i32]);
                    run = None;
                }
                _ => {}
            }
        }
        if let Some(start) = run {
            out.push([(start * tile) as i32, y0 as i32, (w - start * tile) as i32, (y1 - y0) as i32]);
        }
    }
    Some(out)
}

#[allow(dead_code)]
pub fn union_of(rects: &[[i32; 4]]) -> Option<[i32; 4]> {
    let mut it = rects.iter();
    let first = *it.next()?;
    let (mut l, mut t, mut r, mut b) = (first[0], first[1], first[0] + first[2], first[1] + first[3]);
    for x in it {
        l = l.min(x[0]);
        t = t.min(x[1]);
        r = r.max(x[0] + x[2]);
        b = b.max(x[1] + x[3]);
    }
    Some([l, t, r - l, b - t])
}

#[cfg(test)]
mod tests {
    use super::*;

    fn filled(w: u32, h: u32, v: u8) -> Bgra {
        let mut i = Bgra::new(w, h);
        i.data.fill(v);
        i
    }

    #[test]
    fn jpeg_roundtrip_size() {
        let img = filled(64, 48, 128);
        let mut enc = JpegEncoder::new(70).unwrap();
        let j = enc.encode(&img).unwrap();
        assert_eq!(&j[0..2], &[0xFF, 0xD8]);
        let sub = enc.encode_rect(&img, 8, 8, 16, 16).unwrap();
        assert_eq!(&sub[0..2], &[0xFF, 0xD8]);
    }

    #[test]
    fn png_has_signature_and_dims() {
        let img = filled(10, 5, 200);
        let p = encode_png(&img).unwrap();
        assert_eq!(&p[0..8], &[0x89, b'P', b'N', b'G', 0x0D, 0x0A, 0x1A, 0x0A]);
        assert_eq!(u32::from_be_bytes([p[16], p[17], p[18], p[19]]), 10);
        assert_eq!(u32::from_be_bytes([p[20], p[21], p[22], p[23]]), 5);
    }

    #[test]
    fn downscale_limits_the_long_edge() {
        let img = filled(1920, 1080, 77);
        let d = downscale_to(&img, 1024);
        assert_eq!(d.w, 1024);
        assert_eq!(d.h, 576);
        assert_eq!(d.data[0], 77);
        assert_eq!(downscale_to(&img, 4000).w, 1920);
    }

    #[test]
    fn blit_clips() {
        let mut dst = Bgra::new(10, 10);
        let src = filled(4, 4, 9);
        blit(&mut dst, &src, 8, 8);
        assert_eq!(dst.data[(9 * 10 + 9) * 4], 9);
        assert_eq!(dst.data[(7 * 10 + 7) * 4], 0);
        blit(&mut dst, &src, -2, -2);
        assert_eq!(dst.data[(1 * 10 + 1) * 4], 9);
        assert_eq!(dst.data[(2 * 10 + 2) * 4], 0);
    }

    #[test]
    fn blit_rect_copies_a_subregion() {
        let mut dst = Bgra::new(6, 6);
        let mut src = Bgra::new(8, 8);
        for y in 0..8 {
            for x in 0..8 {
                src.data[(y * 8 + x) * 4] = (y * 8 + x) as u8;
            }
        }
        blit_rect(&mut dst, &src, 2, 3, 2, 2, 1, 1);
        assert_eq!(dst.data[(1 * 6 + 1) * 4], (3 * 8 + 2) as u8);
        assert_eq!(dst.data[(2 * 6 + 2) * 4], (4 * 8 + 3) as u8);
        assert_eq!(dst.data[(3 * 6 + 3) * 4], 0);
    }

    #[test]
    fn dirty_rects_find_changed_tiles() {
        let a = filled(100, 70, 1);
        let mut b = a.clone();
        assert_eq!(dirty_rects(&a, &b, 32).unwrap(), Vec::<[i32; 4]>::new());
        b.data[(40 * 100 + 70) * 4] = 2; // tile (2,1)
        let d = dirty_rects(&a, &b, 32).unwrap();
        assert_eq!(d, vec![[64, 32, 32, 32]]);
        assert_eq!(union_of(&d), Some([64, 32, 32, 32]));
        b.data[(40 * 100 + 98) * 4] = 2; // last, narrow tile (3,1)
        assert_eq!(dirty_rects(&a, &b, 32).unwrap(), vec![[64, 32, 36, 32]]);
        assert!(dirty_rects(&a, &filled(10, 10, 1), 32).is_none());
    }
}
