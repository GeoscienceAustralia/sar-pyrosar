# pyrosar-otf
On the fly production of sentinel-1 RTC Backscatter using pyroSAR

# Requirments
- Git
- Docker

# Setup
- Add user credentials to the files stored in the credentials folder
    - Earthdata credentials - https://urs.earthdata.nasa.gov/users/new
    - Add these to both credentials_earthdata.yaml and .netrc file
- build the docker container
```bash
docker build . -t pyrosar
```

# Instructions
- set scene, path and processing details in config.yaml
- run process scripts
```bash
sh run_process.sh
```
- WARNING - current setup mounts the /data folder inside the container. This assumes *all* of the paths where data will be accessed are in the /data folder (e.g. /data/scenes, /data/pyrosar ...). If data will be across multiple folders without a common root folder (such as /data), the run_process.sh script will need to be changed to mount these folders to /data within the container. 

For example, if scenes are downloaded to /my/path/scenes the run_process.sh script will look like:
```bash
docker run -v /data:/data -v ${PWD}:/app/ -v $HOME/.aws:/root/.aws -v /my/path/scenes:/my/path/scenes -it pyrosar
```