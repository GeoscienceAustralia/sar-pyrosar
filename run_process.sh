# assumes all folders referenced in the config are in /data
# mounts local aws files in the container and uses them for upload
# changes to this may be required with data stored in different folders
docker run -v /data:/data -v ${PWD}:/app/ -v $HOME/.aws:/root/.aws -it pyrosar