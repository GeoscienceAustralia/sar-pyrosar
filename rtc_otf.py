import yaml
import argparse
import os
import asf_search as asf
import logging
import zipfile
from shapely.geometry import shape
from utils import *
from etad import *
import time
import shutil
from pyroSAR.snap import geocode
from pyproj import CRS
from pyproj.aoi import AreaOfInterest
from pyproj.database import query_utm_crs_info
import json
import sys
from dem_handler.dem.cop_glo30 import get_cop30_dem_for_bounds
from concurrent.futures import ProcessPoolExecutor, as_completed
import traceback


def setup_logging(log_path):
    # Get the root logger and remove any inherited handlers
    log = logging.getLogger()
    for hdlr in log.handlers[:]:
        log.removeHandler(hdlr)

    # Create new handlers
    logging_file_handler = logging.FileHandler(log_path, mode="w")
    logging_stream_handler = logging.StreamHandler(sys.stdout)

    formatter = logging.Formatter(
        fmt='%(asctime)s %(levelname)-8s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    logging_file_handler.setFormatter(formatter)
    logging_stream_handler.setFormatter(formatter)

    log.setLevel(logging.INFO)
    log.addHandler(logging_file_handler)
    log.addHandler(logging_stream_handler)

def run_process(config, scene):

    # read in the config for on the fly (otf) processing
    with open(config, 'r', encoding='utf8') as fin:
        otf_cfg = yaml.safe_load(fin.read())
        
    # add the scene name to the out folder
    OUT_FOLDER = otf_cfg['pyrosar_output_folder']
    SCENE_OUT_FOLDER = os.path.join(OUT_FOLDER,scene)
    os.makedirs(SCENE_OUT_FOLDER, exist_ok=True)
    
    #setup logging
    log_path = os.path.join(OUT_FOLDER,scene+'.logs')

    # create a haandler to write to file and stdout/console
    setup_logging(log_path)

    timing = {}
    t0 = time.time()

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
        error_msg = f'scene not found on asf: {scene}'
        logging.error(error_msg)
        raise FileNotFoundError(error_msg)
    if len(asf_results) == 1:
        logging.info(f'scene found')
        asf_result = asf_results[0]
    if len(asf_results) > 1:
        error_msg = f'{asf_results} scenes found, expecting one. \
                        check specified processingLevel ()'
        logging.error(error_msg)
        raise FileNotFoundError(error_msg)
    
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
    scene_poly = shape(asf_result.geometry)
    scene_bounds = scene_poly.bounds 
    logging.info(f'Scene bounds : {scene_bounds}')
    logging.info(f'Scene bounds : {scene_bounds}')

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
            get_cop30_dem_for_bounds(
                bounds=scene_bounds,
                save_path=DEM_PATH,
                ellipsoid_heights=True,
                adjust_at_high_lat=True,
                buffer_degrees=0.3,
                cop30_folder_path=dem_dl_folder,
                geoid_tif_path=os.path.join(dem_dl_folder,f"{scene}_geoid.tif"),
                download_dem_tiles=True,
                download_geoid=True,
            )
            reassign_nodata_inplace(DEM_PATH, new_nodata=-9999)
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
        externalDEMApplyEGM=False, 
        spacing=otf_cfg['pyrosar_spacing'],
        scaling=otf_cfg['pyrosar_scaling'],
        refarea=otf_cfg['pyrosar_refarea'],
        t_srs=trg_crs,
        returnWF=True,
        clean_edges=True,
        terrainFlattening=otf_cfg['pyrosar_terrainFlattening'],
        export_extra=otf_cfg['pyrosar_export_extra'],
        gpt_args=otf_cfg['gpt_args'],
        )
    logging.getLogger().setLevel(logging.INFO)

    error_files = find_files(SCENE_OUT_FOLDER, 'error')
    if len(error_files) > 0:
        raise ValueError(f'RTC files, see logs: {error_files}')
    
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
            logging.info(f'Deleting {file_}')
            os.remove(file_)
        if os.path.exists(SAFE_PATH):
            logging.info(f'Clearing SAFE directory: {SAFE_PATH}')
            shutil.rmtree(SAFE_PATH)
        if otf_cfg['apply_ETAD']:
            logging.info(f'Clearing SAFE directory: {ETAD_SAFE_PATH}')
            shutil.rmtree(ETAD_SAFE_PATH)
        logging.info(f'Clearing directory: {SCENE_OUT_FOLDER}')
        try:
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
        bucket_path = os.path.join(bucket_folder, f'{scene}.logs')
        upload_file(file_name=log_path, 
                    bucket=bucket, 
                    object_name=bucket_path)
        os.remove(timing_file)
    

def process_scene(config_path, scene):
    try:
        run_process(config_path, scene)
        return (scene, True, None)
    except Exception as e:
        tb_str = traceback.format_exc()
        return (scene, False, tb_str)

if __name__ == "__main__":
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", "-c", help="path to config.yml", required=True, type=str)
    args = parser.parse_args()

    t_start = time.time()
    # define success tracker
    success = {'pyrosar-rtc': []}
    failed = {'pyrosar-rtc': []}

    # read in the config for on the fly (otf) processing
    with open(args.config, 'r', encoding='utf8') as fin:
        otf_cfg = yaml.safe_load(fin.read())

    # loop through the list of scenes
    # download data -> produce backscatter -> save
    scenes = otf_cfg['scenes']
    n_parallel = otf_cfg['n_parallel']
    logging.info(f'Starting processing with {n_parallel} parallel workers')

    with ProcessPoolExecutor(max_workers=n_parallel) as executor:
        futures = [executor.submit(process_scene, args.config, scene) for scene in scenes]
        for future in as_completed(futures):
            scene, ok, tb = future.result()
            if ok:
                success['pyrosar-rtc'].append(scene)
            else:
                failed['pyrosar-rtc'].append(scene)
                logging.error(f"Scene {scene} failed with traceback:\n{tb}")

    logging.info(f'Run complete, attempted to process {len(otf_cfg["scenes"])} scenes')
    logging.info(f'{len(success["pyrosar-rtc"])} scenes successfully processed: ')
    for s in success['pyrosar-rtc']:
        logging.info(f'{s}')
    logging.info(f'{len(failed["pyrosar-rtc"])} scenes FAILED: ')
    for s in failed['pyrosar-rtc']:
        logging.info(f'{s}')
    logging.info(f'Elapsed time:  {((time.time() - t_start)/60)} minutes')
