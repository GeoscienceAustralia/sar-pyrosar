# pyrosar-otf
On the fly production of Sentinel-1 RTC Backscatter using pyroSAR (download -> process -> upload). The project makes use of the pyrosar tool - https://github.com/johntruckenbrodt/pyroSAR

# Requirments
- Git
- Docker

# Setup
- Add user credentials to the files stored in the credentials folder
    - Earthdata credentials - https://urs.earthdata.nasa.gov/users/new
        - Add these to both *credentials_earthdata.yaml* and *.netrc* file
    - Copernicus Dataspace - https://dataspace.copernicus.eu/
        - Add these to *credentials_copernicus.yaml*
    - AWS credentials
        - Add these to *credentials_aws.yaml* to enable DEM download and upload to desired destination
- run the setup script to build the docker container
```bash
sh setup.sh
```

# Instructions
- set scene, path and processing details in config.yaml
- run process scripts
```bash
sh run_process.sh
```

# Mount the correct directories in docker

WARNING - current setup mounts the /data folder inside the container. This assumes *all* of the paths where data will be accessed are in the /data folder (e.g. /data/scenes, /data/pyrosar ...). If data is stored across multiple folders without a common root folder (such as /data), the run_process.sh script will need to be changed to mount these folders to /data within the container. The workflow also mounts the local aws files in the container. 

For example, if scenes are downloaded to /my/path/scenes, and other folders are all in /data the run_process.sh script will look like:
```bash
docker run -v /data:/data -v ${PWD}:/app/ -v $HOME/.aws:/root/.aws -v /my/path/scenes:/my/path/scenes -it pyrosar
```