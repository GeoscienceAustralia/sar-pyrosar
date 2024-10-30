# build the container
docker build . -t pyrosar --ulimit nofile=122880:122880