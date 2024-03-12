
import os
import zipfile
import tarfile
import numpy as np
from datetime import datetime
import requests
import re
import time
import shutil
import s1etad
import logging
from s1etad import Sentinel1Etad, ECorrectionType
from s1etad_tools.cli.slc_correct import s1etad_slc_correct_main

logger = logging.getLogger(__name__)

def download_scene_etad(scene: str, username: str, password: str, etad_dir: str = '', unzip=False):
    """search and download an ETAD product for a corresponding scene. 
        see - https://documentation.dataspace.copernicus.eu/APIs/OData.html

    Args:
        scene (str): scene of interest. e.g. S1A_IW_SLC__1SSH_20231119T083317_20231119T083345_051283_062FEC_0B2C
        etad_dir (str): where to save the downloaded product
        username (str): username for the copernicus dataspace
        password (str): password for the copernicus dataspace
    Returns:
        etad_path : path to the downloaded ETAD product. None if a product was not found.
    """

    sat, mode, prod,_, pol, start, finish = scene.split('_')[:7]

    logger.info(f'Searching Copernicus Dataspace for ETAD file...')
    # find the ETAD using the start and end timestamps from the SLC
    search_results = requests.get(
        f"https://catalogue.dataspace.copernicus.eu/odata/v1/Products?$filter=contains(Name,'ETA') and contains(Name,'{start}') and contains(Name,'{finish}')&$orderby=ContentDate/Start&$top=100"
        ).json()['value']
    files = [res['Name'] for res in search_results]
    logger.info(f'ETAD files found {files}')
    assert len(search_results) == 1, "error. more than one ETAD product found for scene"
    prod_id = search_results[0]['Id']
    prod_name = search_results[0]['Name']
    etad_filename = prod_name + '.zip'
    
    # get a token from copernicus hub to enable download
    data = {
        'grant_type': 'password',
        'username': f'{username}',
        'password': f'{password}',
        'client_id': 'cdse-public',
    }
    response = requests.post(
        'https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token', data=data)
    access_token = response.json()['access_token']

    # download the ETAD product
    url = f"https://zipper.dataspace.copernicus.eu/odata/v1/Products({prod_id})/$value"
    headers = {"Authorization": f"Bearer {access_token}"}
    session = requests.Session()
    session.headers.update(headers)
    response = session.get(url, headers=headers, stream=True)
    etad_path = os.path.join(etad_dir, etad_filename)
    
    logger.info(f'Downloding ETAD to : {etad_path}')
    with open(f"{etad_path}", "wb") as file:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                file.write(chunk)

    if unzip:
        etad_safe = etad_path.replace('.zip', '')
        logging.info(f'Unzipping to : {etad_safe}')
        if not os.path.isdir(etad_safe):
            archive = zipfile.ZipFile(etad_path, 'r')
            archive.extractall(etad_dir)
            archive.close()

    return etad_path if not unzip else etad_safe


def find_etad_file(scene, ETAD_dir):
    """_summary_

    Args:
        scene (str): scene. e.g. S1A_IW_SLC__1SSH_20231119T083317_20231119T083345_051283_062FEC_0B2C
        ETAD_dir (str): locally accessible directory containing downloaded ETAD products

    Returns:
        _type_: _description_
    """
    # application of correction from https://github.com/SAR-ARD/S1_NRB/blob/main/S1_NRB/etad.py
    ETAD_file = None
    sat, mode, prod,_, pol, start, finish = scene.split('_')[:7]
    logger.info(f'Searching local directory for ETAD product : {ETAD_dir}')
    for f in os.listdir(ETAD_dir):
        if all(substring in f for substring in [sat, mode, pol[-2:], start, finish]):
            ETAD_file = f
            logger.info(f'ETAD found : {ETAD_file}')
            break
    if ETAD_file is None:
        logger.info('ETAD file not found')
    return ETAD_file

def apply_etad_correction(slc_path: str, ETAD_file: str, out_dir: str, nthreads: int=4):
    """
    Apply ETAD correction to a Sentinel-1 SLC product.
    
    Parameters
    ----------
    slc_path: str
        The path to the Sentinel-1 SLC.
    etad_dir: str
        The directory containing ETAD products. This will be searched for products using the SLC.
    out_dir: str
        The directory to store results. An unzipped SAFE folder structure is created.
    nthreads: the number of threads
        The number of threads used for processing. Defaults to 4.

    Returns
    -------
    str
        path to the corrected SLC SAFE product.
    """
    logger.info('Correcting SLC with ETAD product')
    slc_corrected_dir = os.path.join(out_dir)
    os.makedirs(slc_corrected_dir, exist_ok=True)
    slc_base = os.path.basename(slc_path).replace('.zip', '.SAFE')
    slc_corrected = os.path.join(slc_corrected_dir, slc_base)
    if not os.path.isdir(slc_corrected):
        start_time = time.time()
        ext = os.path.splitext(ETAD_file)[1]
        if ext in ['.tar', '.zip']:
            if '.SAFE' in ETAD_file:
                # remove the ext after the safe
                etad_base = os.path.basename(ETAD_file).replace(ext, '')
            else:
                etad_base = os.path.basename(ETAD_file).replace(ext, '.SAFE')
            etad_folder = os.path.dirname(ETAD_file)
            etad = os.path.join(etad_folder, etad_base)
            if not os.path.isdir(etad):
                if ext == '.tar':
                    archive = tarfile.open(ETAD_file, 'r')
                else:
                    archive = zipfile.ZipFile(ETAD_file, 'r')
                archive.extractall(etad_folder)
                archive.close()
        elif ext == '.SAFE':
            etad = ETAD_file
        else:
            raise RuntimeError('ETAD products are required to be .tar/.zip archives or .SAFE folders')
        s1etad_slc_correct_main(s1_product=slc_path,
                                etad_product=etad,
                                outdir=slc_corrected_dir,
                                nthreads=nthreads,
                                order=0)  # using the default 1 introduces a bias of about -0.5 dB.
        t = round((time.time() - start_time), 2)
        logger.info(f'Time taken: {t}')
    else:
        logger.info(f'ETAD corrected product already exists: {slc_corrected}')
    return slc_corrected