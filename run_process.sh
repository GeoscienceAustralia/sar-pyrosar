CONFIG_PATH="config.yaml"

# accept a -c | config argument if passed
while [[ "$#" -gt 0 ]]; do
  case $1 in
    -c|--config)
      CONFIG_PATH="$2"
      shift 2
      ;;
    *)
      shift # Ignore unknown argument
      ;;
  esac
done

# read the folders we need to mount and map these to where expected
# within the container
pyrosar_output_folder=$(yq '.pyrosar_output_folder' $CONFIG_PATH)
scene_folder=$(yq '.scene_folder' $CONFIG_PATH)
dem_folder=$(yq '.dem_folder' $CONFIG_PATH)
ETAD_folder=$(yq '.ETAD_folder' $CONFIG_PATH)

docker run \
    -v ${PWD}:/app/ \
    -v $pyrosar_output_folder:$pyrosar_output_folder \
    -v $scene_folder:$scene_folder \
    -v $dem_folder:$dem_folder \
    -v $ETAD_folder:$ETAD_folder \
    -w /app/ \
    -it pyrosar:latest_software   \
    python rtc_otf.py -c $CONFIG_PATH