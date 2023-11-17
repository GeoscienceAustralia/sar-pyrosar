import yaml
import argparse
import os
import asf_search as asf
import logging
import zipfile
from shapely.geometry import Polygon
import rasterio
from dem_stitcher import stitch_dem
from utils import upload_file, find_files
import time
import re
import shutil
from pyroSAR.snap import geocode
from urllib.request import urlretrieve
import geopandas as gpd
import numpy as np
from pyproj import CRS
from pyproj.aoi import AreaOfInterest
from pyproj.database import query_utm_crs_info


logging.basicConfig(
    format='%(asctime)s %(levelname)-8s %(message)s',
    level=logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S')

if __name__ == "__main__":

    t_start = time.time()
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", "-c", help="path to config.yml", required=True, type=str)
    args = parser.parse_args()

    # define success tracker
    success = {'download': [],
           'opera-rtc': []}

    # read in the config for on the fly (otf) processing
    with open(args.config, 'r', encoding='utf8') as fin:
        otf_cfg = yaml.safe_load(fin.read())
    
    # read in credentials to download from ASF
    with open(otf_cfg['earthdata_credentials'], "r") as f:
        txt = str(f.read())
        uid = txt.split('\n')[1].split('login')[-1][1:]
        pswd = txt.split('\n')[2].split('password')[-1][1:]

    # loop through the list of scenes
    # download data -> produce backscatter -> save
    for i, scene in enumerate(otf_cfg['scenes']):
        
        t_scene_start = time.time()
        logging.info(f'PROCESS 1: Downloads')
        logging.info(f'processing scene {i+1} of {len(otf_cfg["scenes"])} : {scene}')
        # search for the scene in asf
        logging.info(f'searching asf for scene...')
        asf_results = asf.granule_search([scene], asf.ASFSearchOptions(processingLevel='SLC'))
        
        if len(asf_results) > 0:
            logging.info(f'scene found')
            asf_result = asf_results[0]
        else:
            logging.error(f'scene not found : {scene}')
            continue
        
        # download scene
        logging.info(f'downloading scene')
        session = asf.ASFSession()
        session.auth_with_creds(uid,pswd)
        SCENE_NAME = asf_results[0].__dict__['umm']['GranuleUR'].split('-')[0]
        scene_zip = os.path.join(otf_cfg['scene_folder'], SCENE_NAME + '.zip')
        asf_result.download(path=otf_cfg['scene_folder'], session=session)

        # unzip scene
        if otf_cfg['unzip_scene']: 
            SAFE_PATH = scene_zip.replace(".zip",".SAFE")
            logging.info(f'unzipping scene to {SAFE_PATH}')     
            with zipfile.ZipFile(scene_zip, 'r') as zip_ref:
                zip_ref.extractall(otf_cfg['scene_folder'])

        # download a DEM covering the region of interest
        # first get the coordinates from the asf search result
        points = (asf_result.__dict__['umm']['SpatialExtent']['HorizontalSpatialDomain']
                ['Geometry']['GPolygons'][0]['Boundary']['Points'])
        points = [(p['Longitude'],p['Latitude']) for p in points]
        scene_poly = Polygon(points)
        scene_bounds = scene_poly.bounds
        scene_bounds_buff = scene_poly.buffer(1).bounds #buffered

        logging.info(f'downloding DEM for scene bounds : {scene_bounds_buff}')
        logging.info(f'type of DEM being downloaded : {otf_cfg["dem_type"]}')
        # get the DEM and geometry information
        X, p = stitch_dem(scene_bounds_buff,
                        dem_name=otf_cfg['dem_type'],
                        dst_ellipsoidal_height=False,
                        dst_area_or_point='Point')

        # make folders and set filenames
        dem_dl_folder = os.path.join(otf_cfg['dem_folder'],otf_cfg['dem_type'])
        os.makedirs(dem_dl_folder, exist_ok=True)
        dem_filename = SCENE_NAME + '_dem.tif'
        DEM_PATH = os.path.join(dem_dl_folder,dem_filename)
        
        # save with rasterio
        logging.info(f'saving dem to {DEM_PATH}')
        # pyroSAR cant handle a nodata value of np.nan
        # we therefore set this to be -9999
        if np.isnan(p['nodata']):
            replace_nan = True
            p['nodata'] = -9999
        with rasterio.open(DEM_PATH, 'w', **p) as ds:
            if replace_nan:
                X[X==np.nan] = -9999
                X[X=='nan'] = -9999
            ds.write(X, 1)
            ds.update_tags(AREA_OR_POINT='Point')
        del X

        # determine crs if not set by user
        if otf_cfg['pyrosar_t_srs'] == 'default':
            logging.info(f'finding target crs..')
            logging.info(f'scene bounds: {scene_bounds}')
            utm_crs_list = query_utm_crs_info(
                datum_name="WGS 84",
                area_of_interest=AreaOfInterest(
                    west_lon_degree=scene_bounds[0],
                    south_lat_degree=scene_bounds[1],
                    east_lon_degree=scene_bounds[2],
                    north_lat_degree=scene_bounds[3],
                ),
            )
            trg_crs = CRS.from_epsg(utm_crs_list[0].code)
            logging.info(f'target crs: {trg_crs}')
        else:
            trg_crs = otf_cfg['pyrosar_t_srs']
            logging.info(f'target crs: {trg_crs}')


        # run the snap process
        logging.info(f'PROCESS 2: Produce Backscatter')

        # add snap to path if not already there
        if otf_cfg['snap_path'] not in os.environ['PATH']:
            os.environ['PATH'] = os.environ['PATH'] + ':' + otf_cfg['snap_path']

        scene_workflow = geocode(infile=scene_zip,
            outdir=otf_cfg['pyrosar_output_folder'],
            allow_RES_OSV=True,
            externalDEMFile=DEM_PATH,
            externalDEMNoDataValue=-9999,
            spacing=otf_cfg['pyrosar_spacing'],
            scaling=otf_cfg['pyrosar_scaling'],
            refarea=otf_cfg['pyrosar_refarea'],
            t_srs=trg_crs,
            returnWF=True
            )

        logging.info(scene_workflow)
        # keep track of success
        if os.path.exists(prod_path):
            success['opera-rtc'].append(prod_path)
            logging.info(f'RTC Backscatter successfully made')
            
        if otf_cfg['push_to_s3']:
            logging.info(f'PROCESS 3: Push results to S3 bucket')
            bucket = otf_cfg['s3_bucket']
            outputs = [x for x in os.listdir(otf_cfg['pyrosar_output_folder']) if SCENE_NAME in x]
            # set the path in the bucket
            bucket_folder = os.path.join('rtc-opera/',
                                         otf_cfg['dem_type'],
                                         SCENE_NAME)
            for file_ in outputs:
                file_path = os.path.join(otf_cfg['pyrosar_output_folder'],file_)
                bucket_path = os.path.join(bucket_folder,file_)
                logging.info(f'Uploading file: {file_path}')
                logging.info(f'Destination: {bucket_path}')
                upload_file(file_name=file_path, 
                            bucket=bucket, 
                            object_name=bucket_path)
                
            if otf_cfg['upload_dem']:
                bucket_path = os.path.join(bucket_folder,dem_filename)
                logging.info(f'Uploading file: {DEM_PATH}')
                upload_file(DEM_PATH, 
                            bucket=bucket, 
                            object_name=bucket_path)

        if otf_cfg['delete_local_files']:
            logging.info(f'PROCESS 4: Clear files locally')
            #clear downloads
            for file_ in [scene_zip,
                        DEM_PATH,
                        ]:
                logging.info(f'Deleteing {file_}')
                os.remove(file_)
            logging.info(f'Clearing SAFE directory: {SAFE_PATH}')
            shutil.rmtree(SAFE_PATH)
            logging.info(f'Clearing directory: {otf_cfg["pyrosar_output_folder"]}')
            shutil.rmtree(otf_cfg['pyrosar_output_folder'])
            # remake the outdir
            os.makedirs(otf_cfg['pyrosar_output_folder'])
            

        logging.info(f'Scene finished: {SCENE_NAME}')
        logging.info(f'Elapsed time: {((time.time() - t_scene_start)/60)} minutes')


    logging.info(f'Run complete, {len(otf_cfg["scenes"])} scenes processed')
    logging.info(f'Elapsed time:  {((time.time() - t_start)/60)} minutes')








    
