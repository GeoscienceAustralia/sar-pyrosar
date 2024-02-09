# ignore changes to credentials
git update-index --assume-unchanged credentials/*

# build the container
docker build . -t pyrosar