import yaml
import argparse
import os
import asf_search as asf
import logging
import zipfile
from shapely.geometry import Polygon, box
import rasterio
from dem_stitcher import stitch_dem
from utils import *
from etad import *
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
        applied_scene_file = scene_zip

        # unzip scene if specified or ETAD is to be applied
        SAFE_PATH = scene_zip.replace(".zip",".SAFE")
        if otf_cfg['unzip_scene'] or otf_cfg['apply_ETAD']: 
            logging.info(f'unzipping scene to {SAFE_PATH}')     
            with zipfile.ZipFile(scene_zip, 'r') as zip_ref:
                zip_ref.extractall(otf_cfg['scene_folder'])
            applied_scene_file = SAFE_PATH

        # apply the ETAD corrections to the slc
        if otf_cfg['apply_ETAD']:
            logging.info('Applying ETAD corrections')
            logging.info(f'loading copernicus credentials from: {otf_cfg["copernicus_credentials"]}')
            with open(otf_cfg['copernicus_credentials'], "r", encoding='utf8') as f:
                copernicus_cfg = yaml.safe_load(f.read())
                copernicus_uid = copernicus_cfg['login']
                copernicus_pswd = copernicus_cfg['password']
            etad_path = download_scene_etad(
                SCENE_NAME, 
                copernicus_uid, 
                copernicus_pswd, etad_dir=otf_cfg['ETAD_folder'])
            ETAD_SCENE_FOLDER = f'{otf_cfg["scene_folder"]}_ETAD'
            logging.info(f'making new directory for etad corrected slc : {ETAD_SCENE_FOLDER}')
            ETAD_SAFE_PATH = apply_etad_correction(
                SAFE_PATH, 
                etad_path, 
                out_dir=ETAD_SCENE_FOLDER,
                nthreads=otf_cfg['gdal_threads'])
            applied_scene_file = ETAD_SAFE_PATH
        
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
        scene_poly = Polygon(points)
        scene_bounds = scene_poly.bounds 
        logging.info(f'Scene bounds : {scene_bounds}')

        # if we are at high latitudes we need to correct the bounds due to the skewed box shape
        if (scene_bounds[1] < -50) or (scene_bounds[3] < -50):
            # Southern Hemisphere
            logging.info(f'Adjusting scene bounds due to warping at high latitude')
            scene_poly = adjust_scene_poly_at_extreme_lat(scene_bounds, 4326, 3031)
            scene_bounds = scene_poly.bounds 
            logging.info(f'Adjusted scene bounds : {scene_bounds}')
        if (scene_bounds[1] > 50) or (scene_bounds[3] > 50):
            # Northern Hemisphere
            logging.info(f'Adjusting scene bounds due to warping at high latitude')
            scene_poly = adjust_scene_poly_at_extreme_lat(scene_bounds, 4326, 3995)
            scene_bounds = scene_poly.bounds 
            logging.info(f'Adjusted scene bounds : {scene_bounds}')

        buffer = 0.1
        scene_bounds_buf = scene_poly.buffer(buffer).bounds #buffered

        if otf_cfg['dem_path'] is not None:
            # set the dem to be the one specified if supplied
            logging.info(f'using DEM path specified : {otf_cfg["dem_path"]}')
            if not os.path.exists(otf_cfg['dem_path']):
                raise FileExistsError(f'{otf_cfg["dem_path"]} c')
            else:
                DEM_PATH = otf_cfg['dem_path']
                dem_filename = os.path.basename(DEM_PATH)
                otf_cfg['dem_folder'] = os.path.dirname(DEM_PATH) # set the dem folder
                otf_cfg['overwrite_dem'] = False # do not overwrite dem
        else:
            # make folders and set filenames
            dem_dl_folder = os.path.join(otf_cfg['dem_folder'],otf_cfg['dem_type'])
            os.makedirs(dem_dl_folder, exist_ok=True)
            dem_filename = SCENE_NAME + '_dem.tif'
            DEM_PATH = os.path.join(dem_dl_folder,dem_filename)
        
        if (otf_cfg['overwrite_dem']) or (not os.path.exists(DEM_PATH)) or (otf_cfg['dem_path'] is None):
            logging.info(f'Downloding DEM for  bounds : {scene_bounds_buf}')
            logging.info(f'type of DEM being downloaded : {otf_cfg["dem_type"]}')
            # get the DEM and geometry information
            dem_data, dem_meta = stitch_dem(scene_bounds_buf,
                            dem_name=otf_cfg['dem_type'],
                            dst_ellipsoidal_height=True,
                            dst_area_or_point='Point',
                            merge_nodata_value=0,
                            fill_to_bounds=True,
                            )
            
            # save with rasterio
            logging.info(f'saving dem to {DEM_PATH}')
            with rasterio.open(DEM_PATH, 'w', **dem_meta) as ds:
                ds.write(dem_data, 1)
                ds.update_tags(AREA_OR_POINT='Point')
            del dem_data
        else:
            logging.info(f'Using existing DEM : {DEM_PATH}')

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

        logging.info(f'Performing RTC on file : {applied_scene_file}')
        logging.getLogger().setLevel(logging.DEBUG)
        scene_workflow = geocode(infile=applied_scene_file,
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
            terrainFlattening=otf_cfg['pyrosar_terrainFlattening'],
            export_extra=otf_cfg['pyrosar_export_extra'],
            #gpt_args=otf_cfg['gpt_args'],
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
        # look for tif if the output product is in tif format
        RTC_TIF_PATH = ''
        output_folders = [SCENE_OUT_FOLDER] # folders to upolod files from
        for f in os.listdir(SCENE_OUT_FOLDER):
            if ((scene_start_id in f) and ('.tif' in f) and ('rtc' in f)):
                # path to rtc tif
                RTC_TIF_PATH = os.path.join(SCENE_OUT_FOLDER, f)
                IMG_PATH = str(RTC_TIF_PATH.replace('.tif','.png'))
                success['pyrosar-rtc'].append(RTC_TIF_PATH)
                logging.info(f'RTC Backscatter successfully made : {RTC_TIF_PATH}')
                save_tif_as_image(RTC_TIF_PATH, IMG_PATH, downscale_factor=6)
        
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
                    save_tif_as_image(RTC_TIF_PATH, IMG_PATH, downscale_factor=6)

        t4 = time.time()
        timing['RTC Processing'] = t4 - t3

        if otf_cfg['push_to_s3']:
            logging.info(f'PROCESS 3: Push results to S3 bucket')
            bucket = otf_cfg['s3_bucket']
            outputs = [x for x in os.listdir(SCENE_OUT_FOLDER)]
            # set the path in the bucket
            SCENE_PREFIX = '' if otf_cfg["scene_prefix"] == None else otf_cfg["scene_prefix"]
            S3_BUCKET_FOLDER = '' if otf_cfg["s3_bucket_folder"] == None else otf_cfg["s3_bucket_folder"]
            bucket_folder = os.path.join(
                S3_BUCKET_FOLDER,
                otf_cfg['software'],
                otf_cfg['dem_type'],
                f'{str(trg_crs).split(":")[-1]}',
                f'{SCENE_PREFIX}{SCENE_NAME}')
            for output_folder in output_folders:
                outputs = [x for x in os.listdir(output_folder)]
                for file_ in outputs:
                    if '.' not in file_[-6:]: 
                        #ensure is a file
                        continue
                    file_path = os.path.join(output_folder,file_)
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
            clear_files = [scene_zip, DEM_PATH]
            for file_ in clear_files:
                logging.info(f'Deleteing {file_}')
                os.remove(file_)
            if os.path.exists(SAFE_PATH):
                logging.info(f'Clearing SAFE directory: {SAFE_PATH}')
                shutil.rmtree(SAFE_PATH)
            if otf_cfg['apply_ETAD']:
                logging.info(f'Clearing SAFE directory: {ETAD_SAFE_PATH}')
                shutil.rmtree(ETAD_SAFE_PATH)
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
        
        # save timing file
        timing_file = SCENE_NAME + '_timing.json'
        with open(timing_file, 'w') as fp:
                json.dump(timing, fp)
        
        # push timings + logs to s3
        if otf_cfg['push_to_s3']:
            bucket_path = os.path.join(bucket_folder, timing_file)
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