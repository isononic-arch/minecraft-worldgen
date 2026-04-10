import numpy as np
from PIL import Image, ImageDraw

arr_before = np.array(Image.open(r'override_final.png'), dtype=np.uint8)
arr_after  = np.array(Image.open(r'override_smoothed.png'), dtype=np.uint8)

# Find 512x512 patch with most boundary pixels
best, bx, bz = 0, 0, 0
step = 256
for r in range(0, arr_before.shape[0]-512, step):
    for c in range(0, arr_before.shape[1]-512, step):
        patch = arr_before[r:r+512, c:c+512]
        diff_r = patch != np.roll(patch, 1, 0)
        diff_c = patch != np.roll(patch, 1, 1)
        b = (diff_r | diff_c).sum()
        if b > best:
            best, bx, bz = b, c, r

print(f'Best crop: x={bx} y={bz}  boundary_px={best}')

# Save before crop
crop_before = Image.fromarray(arr_before[bz:bz+512, bx:bx+512])
crop_before.save('validation_report/boundary_crop_before.png')

# Save after crop
crop_after = Image.fromarray(arr_after[bz:bz+512, bx:bx+512])
crop_after.save('validation_report/boundary_crop_after.png')

print('Saved boundary_crop_before.png and boundary_crop_after.png')
