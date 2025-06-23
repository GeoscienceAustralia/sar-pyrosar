# pyrosar-otf
On the fly production of Sentinel-1 RTC Backscatter using pyroSAR (download -> process -> upload). The project makes use of the pyrosar tool - https://github.com/johntruckenbrodt/pyroSAR

# Requirments
- Git
- Docker

# Setup
1. Add user credentials to the files stored in the credentials folder
    - Earthdata credentials - https://urs.earthdata.nasa.gov/users/new
        - Add these to both *credentials_earthdata.yaml* and *.netrc* file
    - Copernicus Dataspace - https://dataspace.copernicus.eu/
        - Add these to *credentials_copernicus.yaml*
    - AWS credentials
        - Add these to *credentials_aws.yaml* to enable DEM download and upload to desired destination

2. install yq
```bash
sudo wget https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 -O /usr/local/bin/yq
sudo chmod +x /usr/local/bin/yq
```

3. Build the docker container
```bash
docker build . -t pyrosar --ulimit nofile=122880:122880
```

# Instructions
- set scene, path and processing details in config.yaml
- run process scripts
```bash
sh run_process.sh -c config.yaml
```
Note, volumes are mounted within the docker container based on the settings in the config.yaml