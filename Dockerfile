FROM continuumio/miniconda3 as snap

USER root

# fix error "ttyname failed: Inappropriate ioctl for device"
RUN sed -i ~/.profile -e 's/mesg n || true/tty -s \&\& mesg n/g'
RUN apt-get update && apt-get install -y software-properties-common
RUN apt-get install -y git python3-pip wget libpq-dev

# download SNAP installer
WORKDIR /tmp/
RUN wget https://download.esa.int/step/snap/12.0/installers/esa-snap_all_linux-12.0.0.sh
COPY docker/esa-snap.varfile /tmp/esa-snap.varfile
RUN chmod +x esa-snap_all_linux-12.0.0.sh

# install and update SNAP
RUN /tmp/esa-snap_all_linux-12.0.0.sh -q /tmp/varfile esa-snap.varfile
RUN apt install -y fonts-dejavu fontconfig
COPY docker/update_snap.sh /tmp/update_snap.sh
RUN chmod +x update_snap.sh
RUN /tmp/update_snap.sh

FROM snap as s1_nrb

# setup conda environment
COPY environment.yaml /tmp/environment.yaml
RUN conda update -n base -c defaults conda -y \
 && conda env create --file /tmp/environment.yaml

# ensure environment variables
ENV PATH /opt/conda/envs/nrb_env/bin:$PATH
ENV PROJ_LIB /opt/conda/envs/nrb_env/share/proj

# install project code
WORKDIR /app/
COPY . /app/

# install requirements and pyrosar in the conda env
RUN conda run -n nrb_env pip install -r requirements.txt \
 && conda run -n nrb_env pip uninstall -y pyrosar \
 && conda run -n nrb_env pip install git+https://github.com/johntruckenbrodt/pyroSAR.git@v0.30.0

ENTRYPOINT ["conda", "run", "--no-capture-output", "-n", "nrb_env"]
CMD []