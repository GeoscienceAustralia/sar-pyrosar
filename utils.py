import os
import sys
import threading
import logging
import boto3
from botocore.exceptions import ClientError
import os
import rasterio
from rasterio.transform import from_origin
from rasterio.enums import Resampling
import numpy as np
import cv2
import pyproj
from shapely.geometry import Polygon

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

def normalise_bands(image: np.array, n_bands: int, p_min: int = 5, p_max: int = 95):
    """Normalise the bands between the specified percentiles

    Args:
        image (np.array): image to be normalised
        n_bands (int): The number of bands
        p_min (int, optional): Min percentile. Defaults to 5.
        p_max (int, optional): Max percentile. Defaults to 95.

    Returns:
        np.array: normalised array
    """
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

def save_tif_as_image(tif_path: str, img_path: str, downscale_factor: int =5):
    """ save a specified tif as an image

    Args:
        tif_path (str): path to the tif
        img_path (str): save path of the image. e.g. img.jpeg
        downscale_factor (int, optional): factor to downscale the image. Defaults to 5.
    """
    logging.info(f'saving tif as image : {tif_path}')
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
