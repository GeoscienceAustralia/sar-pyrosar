# build the container
docker build . -t pyrosar --ulimit nofile=122880:122880

# ignore changes to credentials
git update-index --assume-unchanged credentials/*
git update-index --assume-unchanged credentials/.netrc