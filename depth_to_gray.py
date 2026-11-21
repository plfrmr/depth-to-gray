#!/usr/bin/env python3
"""
Convert a "weird colored" 24-bit depth map into a normal grayscale depth map.

WARNING: VIBE CODED. This script was written by an AI and only tested on
synthetic images. Check its output yourself before relying on it.

Two kinds of colored depth maps are supported:

  packed   (default) The depth value is split across the three color bytes,
           e.g. depth = R + G*256 + B*65536. These look like noisy rainbow
           stripes / banding (CARLA, Unity, many game engines and simulators).
           The byte order is detected automatically, or set with --order.

  colormap The depth was painted with a color palette (turbo, jet, ...),
           so it looks like a smooth rainbow heat map. Use --mode colormap.

Usage examples:
  python depth_to_gray.py depth.png
  python depth_to_gray.py depth.png -o depth_gray.png
  python depth_to_gray.py some_folder/            (converts every image in it)
  python depth_to_gray.py depth.png --order rgb   (R is the most significant byte)
  python depth_to_gray.py depth.png --mode colormap --cmap jet
  python depth_to_gray.py depth.png --16bit       (16-bit PNG, keeps more precision)
  python depth_to_gray.py depth.png --clip 1      (ignore the 1% extremes, e.g. sky)

By default near = white and far = black (the usual look for depth maps used in
Blender, ControlNet, etc.). Use --invert to get near = black, far = white.

Requires: numpy, opencv-python
"""

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

IMAGE_EXTS = {".png", ".bmp", ".tif", ".tiff", ".jpg", ".jpeg", ".webp"}

CMAPS = {
    "turbo": cv2.COLORMAP_TURBO,
    "jet": cv2.COLORMAP_JET,
    "inferno": cv2.COLORMAP_INFERNO,
    "magma": cv2.COLORMAP_MAGMA,
    "plasma": cv2.COLORMAP_PLASMA,
    "viridis": cv2.COLORMAP_VIRIDIS,
    "hot": cv2.COLORMAP_HOT,
    "rainbow": cv2.COLORMAP_RAINBOW,
}


# ---------------------------------------------------------------- image I/O
# cv2.imread/imwrite fail on Windows paths with non-ASCII characters,
# so go through numpy buffers instead.

def read_rgb(path: Path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError(f"could not read image: {path}")
    if img.ndim == 2:
        raise ValueError(f"{path.name} is already single-channel (grayscale)")
    if img.dtype != np.uint8:
        raise ValueError(f"{path.name} is {img.dtype}, expected 8 bits per channel")
    img = img[:, :, :3]  # drop alpha if present
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def write_gray(path: Path, gray: np.ndarray) -> None:
    ok, buf = cv2.imencode(path.suffix or ".png", gray)
    if not ok:
        raise ValueError(f"could not encode output image: {path}")
    buf.tofile(str(path))


# ---------------------------------------------------------------- decoding

def detect_order(rgb: np.ndarray) -> str:
    """Guess which channel is the most significant byte.

    The high byte changes slowly across the image (smooth), the low byte
    wraps around constantly (noisy). So: smoothest channel = high byte.
    """
    ch = rgb.astype(np.int32)
    roughness = []
    for c in range(3):
        dx = np.abs(np.diff(ch[:, :, c], axis=1)).mean()
        dy = np.abs(np.diff(ch[:, :, c], axis=0)).mean()
        roughness.append(dx + dy)
    # R and B are the candidates for the high byte; G is always the middle one.
    return "rgb" if roughness[0] <= roughness[2] else "bgr"


def decode_packed(rgb: np.ndarray, order: str) -> np.ndarray:
    r, g, b = (rgb[:, :, i].astype(np.uint32) for i in range(3))
    if order == "rgb":  # R = high byte  ->  R*65536 + G*256 + B
        return (r << 16) | (g << 8) | b
    else:               # B = high byte  ->  B*65536 + G*256 + R  (CARLA style)
        return (b << 16) | (g << 8) | r


def decode_colormap(rgb: np.ndarray, cmap_name: str) -> np.ndarray:
    """Map each pixel to the position (0..255) of the closest palette color."""
    ramp = np.arange(256, dtype=np.uint8).reshape(-1, 1)
    lut = cv2.applyColorMap(ramp, CMAPS[cmap_name])[:, 0, ::-1].astype(np.int32)  # RGB

    flat = rgb.reshape(-1, 3)
    keys = (flat[:, 0].astype(np.uint32) << 16) | (flat[:, 1].astype(np.uint32) << 8) | flat[:, 2]
    uniq, inverse = np.unique(keys, return_inverse=True)
    colors = np.stack([(uniq >> 16) & 255, (uniq >> 8) & 255, uniq & 255], axis=1).astype(np.int32)

    idx = np.empty(len(colors), dtype=np.uint8)
    step = 65536
    for s in range(0, len(colors), step):
        chunk = colors[s:s + step]
        d = ((chunk[:, None, :] - lut[None, :, :]) ** 2).sum(axis=2)
        idx[s:s + step] = d.argmin(axis=1)

    return idx[inverse].reshape(rgb.shape[:2]).astype(np.uint32)


# ---------------------------------------------------------------- output

def to_gray(depth: np.ndarray, invert: bool, clip: float, sixteen: bool) -> np.ndarray:
    d = depth.astype(np.float64)
    if clip > 0:
        lo, hi = np.percentile(d, [clip, 100 - clip])
    else:
        lo, hi = d.min(), d.max()
    if hi <= lo:
        norm = np.zeros_like(d)
    else:
        norm = np.clip((d - lo) / (hi - lo), 0.0, 1.0)

    # Raw depth: bigger = farther. Default output: near = white.
    if not invert:
        norm = 1.0 - norm

    if sixteen:
        return np.round(norm * 65535).astype(np.uint16)
    return np.round(norm * 255).astype(np.uint8)


def convert(src: Path, dst: Path, args) -> None:
    rgb = read_rgb(src)
    if args.mode == "packed":
        order = args.order if args.order != "auto" else detect_order(rgb)
        depth = decode_packed(rgb, order)
        info = f"packed, high byte = {'R' if order == 'rgb' else 'B'}"
    else:
        depth = decode_colormap(rgb, args.cmap)
        info = f"colormap '{args.cmap}'"
    gray = to_gray(depth, args.invert, args.clip, args.sixteen)
    if args.sixteen and dst.suffix.lower() not in (".png", ".tif", ".tiff"):
        dst = dst.with_suffix(".png")
    write_gray(dst, gray)
    print(f"{src.name} -> {dst.name}  ({info})")


def main() -> int:
    p = argparse.ArgumentParser(description="Convert a 24-bit colored depth map to grayscale.")
    p.add_argument("input", type=Path, help="image file or folder of images")
    p.add_argument("-o", "--output", type=Path,
                   help="output file (or folder when input is a folder). "
                        "Default: <name>_gray.png next to the input")
    p.add_argument("--mode", choices=["packed", "colormap"], default="packed",
                   help="packed = depth split across R/G/B bytes (default); "
                        "colormap = depth painted with a palette like turbo/jet")
    p.add_argument("--order", choices=["auto", "rgb", "bgr"], default="auto",
                   help="packed mode: which channel is the most significant byte "
                        "(rgb = R high, bgr = B high / CARLA). Default: auto-detect")
    p.add_argument("--cmap", choices=sorted(CMAPS), default="turbo",
                   help="colormap mode: palette used to make the image (default: turbo)")
    p.add_argument("--invert", action="store_true",
                   help="near = black, far = white (default is near = white)")
    p.add_argument("--clip", type=float, default=0.0, metavar="PERCENT",
                   help="ignore this %% of darkest/brightest values when stretching "
                        "contrast, e.g. 1 (useful when the sky is at max depth)")
    p.add_argument("--16bit", dest="sixteen", action="store_true",
                   help="save a 16-bit PNG instead of 8-bit")
    args = p.parse_args()

    src = args.input
    if src.is_dir():
        files = sorted(f for f in src.iterdir()
                       if f.suffix.lower() in IMAGE_EXTS and not f.stem.endswith("_gray"))
        if not files:
            print(f"no images found in {src}", file=sys.stderr)
            return 1
        out_dir = args.output or src
        out_dir.mkdir(parents=True, exist_ok=True)
        jobs = [(f, out_dir / f"{f.stem}_gray.png") for f in files]
    elif src.is_file():
        jobs = [(src, args.output or src.with_name(f"{src.stem}_gray.png"))]
    else:
        print(f"not found: {src}", file=sys.stderr)
        return 1

    failed = 0
    for s, d in jobs:
        try:
            convert(s, d, args)
        except Exception as e:  # keep going in batch mode
            print(f"{s.name}: ERROR - {e}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
