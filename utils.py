import os
import sys
import threading
import logging
import boto3
from botocore.exceptions import ClientError
import os
import rasterio
from rasterio.transform import from_origin
import numpy as np
import cv2

logger = logging.getLogger(__name__)

def find_files(folder, contains):
    paths = []
    for root, dirs, files in os.walk(folder):
        for name in files:
            if contains in name:
                filename = os.path.join(root,name)
                paths.append(filename)
    return paths

class ProgressPercentage(object):

    def __init__(self, filename):
        self._filename = filename
        self._size = float(os.path.getsize(filename))
        self._seen_so_far = 0
        self._lock = threading.Lock()

    def __call__(self, bytes_amount):
        # To simplify, assume this is hooked up to a single filename
        with self._lock:
            self._seen_so_far += bytes_amount
            percentage = (self._seen_so_far / self._size) * 100
            sys.stdout.write(
                "\r%s  %s / %s  (%.2f%%)" % (
                    self._filename, self._seen_so_far, self._size,
                    percentage))
            sys.stdout.flush()


def upload_file(file_name, bucket, object_name=None):
    """Upload a file to an S3 bucket

    :param file_name: File to upload
    :param bucket: Bucket to upload to
    :param object_name: S3 object name. If not specified then file_name is used
    :return: True if file was uploaded, else False
    """

    # If S3 object_name was not specified, use file_name
    if object_name is None:
        object_name = os.path.basename(file_name)

    # Upload the file
    s3_client = boto3.client('s3')
    try:
        response = s3_client.upload_file(file_name, bucket, object_name, Callback=ProgressPercentage(file_name))
    except ClientError as e:
        logging.error(e)
        return False

def expand_raster_with_bounds(input_raster, output_raster, old_bounds, new_bounds):

    # Open the raster dataset
    with rasterio.open(input_raster, 'r') as src:
        # get old bounds
        old_left, old_bottom, old_right, old_top = old_bounds
        # Define the new bounds
        new_left, new_bottom, new_right, new_top = new_bounds
        # adjust the new bounds with even pixel multiples of existing
        # this will stop small offsets
        logging.info(f'Making new raster with target bounds: {new_bounds}')
        new_left = old_left - int(abs(new_left-old_left)/src.res[0])*src.res[0]
        new_right = old_right + int(abs(new_right-old_right)/src.res[0])*src.res[0]
        new_bottom = old_bottom - int(abs(new_bottom-old_bottom)/src.res[1])*src.res[1]
        new_top = old_top + int(abs(new_top-old_top)/src.res[1])*src.res[1]
        logging.info(f'New raster bounds: {(new_left, new_bottom, new_right, new_top)}')
        # Calculate the new width and height, should be integer values
        new_width = int((new_right - new_left) / src.res[0])
        new_height = int((new_top - new_bottom) / src.res[1])
        # Define the new transformation matrix
        transform = from_origin(new_left, new_top, src.res[0], src.res[1])
        # Create a new raster dataset with expanded bounds
        profile = src.profile
        profile.update({
            'width': new_width,
            'height': new_height,
            'transform': transform
        })
        # make a temp file
        tmp = output_raster.replace('.tif','_tmp.tif')
        logging.debug(f'Making temp file: {tmp}')
        with rasterio.open(tmp, 'w', **profile) as dst:
            # Read the data from the source and write it to the destination
            data = np.full((new_height, new_width), fill_value=profile['nodata'], dtype=profile['dtype'])
            dst.write(data, 1)
        # merge the old raster into the new raster with expanded bounds 
        logging.info(f'Merging original raster and expanding bounds...')
        rasterio.merge.merge(
            datasets=[tmp, input_raster],
            method='max',
            dst_path=output_raster)
        os.remove(tmp)

def normalise_bands(image, n_bands, p_min=5, p_max=95):
    norm = []
    for c in range(0,n_bands):
        band = image[c,:, :].copy()
        stat_band = band[(np.isfinite(band))].copy()
        plow, phigh = np.percentile(stat_band, (p_min,p_max))
        band = (band - plow) / (phigh - plow)
        band[band<0] = 0
        band[band>1] = 1
        norm.append(band)
    return np.array(norm) # c,h,w in blue, green, red    

def save_tif_as_image(tif_path, img_path, downscale_factor=5):
    with rasterio.open(tif_path) as src:
        X = src.read()
        img = normalise_bands(X,1)
        img = (255*img).astype('uint8')[0]
        # resize based on the downscale factor
        h,w = img.shape
        new_h, new_w = int(h/downscale_factor), int(w/downscale_factor)
        res = cv2.resize(img, 
                         dsize=(new_h, new_w), 
                         interpolation=cv2.INTER_CUBIC)
        cv2.imwrite(img_path, res)
