import yaml
import argparse
import os
import asf_search as asf
import logging
import zipfile
from shapely.geometry import Polygon, box
import rasterio
from dem_stitcher import stitch_dem
from utils import (upload_file, 
                   find_files, 
                   expand_raster_with_bounds, 
                   save_tif_as_image,
                   transform_polygon,
                   compress_tif)
import time
import shutil
from pyroSAR.snap import geocode
import geopandas as gpd
import numpy as np
from pyproj import CRS
from pyproj.aoi import AreaOfInterest
from pyproj.database import query_utm_crs_info
import json
import sys

def setup_logging(log_path):

    log = logging.getLogger()  # root logger
    for hdlr in log.handlers[:]:  # remove all old handlers
        log.removeHandler(hdlr)

    # create a haandler to write to file and stdout/console
    logging_file_handler = logging.FileHandler(log_path, mode="w")
    logging.basicConfig(
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging_file_handler
    ],
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(message)s',
    datefmt="%Y-%m-%d %H:%M:%S",
    )

    return logging_file_handler


if __name__ == "__main__":

    t_start = time.time()
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", "-c", help="path to config.yml", required=True, type=str)
    args = parser.parse_args()

    # define success tracker
    success = {'pyrosar-rtc': []}
    failed = {'pyrosar-rtc': []}

    # read in the config for on the fly (otf) processing
    with open(args.config, 'r', encoding='utf8') as fin:
        otf_cfg = yaml.safe_load(fin.read())
    
    # loop through the list of scenes
    # download data -> produce backscatter -> save
    for i, scene in enumerate(otf_cfg['scenes']):
        
        # add the scene name to the out folder
        OUT_FOLDER = otf_cfg['pyrosar_output_folder']
        SCENE_OUT_FOLDER = os.path.join(OUT_FOLDER,scene)
        os.makedirs(SCENE_OUT_FOLDER, exist_ok=True)
        
        #setup logging
        log_path = os.path.join(SCENE_OUT_FOLDER,scene+'.logs')

        # create a haandler to write to file and stdout/console
        logging_file_handler = setup_logging(log_path)

        timing = {}
        t0 = time.time()

        logging.info(f'processing scene {i+1} of {len(otf_cfg["scenes"])} : {scene}')
        logging.info(f'PROCESS 1: Downloads')
        
        # read in aws credentials and set as environ vars
        logging.info(f'setting aws credentials from : {otf_cfg["aws_credentials"]}')
        with open(otf_cfg['aws_credentials'], "r", encoding='utf8') as f:
            aws_cfg = yaml.safe_load(f.read())
            # set all keys as environment variables
            for k in aws_cfg.keys():
                logging.info(f'setting {k}')
                os.environ[k] = aws_cfg[k]
            
        # set parameters to limit search results to single scene
        level = scene.split('_')[2]
        mode = scene.split('_')[1]
        if (('GRD' in level) and (mode=='EW')):
            level = ['GRD_MD','GRD_HD', 'GRD_MS','GRD_FD']
        if (('GRD' in level) and (mode=='IW')):
            level = ['GRD_HS','GRD_HD','GRD_FD']

        # search for the scene in asf
        logging.info(f'searching asf for scene...')
        asf.constants.CMR_TIMEOUT = 45
        logging.debug(f'CMR will timeout in {asf.constants.CMR_TIMEOUT}s')
        asf_results = asf.granule_search(
            [scene], 
            asf.ASFSearchOptions(processingLevel=level, beamMode=mode))
        
        if len(asf_results) == 0:
            logging.error(f'scene not found : {scene}')
            run_success = False
            failed['pyrosar-rtc'].append(scene)
            continue
        if len(asf_results) == 1:
            logging.info(f'scene found')
            asf_result = asf_results[0]
        if len(asf_results) > 1:
            logging.error(f'{asf_results} scenes found, expecting one. \
                          check specified processingLevel ()')
        
        # read in credentials to download from ASF
        logging.info(f'setting earthdata credentials from: {otf_cfg["earthdata_credentials"]}')
        with open(otf_cfg['earthdata_credentials'], "r", encoding='utf8') as f:
            earthdata_cfg = yaml.safe_load(f.read())
            earthdata_uid = earthdata_cfg['login']
            earthdata_pswd = earthdata_cfg['password']
        
        # download scene
        logging.info(f'downloading scene')
        session = asf.ASFSession()
        session.auth_with_creds(earthdata_uid,earthdata_pswd)
        SCENE_NAME = asf_results[0].__dict__['umm']['GranuleUR'].split('-')[0]
        scene_zip = os.path.join(otf_cfg['scene_folder'], SCENE_NAME + '.zip')
        asf_result.download(path=otf_cfg['scene_folder'], session=session)

        # unzip scene
        SAFE_PATH = scene_zip.replace(".zip",".SAFE")
        if otf_cfg['unzip_scene']: 
            logging.info(f'unzipping scene to {SAFE_PATH}')     
            with zipfile.ZipFile(scene_zip, 'r') as zip_ref:
                zip_ref.extractall(otf_cfg['scene_folder'])

        t1 = time.time()
        timing['Download Scene'] = t1 - t0
        
        # orbits are downloaded as part of pyrosar workflow
        # assume following for now
        t2 = time.time()
        timing['Download Orbits'] = t2 - t1

        # download a DEM covering the region of interest
        # first get the coordinates from the asf search result
        points = (asf_result.__dict__['umm']['SpatialExtent']['HorizontalSpatialDomain']
                ['Geometry']['GPolygons'][0]['Boundary']['Points'])
        points = [(p['Longitude'],p['Latitude']) for p in points]
        buffer = 1.5
        scene_poly = Polygon(points)
        scene_poly_buf = scene_poly.buffer(buffer)
        scene_bounds = scene_poly.bounds 
        scene_bounds_buf = scene_poly.buffer(buffer).bounds #buffered
        logging.info(f'Scene bounds : {scene_bounds}')
        logging.info(f'Downloding DEM for  bounds : {scene_bounds_buf}')
        logging.info(f'type of DEM being downloaded : {otf_cfg["dem_type"]}')

        # transform the scene geometries to 3031
        scene_poly_3031 = transform_polygon(4326, 3031, scene_poly)
        scene_poly_buf_3031 = transform_polygon(4326, 3031, scene_poly_buf)
        scene_bounds_3031 = transform_polygon(4326, 3031, box(*scene_bounds))
        scene_bounds_buf_3031 = transform_polygon(4326, 3031, box(*scene_bounds_buf))

        # make folders and set filenames
        dem_dl_folder = os.path.join(otf_cfg['dem_folder'],otf_cfg['dem_type'])
        os.makedirs(dem_dl_folder, exist_ok=True)
        dem_filename = SCENE_NAME + '_dem.tif'
        DEM_PATH = os.path.join(dem_dl_folder,dem_filename)

        # get the DEM and geometry information
        dem_data, dem_meta = stitch_dem(scene_bounds_buf,
                        dem_name=otf_cfg['dem_type'],
                        dst_ellipsoidal_height=False,
                        dst_area_or_point='Point')
        
        # save with rasterio
        logging.info(f'saving dem to {DEM_PATH}')
        # pyroSAR cant handle a nodata value of np.nan
        # we therefore set this to be -9999
        if np.isnan(dem_meta['nodata']):
            logging.info(f'replace dem nodata from np.nan to -9999')
            replace_nan = True
            dem_meta['nodata'] = -9999
        with rasterio.open(DEM_PATH, 'w', **dem_meta) as ds:
            logging.info(f'DEM crs : {ds.meta["crs"]}')
            if replace_nan:
                dem_data[dem_data==np.nan] = -9999
                dem_data[dem_data=='nan'] = -9999
            ds.write(dem_data, 1)
            ds.update_tags(AREA_OR_POINT='Point')
        del dem_data

        # get the bounds of the downloaded DEM
        # the full area requested may not be covered
        dem_bounds = rasterio.transform.array_bounds(
            dem_meta['height'], dem_meta['width'], dem_meta['transform'])
        logging.info(f'Downloaded DEM bounds: {dem_bounds}')
        # Pad the DEM if it does not cover the full area od the scene
        if not box(*dem_bounds).contains_properly(box(*scene_bounds_buf)):
            logging.warning('Downloaded DEM does not cover scene bounds, filling with nodata')
            logging.info('Expanding the bounds of the downloaded DEM')
            DEM_ADJ_PATH = DEM_PATH.replace('.tif','_adj.tif') #adjusted DEM path
            expand_raster_with_bounds(DEM_PATH, DEM_ADJ_PATH, dem_bounds, scene_bounds_buf)
            logging.info(f'Replacing old DEM: {DEM_PATH}')
            os.remove(DEM_PATH)
            os.rename(DEM_ADJ_PATH, DEM_PATH)
        

        t3 = time.time()
        timing['Download DEM'] = t3 - t2

        # determine crs if not set by user
        if otf_cfg['pyrosar_t_srs'] == 'default':
            logging.info(f'finding target crs..')
            logging.info(f'scene bounds: {scene_bounds}')
            # make a small area based on the centroid of the scene
            centre_lat = (scene_bounds[3] + scene_bounds[1])/2
            centre_lon = (scene_bounds[2] + scene_bounds[0])/2
            utm_crs_list = query_utm_crs_info(
                datum_name="WGS 84",
                area_of_interest=AreaOfInterest(
                    west_lon_degree=centre_lon-0.01,
                    south_lat_degree=centre_lat-0.01,
                    east_lon_degree=centre_lon+0.01,
                    north_lat_degree=centre_lat+0.01,
                ),
            )
            logging.info(f'Getting crs at lat: {centre_lat}, lon: {centre_lon}')
            trg_crs = CRS.from_epsg(utm_crs_list[0].code)
            trg_crs = str(trg_crs)
            logging.info(f'target crs: {trg_crs}')
        else:
            trg_crs = otf_cfg['pyrosar_t_srs']
            logging.info(f'target crs: {trg_crs}')

        # run the snap process
        logging.info(f'PROCESS 2: Produce Backscatter')

        # add snap to path if not already there
        if otf_cfg['snap_path'] not in os.environ['PATH']:
            os.environ['PATH'] = os.environ['PATH'] + ':' + otf_cfg['snap_path']

        logging.info(scene_zip)
        logging.getLogger().setLevel(logging.DEBUG)
        scene_workflow = geocode(infile=scene_zip,
            outdir=SCENE_OUT_FOLDER,
            allow_RES_OSV=True,
            externalDEMFile=DEM_PATH,
            externalDEMNoDataValue=-9999,
            externalDEMApplyEGM=False, 
            spacing=otf_cfg['pyrosar_spacing'],
            scaling=otf_cfg['pyrosar_scaling'],
            refarea=otf_cfg['pyrosar_refarea'],
            t_srs=trg_crs,
            returnWF=True,
            clean_edges=True,
            export_extra=["localIncidenceAngle","DEM","layoverShadowMask","scatteringArea"],
            )
        logging.getLogger().setLevel(logging.INFO)

        if scene_workflow is None:
            # scene might already be processed
            xml_filename = find_files(SCENE_OUT_FOLDER, 'xml')[0]
            _, xml_filename = os.path.split(str(xml_filename))
            logging.info(f'Process graph: {xml_filename}')
            # if not an error will be raised as the process failed 
        else:
            _, xml_filename = os.path.split(scene_workflow)
            logging.info(f'Process graph: {xml_filename}')
        scene_start_id = xml_filename.split('_')[6]
        # look for tif if the output product is in tif formate
        RTC_TIF_PATH = ''
        output_folders = [SCENE_OUT_FOLDER] # folders to upolod files from
        for f in os.listdir(SCENE_OUT_FOLDER):
            if ((scene_start_id in f) and ('.tif' in f) and ('rtc' in f)):
                # path to rtc tif
                RTC_TIF_PATH = os.path.join(SCENE_OUT_FOLDER, f)
                IMG_PATH = str(RTC_TIF_PATH.replace('.tif','.png'))
                success['pyrosar-rtc'].append(RTC_TIF_PATH)
                logging.info(f'RTC Backscatter successfully made : {RTC_TIF_PATH}')
        
        # look through nested folder if tif not found, find .img
        RTC_SUB_FOLDER = os.path.join(SCENE_OUT_FOLDER,xml_filename.replace('_proc.xml',''))
        if os.path.exists(RTC_SUB_FOLDER):
            output_folders.append(RTC_SUB_FOLDER)
            for f in os.listdir(RTC_SUB_FOLDER):
                if (('.img' in f) and ('HH' in f) and (('Gamma' in f) or ('Sigma' in f))):
                    RTC_TIF_PATH = os.path.join(RTC_SUB_FOLDER, f)
                    IMG_PATH = str(RTC_TIF_PATH.replace('.tif','.png'))
                    success['pyrosar-rtc'].append(RTC_TIF_PATH)
                    logging.info(f'RTC Backscatter successfully made : {RTC_TIF_PATH}')

        t4 = time.time()
        timing['RTC Processing'] = t4 - t3

        # make a thumbnail image to upload
        # save_tif_as_image(RTC_TIF_PATH, IMG_PATH, downscale_factor=6)

        if otf_cfg['push_to_s3']:
            logging.info(f'PROCESS 3: Push results to S3 bucket')
            bucket = otf_cfg['s3_bucket']
            outputs = [x for x in os.listdir(SCENE_OUT_FOLDER)]
            # set the path in the bucket
            SCENE_PREFIX = '' if otf_cfg["scene_prefix"] == None else otf_cfg["scene_prefix"]
            S3_BUCKET_FOLDER = '' if otf_cfg["s3_bucket_folder"] == None else otf_cfg["s3_bucket_folder"]
            bucket_folder = os.path.join(
                S3_BUCKET_FOLDER,
                'pyrosar',
                otf_cfg['dem_type'],
                f'{str(trg_crs).split(":")[-1]}',
                f'{SCENE_PREFIX}{SCENE_NAME}')
            for output_folder in output_folders:
                outputs = [x for x in os.listdir(output_folder)]
                for file_ in outputs:
                    if '.' in file_[-6:]: 
                        #ensure is a file
                        continue
                    file_path = os.path.join(output_folder,file_)
                    if otf_cfg["img_compression_type"] is not None:
                        if (('.img' in file_) or ('.tif' in file_)):
                            compress_tif(file_path, file_path, compression=otf_cfg["img_compression_type"])
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

        t5 = time.time()
        timing['S3 Upload'] = t5 - t4

        if otf_cfg['delete_local_files']:
            logging.info(f'PROCESS 4: Clear files locally')
            #clear downloads
            for file_ in [scene_zip,
                        DEM_PATH
                        ]:
                logging.info(f'Deleteing {file_}')
                os.remove(file_)
            if os.path.exists(SAFE_PATH):
                logging.info(f'Clearing SAFE directory: {SAFE_PATH}')
                shutil.rmtree(SAFE_PATH)
            logging.info(f'Clearing directory: {SCENE_OUT_FOLDER}')
            try:
                # pyrosar downloads orbit files to current directory, find and delete
                logging.debug('Deleting Orbit files')
                for file_ in os.listdir(os.getcwd()):
                    if (file_.endswith('.EOF') and ('ORB' in file_)):
                        os.remove(os.path.join(os.getcwd(), file_))
                logging.debug(f'Deleting files in {SCENE_OUT_FOLDER}')
                for file_ in os.listdir(SCENE_OUT_FOLDER):
                    if 'log' not in file_:
                        # we clear logs at the end after pushing
                        os.remove(os.path.join(SCENE_OUT_FOLDER, file_))
                logging.debug('Files deleted')
            except:
                logging.debug(f'Changing permissions and deleting files in {SCENE_OUT_FOLDER}')
                os.system(f'sudo chmod -R 777 {SCENE_OUT_FOLDER}')
                for file_ in os.listdir(SCENE_OUT_FOLDER):
                    if 'log' not in file_:
                        # we clear logs at the end after pushing
                        os.remove(os.path.join(SCENE_OUT_FOLDER, file_))
            
        t6 = time.time()
        timing['Delete Files'] = t6 - t5

        logging.info(f'Scene finished: {SCENE_NAME}')
        logging.info(f'Elapsed time: {((time.time() - t0)/60)} minutes')
        timing['Total'] = t6 - t0

        # push timings + logs to s3
        if otf_cfg['push_to_s3']:
            timing_file = SCENE_NAME + '_timing.json'
            bucket_path = os.path.join(bucket_folder, timing_file)
            with open(timing_file, 'w') as fp:
                json.dump(timing, fp)
            logging.info(f'Uploading file: {timing_file}')
            logging.info(f'Destination: {bucket_path}')
            upload_file(file_name=timing_file, 
                        bucket=bucket, 
                        object_name=bucket_path)
            _, log_file = os.path.split(log_path)
            bucket_path = os.path.join(bucket_folder, log_file)
            upload_file(file_name=log_path, 
                        bucket=bucket, 
                        object_name=bucket_path)
            os.remove(timing_file)
        
        logging.getLogger().removeHandler(logging_file_handler)
        os.remove(log_path)

    logging.info(f'Run complete, {len(otf_cfg["scenes"])} scenes processed')
    logging.info(f'Elapsed time:  {((time.time() - t_start)/60)} minutes')