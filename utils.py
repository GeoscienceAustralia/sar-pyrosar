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

def transform_polygon(src_crs, dst_crs, geometry, always_xy=True):
    src_crs = pyproj.CRS(f"EPSG:{src_crs}")
    dst_crs = pyproj.CRS(f"EPSG:{dst_crs}") 
    transformer = pyproj.Transformer.from_crs(src_crs, dst_crs, always_xy=always_xy)
     # Transform the polygon's coordinates
    transformed_exterior = [
        transformer.transform(x, y) for x, y in geometry.exterior.coords
    ]
    # Create a new Shapely polygon with the transformed coordinates
    transformed_polygon = Polygon(transformed_exterior)
    return transformed_polygon

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

def expand_raster_with_bounds(input_raster, output_raster, old_bounds, new_bounds, fill_value=None):
    """Expand the raster to the desired bounds. Resolution and Location are preserved.

    Args:
        input_raster (str): input raster path
        output_raster (str): out raster path
        old_bounds (tuple): current bounds
        new_bounds (tuple): new bounds
        fill_value (float, int, optional): Fill value to pad with. Defaults to None and nodata is used.
    """
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
        logging.info(f'Making temp file: {tmp}')
        with rasterio.open(tmp, 'w', **profile) as dst:
            # Read the data from the source and write it to the destination
            fill_value = profile['nodata'] if fill_value is None else fill_value
            logging.info(f'Padding new raster extent with value: {fill_value}')
            data = np.full((new_height, new_width), fill_value=fill_value, dtype=profile['dtype'])
            dst.write(data, 1)
        # merge the old raster into the new raster with expanded bounds 
        logging.info(f'Merging original raster and expanding bounds...')
    del data
    rasterio.merge.merge(
        datasets=[tmp, input_raster],
        method='max',
        dst_path=output_raster)
    os.remove(tmp)

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

def adjust_scene_poly_at_extreme_lat(bbox, src_crs, ref_crs, delta=0.1):
    """
    Adjust the bounding box around a scene in src_crs (4326) due to warping at high
    Latitudes. For example, the min and max boudning values for an antarctic scene in
    4326 may not actually be the true min and max due to distortions at high latitudes. 

    Parameters:
    - bbox: Tuple of four coordinates (x_min, y_min, x_max, y_max).
    - src_crs: Source EPSG. e.g. 4326
    - ref_crs: reference crs to create the true bbox. i.e. 3031 in southern 
                hemisphere and 3995 in northern (polar stereographic)
    - delta: distance between generation points along the bounding box sides in
            src_crs. e.g. 0.1 degrees in lat/lon 

    Returns:
    - A polygon bounding box expanded to the true min max
    """
    x_min, y_min, x_max, y_max = bbox
    # Generate points along the top side
    top_side = [(x, y_max) for x in list(np.arange(x_min, x_max, delta)) + [x_max]]    
    # Generate points along the right side
    right_side = [(x_max, y) for y in list(np.arange(y_max - delta, y_min-delta, -delta)) + [y_min]]
    # Generate points along the bottom side
    bottom_side = [(x, y_min) for x in list(np.arange(x_max - delta, x_min-delta, -delta)) + [x_min]]
    list(np.arange(y_min + delta, y_max, delta)) + [y_max]
    # Generate points along the left side
    left_side = [(x_min, y) for y in list(np.arange(y_min + delta, y_max, delta)) + [y_max]]
    # Combine all sides' points
    all_points = top_side + right_side + bottom_side + left_side
    # convert to a polygon 
    polygon = Polygon(all_points)
    # convert polygon to desired crs and get bounds in those coordinates
    trans_bounds = transform_polygon(src_crs, ref_crs, polygon).bounds
    trans_poly = Polygon(
        [(trans_bounds[0], trans_bounds[1]), 
         (trans_bounds[2], trans_bounds[1]), 
         (trans_bounds[2], trans_bounds[3]), 
         (trans_bounds[0], trans_bounds[3])]
        )
    corrected_poly = transform_polygon(ref_crs, src_crs, trans_poly)
    return corrected_poly