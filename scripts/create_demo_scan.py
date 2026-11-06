"""Create a small, entirely synthetic tiled TIFF pyramid for the public demo."""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import tifffile


def create_demo(path: Path) -> None:
    y, x = np.mgrid[:2048, :2048]
    rings = (np.sin(np.hypot(x - 1024, y - 1024) / 25) + 1) / 2
    pixels = np.stack((40 + 180 * x / 2047, 40 + 180 * y / 2047,
                       50 + 170 * rings), axis=-1).astype(np.uint8)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents overwriting a user's existing image.
    with path.open('xb') as output, tifffile.TiffWriter(output) as writer:
        writer.write(pixels, photometric='rgb', tile=(256, 256),
                     compression='deflate', subifds=3, metadata=None,
                     description='Synthetic demo: mathematical RGB patterns; no acquired data.')
        for factor in (2, 4, 8):
            writer.write(pixels[::factor, ::factor], photometric='rgb',
                         tile=(256, 256), compression='deflate',
                         subfiletype=1, metadata=None)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=(
        Path(__file__).resolve().parents[1] / 'scans' / 'synthetic-demo.tif'))
    args = parser.parse_args()
    try:
        create_demo(args.output)
    except FileExistsError:
        parser.exit(1, 'Output already exists; choose another --output path.\n')
    print(f'Created synthetic demo: {args.output.name}')


if __name__ == '__main__':
    main()
